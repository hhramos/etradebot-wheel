"""
wheel_bot.py — Autonomous Wheel Strategy Monitor (Sprint 4b)
=============================================================
Called by server.py scheduler at each market checkpoint.
Reads tokens from _session (already authenticated by UI) — no re-auth needed.

Run modes (set in data/config.json → "run_mode"):
  dry_run   — evaluate and log actions, place NO orders  (default, always safe)
  semi      — auto-execute exits (BTC) only; queue entries for human approval
  full      — execute exits autonomously; queue entries for human approval

Entries are ALWAYS queued for human approval regardless of mode.
Only exits (risk-reducing BUY_CLOSE orders) are ever auto-executed.

Scheduler fire points (from server.py):
  premarket  06:30 ET  — evaluate overnight, queue day's actions
  open       09:35 ET  — final entry check, confirm exit GTC orders live
  intraday   every 30m — monitor exit triggers
  preclose   15:45 ET  — close anything that should not hold overnight
  postclose  16:15 ET  — record fills, update state
"""

from __future__ import annotations

import os
import json
import logging
import datetime

logger = logging.getLogger(__name__)

# ── Config path ────────────────────────────────────────────────────────────
_CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "config.json")
_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "trade_log.json")


def _load_config() -> dict:
    try:
        with open(_CFG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _append_trade_log(entry: dict) -> None:
    try:
        try:
            with open(_LOG_PATH) as f:
                log = json.load(f)
        except Exception:
            log = []
        log.append(entry)
        with open(_LOG_PATH, "w") as f:
            json.dump(log[-500:], f, indent=2, default=str)  # keep last 500 entries
    except Exception as e:
        logger.warning(f"trade_log write failed: {e}")


def _queue_action(session: dict, action: dict) -> None:
    """Add an action to the pending queue shown in the UI."""
    pending = session.setdefault("_pending_actions", [])
    # Deduplicate by ticker + action type
    key = f"{action.get('ticker')}:{action.get('action')}"
    existing_keys = {f"{a.get('ticker')}:{a.get('action')}" for a in pending}
    if key not in existing_keys:
        pending.append({**action, "queued_at": datetime.datetime.now().isoformat()})


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_cycle(session: dict, dry_run: bool | None = None) -> dict:
    """
    Run one monitoring cycle. Called by server.py scheduler.

    session:  _session dict from server.py (tokens, NAV, cached positions/screener)
    dry_run:  override config — None means read from config.json "run_mode"

    Returns a summary dict stored in session["_last_bot_run"] and
    appended to data/trade_log.json.
    """
    summary: dict = {
        "ran_at":  datetime.datetime.now().isoformat(),
        "mode":    None,
        "fills":   [],
        "exits":   [],
        "queued":  [],
        "entries": [],
        "errors":  [],
        "skipped": [],
    }

    # ── 0. Resolve run mode ────────────────────────────────────────────────
    cfg = _load_config()
    if dry_run is None:
        dry_run = cfg.get("run_mode", "dry_run") != "full"
    semi    = cfg.get("run_mode") == "semi"
    auto_exit = not dry_run   # semi + full both auto-execute exits
    summary["mode"] = "dry_run" if dry_run else ("semi" if semi else "full")
    logger.info(f"[BOT] cycle start — mode={summary['mode']}")

    # ── 1. Import bot modules (lazy — avoids import errors if modules missing)
    try:
        from bot.wheel_engine import WheelEngine, Phase
        from bot.etrade_api   import ETradeAPI, TokenExpiredError, ETradeAPIError
        from bot.trade_rules  import (should_exit_position, validate_entry,
                                      check_position_allowed, get_sizing_rules)
        from bot.time_utils   import today_et, is_market_open, days_to_expiry
    except ImportError as e:
        summary["errors"].append(f"import error: {e}")
        session["_last_bot_run"] = summary
        return summary

    # ── 2. Load wheel state ────────────────────────────────────────────────
    engine = WheelEngine()
    try:
        engine.load()
    except Exception as e:
        summary["errors"].append(f"wheel_engine.load: {e}")

    # ── 3. Build API client using session tokens directly ─────────────────
    # ETradeAPI reads from a token file, but the server stores tokens in
    # _session memory only. Build OAuth session directly from session dict.
    consumer_key    = session.get("consumer_key","")
    consumer_secret = session.get("consumer_secret","")
    access_token    = session.get("access_token","")
    access_secret   = session.get("access_token_secret","")
    account_id      = session.get("account_id","")

    if not all([consumer_key, consumer_secret, access_token, access_secret]):
        summary["errors"].append("Token expired — re-authenticate via UI")
        logger.warning("[BOT] no tokens in session — re-auth needed")
        session["_last_bot_run"] = summary
        return summary

    try:
        from requests_oauthlib import OAuth1Session as _OA1
        api_sess = _OA1(
            consumer_key,
            client_secret         = consumer_secret,
            resource_owner_key    = access_token,
            resource_owner_secret = access_secret,
        )
        # Quick connectivity test
        _test = api_sess.get(
            f"https://api.etrade.com/v1/accounts/{account_id}/balance",
            headers={"Accept":"application/json"},
            params={"instType":"BROKERAGE","realTimeNAV":True},
            timeout=10,
        )
        if _test.status_code == 401:
            session["_token_expired"] = True
            summary["errors"].append("Token expired — 401 on connectivity test")
            session["_last_bot_run"] = summary
            return summary
    except Exception as e:
        summary["errors"].append(f"OAuth session init: {e}")
        session["_last_bot_run"] = summary
        return summary

    # Wrap in a simple fills fetcher using the live OAuth session
    def _get_fills(since_dt):
        """Fetch filled orders since since_dt using live session tokens."""
        try:
            resp = api_sess.get(
                f"https://api.etrade.com/v1/accounts/{account_id}/orders",
                headers={"Accept":"application/json"},
                params={"status":"EXECUTED","count":25},
                timeout=15,
            )
            if resp.status_code == 401:
                session["_token_expired"] = True
                raise TokenExpiredError("401 fetching orders")
            if resp.status_code != 200:
                return []
            data = resp.json()
            orders = (data.get("OrdersResponse",{})
                         .get("Order",[]) or [])
            fills = []
            for o in orders:
                for leg in o.get("OrderDetail",[{}])[0].get("Instrument",[]):
                    fills.append({
                        "ticker":     leg.get("Product",{}).get("symbol",""),
                        "action":     leg.get("orderAction",""),
                        "option_type":leg.get("Product",{}).get("callPut",""),
                        "strike":     leg.get("Product",{}).get("strikePrice"),
                        "expiry":     None,
                        "contracts":  leg.get("filledQuantity",0),
                        "fill_price": leg.get("averageExecutionPrice",0),
                    })
            return fills
        except TokenExpiredError:
            raise
        except Exception as e:
            logger.warning(f"[BOT] fills fetch error: {e}")
            return []

    # ── 4. Fetch recent fills and process state transitions ────────────────
    last_run = engine._state.get("last_run")
    since = (datetime.datetime.fromisoformat(last_run)
             if last_run else
             datetime.datetime.now() - datetime.timedelta(hours=8))
    try:
        fills = _get_fills(since)
        logger.info(f"[BOT] {len(fills)} fills since {since.strftime('%H:%M')}")
    except TokenExpiredError:
        fills = []
        summary["errors"].append("Token expired fetching fills")
    except Exception as e:
        fills = []
        summary["errors"].append(f"get_fills: {e}")

    for fill in fills:
        ticker = fill.get("ticker", "")
        action = fill.get("action", "")
        price  = float(fill.get("fill_price", 0) or 0)
        try:
            if action == "BUY_CLOSE":
                wheel   = engine._state.get("wheels", {}).get(ticker, {})
                sold    = float(wheel.get("csp_premium", price * 2) or price * 2)
                profit  = round((sold - price) * 100 * int(fill.get("contracts", 1)), 2)
                engine.on_btc_filled(ticker, profit, order_id=None)
                summary["fills"].append({"ticker": ticker, "action": "BTC",
                                          "profit": profit, "price": price})
                logger.info(f"[BOT] BTC fill {ticker} @ ${price} profit=${profit}")

            elif action == "SELL_OPEN" and fill.get("option_type") == "PUT":
                engine.on_csp_filled(
                    ticker,
                    fill_price = price,
                    order_id   = None,
                )
                summary["fills"].append({"ticker": ticker, "action": "CSP_FILL",
                                          "premium": price})
                logger.info(f"[BOT] CSP fill {ticker} ${fill.get('strike')} @ ${price}")

            elif fill.get("assigned"):
                engine.on_assigned(ticker,
                                   shares=int(fill.get("contracts", 1)) * 100,
                                   strike=float(fill.get("strike", 0)))
                summary["fills"].append({"ticker": ticker, "action": "ASSIGNED"})
                logger.info(f"[BOT] ASSIGNED {ticker} @ ${fill.get('strike')}")

        except Exception as e:
            summary["errors"].append(f"fill {ticker} {action}: {e}")

    # ── 5. Evaluate exits for all open CSP and CC positions ───────────────
    nav = float(session.get("_net_value", 0) or 0)
    positions = session.get("_last_positions", [])

    for pos in positions:
        ptype = pos.get("type", "")
        if ptype not in ("CSP", "CC"):
            continue
        ticker   = pos.get("ticker", "")
        cost     = float(pos.get("cost", 0) or 0)
        current  = float(pos.get("current", 0) or 0)
        expiry   = pos.get("expiry", "")
        strikes  = float(pos.get("strike", 0) or 0)
        conts    = int(pos.get("contracts", 1) or 1)
        earnings = pos.get("earnings_date")
        liquid   = pos.get("is_liquid", True)

        try:
            dte = days_to_expiry(expiry) if expiry else 999
        except Exception:
            dte = 999

        try:
            should_exit, reason = should_exit_position(
                cost, current, dte,
                earnings_date = earnings,
                is_liquid     = liquid,
                ticker        = ticker,
            )
        except Exception as e:
            summary["errors"].append(f"should_exit {ticker}: {e}")
            continue

        if not should_exit:
            summary["skipped"].append({"ticker": ticker, "dte": dte,
                                        "pnl_pct": pos.get("pnl_pct")})
            continue

        # Build the BTC order
        limit_price = round(float(current) * 1.02, 2)  # 2% above current ask
        btc_rec = {
            "ticker":      ticker,
            "action":      "BUY_CLOSE",
            "option_type": ptype,
            "strike":      strikes,
            "expiry":      expiry,
            "contracts":   conts,
            "limit_price": limit_price,
            "tif":         "DAY",
            "reason":      reason,
        }

        if auto_exit and not dry_run:
            try:
                result = api.place_order(**btc_rec)
                order_id = result.get("order_id", "?")
                summary["exits"].append({**btc_rec, "order_id": order_id})
                logger.info(f"[BOT] EXIT placed {ticker}: {reason} → #{order_id}")
            except TokenExpiredError:
                summary["errors"].append(f"Token expired placing exit {ticker}")
                _queue_action(session, btc_rec)
                summary["queued"].append(btc_rec)
            except Exception as e:
                summary["errors"].append(f"exit order {ticker}: {e}")
                _queue_action(session, btc_rec)
                summary["queued"].append(btc_rec)
        else:
            _queue_action(session, btc_rec)
            summary["queued"].append(btc_rec)
            logger.info(f"[BOT] EXIT queued {ticker}: {reason}")

    # ── 6. Evaluate entries (always queued, never auto-executed) ──────────
    try:
        market_open = is_market_open()
    except Exception:
        market_open = False

    if market_open and nav > 0:
        candidates = session.get("_screener_cache", {}).get("candidates", [])
        rules      = get_sizing_rules(nav)
        today_str  = datetime.date.today().isoformat()

        for c in candidates[:10]:   # top 10 by wheel score
            ticker = c.get("ticker", "")
            strike = c.get("csp_strike")
            expiry = c.get("csp_expiry", "")
            prem   = c.get("premium_est", 0)
            iv     = c.get("iv_pct", 0)
            liquid = c.get("is_liquid", True)
            delta  = c.get("entry_delta")
            d_earn = c.get("days_to_earnings")

            # Skip if already running a wheel on this ticker
            wheel_phase = engine._state.get("wheels", {}).get(ticker, {}).get("phase")
            if wheel_phase not in (None, "IDLE", "COMPLETE"):
                continue

            # Skip if already have an open position in this ticker
            already_open = any(p.get("ticker") == ticker and
                               p.get("type") in ("CSP","CC")
                               for p in positions)
            if already_open:
                continue

            # Entry validation
            try:
                dte_entry  = days_to_expiry(expiry) if expiry else 0
                earn_date  = None
                if d_earn is not None and d_earn >= 0:
                    earn_date = (datetime.date.today() +
                                 datetime.timedelta(days=d_earn)).isoformat()
                ok, errs, warns = validate_entry(
                    ticker, dte_entry, earn_date, delta, liquid, iv)
                if not ok:
                    continue
            except Exception as e:
                summary["errors"].append(f"validate_entry {ticker}: {e}")
                continue

            # Concentration check
            try:
                allowed, cerrs, _ = check_position_allowed(
                    ticker, strike, 1, nav, positions)
                if not allowed:
                    continue
            except Exception as e:
                summary["errors"].append(f"check_allowed {ticker}: {e}")
                continue

            # Queue for human approval
            entry_rec = {
                "ticker":      ticker,
                "action":      "SELL_OPEN",
                "option_type": "PUT",
                "strike":      strike,
                "expiry":      expiry,
                "contracts":   1,
                "limit_price": prem,
                "tif":         "DAY",
                "reason":      (f"Fisher:{c.get('fisher_score')} "
                                f"Wheel:{c.get('wheel_grade')}·{c.get('wheel_score')} "
                                f"ROC:{c.get('roc_est', 0):.2f}%"),
            }
            _queue_action(session, entry_rec)
            summary["entries"].append(entry_rec)
            logger.info(f"[BOT] ENTRY queued {ticker} ${strike} {expiry}")
            break   # one new entry candidate per cycle

    # ── 7. Save state and log ─────────────────────────────────────────────
    try:
        engine._state["last_run"]      = datetime.datetime.now().isoformat()
        engine._state["last_run_mode"] = summary["mode"]
        engine.save()
    except Exception as e:
        summary["errors"].append(f"engine.save: {e}")

    # ── 8. Update persistent memory ───────────────────────────────────────
    try:
        from bot.memory import update as _mem_update
        _mem_update(session, positions)
    except Exception as e:
        logger.debug(f"memory.update skipped: {e}")

    # Write structured event records so projection.html can render them
    # (summary blob has no "event" field — projection filter won't match it)
    for q in summary.get("queued", []):
        _append_trade_log({
            "event":   "QUEUED_EXIT"  if q.get("action") == "BUY_CLOSE" else "QUEUED_ENTRY",
            "ticker":  q.get("ticker",""),
            "ts":      summary["ran_at"],
            "mode":    summary["mode"],
            "action":  q.get("action",""),
            "reason":  q.get("reason",""),
            "strike":  q.get("strike"),
            "expiry":  q.get("expiry"),
            "limit_price": q.get("limit_price"),
        })
    for e in summary.get("exits", []):
        _append_trade_log({
            "event":    "EXIT_PLACED",
            "ticker":   e.get("ticker",""),
            "ts":       summary["ran_at"],
            "mode":     summary["mode"],
            "order_id": e.get("order_id",""),
            "reason":   e.get("reason",""),
        })
    for f in summary.get("fills", []):
        evt = {"BTC":"CYCLE_COMPLETE","CSP_FILL":"CSP_FILLED","ASSIGNED":"ASSIGNED"}.get(f.get("action",""),"FILL")
        _append_trade_log({
            "event":  evt,
            "ticker": f.get("ticker",""),
            "ts":     summary["ran_at"],
            "profit": f.get("profit"),
            "premium": f.get("premium"),
        })
    # Also keep the raw summary for debugging
    _append_trade_log(summary)
    session["_last_bot_run"] = summary

    n_q = len(summary["queued"]) + len(summary["entries"])
    n_e = len(summary["exits"])
    n_f = len(summary["fills"])
    logger.info(
        f"[BOT] cycle done — fills:{n_f} exits:{n_e} queued:{n_q} "
        f"errors:{len(summary['errors'])} mode:{summary['mode']}"
    )
    return summary
