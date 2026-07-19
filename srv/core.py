"""Shared state, config, helpers, scheduler — extracted from server.py.
ORDER MATTERS in this file: config path, guardrails, and the advisor
system prompt resolve top-to-bottom exactly as the original did.
"""
"""
server.py — ETradeBot Wheel Strategy UI Backend
================================================
Flask backend that bridges ui/index.html with the existing etradebot
etrade/ module and wheel_screener_strategy.

Start with:
    python server.py

Endpoints:
    POST /auth/init          — store consumer key+secret in session memory
    POST /auth/token         — exchange verifier for access token
    GET  /auth/status        — current connection status
    GET  /positions          — live portfolio positions
    GET  /orders             — open + recent orders
    POST /order/preview      — preview an order (no execution)
    POST /order/submit       — submit an order to E-Trade
    GET  /screener           — run wheel screener, return ranked candidates
    POST /run                — run bot.run() in preview or live mode
    GET  /account            — account balance and buying power

Security:
    - consumer_key and consumer_secret are held in server-side session memory
      only (Flask session, NOT stored to disk or database)
    - Access token stored in memory for the session duration
    - No credentials are logged
"""

import os
import json
import logging

# Repo root — this file lives in srv/, data/ and ui/ live one level up
import os as _os_for_base
_BASE_DIR = _os_for_base.path.dirname(_os_for_base.path.dirname(_os_for_base.path.abspath(__file__)))
from flask import Flask, request, jsonify, session, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── File logging (rotates at 2 MB, keeps 3 backups) ──────────────────────
try:
    from logging.handlers import RotatingFileHandler as _RFH
    _LOG_FILE = os.path.join(_BASE_DIR, "data", "server.log")
    _fh = _RFH(_LOG_FILE, maxBytes=2*1024*1024, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception as _lfe:
    logger.warning(f"Could not set up file logging: {_lfe}")

UI_DIR = os.path.join(_BASE_DIR, "ui")

app = Flask(__name__)
app.secret_key = os.urandom(32)   # ephemeral -- new key every server start
CORS(app, supports_credentials=True)

# ---------------------------------------------------------------------------
# Static UI file serving
# ---------------------------------------------------------------------------

@app.route("/")
def root():
    resp = send_from_directory(UI_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/ui/<path:filename>")
def serve_ui(filename):
    resp = send_from_directory(UI_DIR, filename)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

# ---------------------------------------------------------------------------
# In-memory session store (cleared on server restart)
# ---------------------------------------------------------------------------
_session = {
    "consumer_key":    None,
    "consumer_secret": None,
    "oauth_obj":       None,   # pyetrade.ETradeOAuth instance held between init and token calls
    "access_token":    None,
    "access_token_secret": None,
    "account_id":      None,
    "connected":       False,
}

# Module-level config path — used by get_bot_mode, set_bot_mode, and wheel_bot
_CFG_PATH = os.path.join(_BASE_DIR, "data", "config.json")

# Load sector map and sizing config on startup
try:
    _cfg_path = _CFG_PATH   # alias for the try block below
    with open(_cfg_path) as _f:
        _cfg_boot = json.load(_f)
    _session["_sector_map"]    = _cfg_boot.get("sector_map", {})
    _session["_sizing_tiers"]  = _cfg_boot.get("position_sizing", {}).get("tiers", [])
    logger.info(f"Sector map loaded: {len(_session['_sector_map'])} tickers")
except Exception as _e:
    _session["_sector_map"]   = {}
    _session["_sizing_tiers"] = []
    logger.warning(f"Could not load sector config: {_e}")

# ── Load wheel strategy guardrail playbook ─────────────────────────────────
_GUARDRAILS_TEXT  = ""
_GUARDRAILS_QUICK = ""
try:
    from html.parser import HTMLParser as _HTMLParser
    import re as _re2

    class _GuardrailParser(_HTMLParser):
        def __init__(self):
            super().__init__()
            self.texts = []
            self._skip = False
        def handle_starttag(self, tag, attrs):
            if tag in ("style", "script"):
                self._skip = True
        def handle_endtag(self, tag):
            if tag in ("style", "script"):
                self._skip = False
        def handle_data(self, data):
            if not self._skip:
                s = data.strip()
                if s:
                    self.texts.append(s)

    _gr_path = os.path.join(_BASE_DIR, "data", "guardrails.html")
    if os.path.exists(_gr_path):
        _gp = _GuardrailParser()
        _gp.feed(open(_gr_path, encoding="utf-8").read())
        _raw_gr = "\n".join(_gp.texts)
        _GUARDRAILS_TEXT = _re2.sub(r"\n{3,}", "\n\n", _raw_gr).strip()
        _t0 = _GUARDRAILS_TEXT.find("NEW POSITION OPPORTUNITY?")
        _t1 = _GUARDRAILS_TEXT.find("The Wheel Strategy in 60 Seconds")
        if _t0 > 0:
            _GUARDRAILS_QUICK = _GUARDRAILS_TEXT[_t0 : _t1 if _t1 > _t0 else _t0 + 1200].strip()
        logger.info(f"Guardrails loaded: {len(_GUARDRAILS_TEXT)} chars")
    else:
        logger.warning("data/guardrails.html not found — advisor running without playbook")
except Exception as _ge:
    logger.warning(f"Could not load guardrails: {_ge}")


# ---------------------------------------------------------------------------
# Debug endpoint — visit /debug in browser to diagnose environment issues
# ---------------------------------------------------------------------------

@app.route("/debug", methods=["GET"])
def debug():
    import sys, importlib
    info = {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "packages": {}
    }
    for pkg in ["flask", "flask_cors", "pyetrade", "requests", "requests_oauthlib"]:
        try:
            m = importlib.import_module(pkg)
            info["packages"][pkg] = getattr(m, "__version__", "installed (no version attr)")
        except ImportError as e:
            info["packages"][pkg] = f"NOT INSTALLED: {e}"
    return jsonify(info)


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def _exit_reason(cost, current, expiry, earnings_date=None, is_liquid=True, ticker=""):
    """Return which exit rule is active for a position, or empty string."""
    if not cost or cost <= 0 or current is None:
        return ""
    dte = 999
    if expiry:
        try:
            from bot.time_utils import days_to_expiry
            dte = days_to_expiry(expiry)
        except Exception:
            try:
                import datetime
                dte = (datetime.date.fromisoformat(expiry) - datetime.date.today()).days
            except Exception:
                # dte stays 999 → the 21 DTE exit will NEVER fire for this
                # position. Log loudly so a malformed expiry is diagnosable.
                logger.warning(f"DTE unparseable for expiry={expiry!r} — "
                               f"DTE exit disabled for this position")
    try:
        from bot.trade_rules import should_exit_position as _sep
        fired, reason = _sep(cost, current, dte,
                             earnings_date=earnings_date, is_liquid=is_liquid,
                             ticker=ticker)
        if fired:
            return reason
    except Exception:
        pass
    decay_pct = (cost - current) / cost * 100
    if dte <= 21 and current >= cost:
        return f"21 DTE ({dte}d left, at loss — consider roll)"
    return ""


def _classify_position(pos, qty=0, cost=None, current=None, expiry=None):
    """
    Determine wheel action for a position.
    Exit rules: 50% premium decay OR 21 DTE — whichever comes first.
    """
    product   = pos.get("Product", pos.get("product", {}))
    sec_type  = product.get("securityType", "EQ")
    call_put  = product.get("callPut", "")
    days_gain = float(pos.get("daysGain", 0) or 0)

    if sec_type == "OPTN":
        # Guard: positive qty means LONG option — BTC/Roll not valid
        if qty > 0:
            return "LONG_OPT"
        if call_put == "PUT":
            # ── Compute real premium decay ─────────────────────────────
            # Use passed-in cost/current (from positions parser) if available,
            # else fall back to E*Trade totalGainPct estimate
            if cost and cost > 0 and current is not None:
                decay_pct = (cost - current) / cost * 100
            else:
                # Fallback: E*Trade totalGainPct (less accurate but always present)
                decay_pct = float(pos.get("totalGainPct", 0) or 0)

            # ── Compute DTE ────────────────────────────────────────────
            dte = 999   # default: far from expiry
            if expiry:
                try:
                    from bot.time_utils import days_to_expiry
                    dte = days_to_expiry(expiry)
                except Exception:
                    try:
                        import datetime
                        exp = datetime.date.fromisoformat(expiry)
                        dte = (exp - datetime.date.today()).days
                    except Exception:
                        logger.warning(f"DTE unparseable for expiry={expiry!r} — "
                                       f"DTE exit disabled for this position")

            in_profit = (current is not None and cost is not None
                         and cost > 0 and current < cost)

            # ── Multi-factor exit rules ────────────────────────────────
            if decay_pct >= 65:
                return "CLOSE_RECYCLE"
            try:
                from bot.trade_rules import should_exit_position as _sep
                _ok, _ = _sep(
                    cost or 0, current or 0, dte,
                    earnings_date=pos.get("earnings_date"),
                    is_liquid=pos.get("is_liquid", True),
                    ticker=pos.get("ticker",""),
                )
                if _ok:
                    return "BTC_READY"
            except Exception:
                if decay_pct >= 50 or (dte <= 21 and in_profit):
                    return "BTC_READY"
            if dte <= 21 and not in_profit:
                return "ROLL"
            elif days_gain < -15:
                return "ROLL"
            else:
                return "HOLD"
        else:
            # Covered call — same dual-trigger logic
            if cost and cost > 0 and current is not None:
                decay_pct = (cost - current) / cost * 100
            else:
                decay_pct = float(pos.get("totalGainPct", 0) or 0)
            dte = 999
            if expiry:
                try:
                    from bot.time_utils import days_to_expiry
                    dte = days_to_expiry(expiry)
                except Exception:
                    try:
                        import datetime
                        exp = datetime.date.fromisoformat(expiry)
                        dte = (exp - datetime.date.today()).days
                    except Exception:
                        logger.warning(f"DTE unparseable for expiry={expiry!r} — "
                                       f"DTE exit disabled for this position")
            in_profit = (current is not None and cost is not None
                         and cost > 0 and current < cost)
            if decay_pct >= 50 or (dte <= 21 and in_profit):
                return "BTC_READY"
            elif dte <= 21 and not in_profit:
                return "ROLL"
            else:
                return "HOLD"
    else:
        qty_abs = abs(qty)
        if qty_abs < 50:
            return "MONITOR_ONLY"
        elif qty_abs < 100:
            return "ACCUMULATE"
        elif qty_abs % 100 == 0:
            return "SELL_CC"
        else:
            return "SELL_CC_PARTIAL"


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Order preview + submit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------

def _annotate_candidates(candidates):
    """Add wheel_collateral_pct and cached signals to each candidate."""
    net_value = _session.get("_net_value", 0) or 0
    # Pull any already-cached signals from wheel_screener
    try:
        from wheel_screener import _signals_cache
        sig_cache = _signals_cache
    except Exception:
        sig_cache = {}

    for c in candidates:
        strike = float(c.get("csp_strike") or c.get("price") or 0)
        collateral = strike * 100
        c["csp_collateral"] = round(collateral, 2)
        if net_value > 0:
            c["wheel_collateral_pct"] = round(collateral / net_value * 100, 2)
        else:
            c["wheel_collateral_pct"] = None

        # Attach cached signals if available (non-blocking)
        ticker  = c.get("ticker", "")
        cached  = sig_cache.get(ticker, {}).get("signals")
        c["insider"]   = cached.get("insider",   {"signal":"none","label":"—"}) if cached else {"signal":"none","label":"—"}
        c["political"] = cached.get("political", {"signal":"none","label":"—"}) if cached else {"signal":"none","label":"—"}
        c["social"]    = cached.get("social",    {"signal":"none","label":"—"}) if cached else {"signal":"none","label":"—"}
    return candidates


# ---------------------------------------------------------------------------
# Run bot
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Wheel Advisor — Ollama / Qwen 2.5 streaming endpoint
# ---------------------------------------------------------------------------

OLLAMA_URL   = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:7b"   # default; overridden at runtime by /advisor/model
_active_model = {"name": OLLAMA_MODEL}   # mutable container

ADVISOR_SYSTEM = """You are an expert options wheel strategy advisor. You analyze brokerage positions and provide specific, actionable next-step recommendations.

POSITION TYPE RULES — READ CAREFULLY:
- "STOCK Nsh" means N shares of stock. It is NOT an option. Never recommend rolling a stock position.
- "CSP $X exp DATE ×Nc" means a Cash-Secured Put option. This CAN be rolled or closed early.
- "CC $X exp DATE ×Nc" means a Covered Call option. This CAN be rolled or closed early.
- A ticker can appear multiple times (e.g. CCL STOCK 100sh AND CCL CC $30). Treat each line independently.
- If a STOCK position is marked "HOLD_CC — CC already open", do NOT suggest selling a covered call. One is already active. Only suggest managing the existing CC.
- Any position marked "NOT wheel-eligible" or "sub-lot" or "monitor only" MUST be listed in Position Actions as "Monitor — no action" or simply omitted. NEVER suggest CC, CSP, roll, or BTC on these positions. They cannot support options trades.
- Only positions with 100+ shares can sell a covered call. Do NOT suggest CC on any position with fewer than 100 shares.

WHEEL STRATEGY RULES YOU MUST FOLLOW:
- Sell Cash-Secured Puts (CSP) on high-quality dividend stocks at 5-10% OTM
- Buy to Close (BTC) when premium decays to 50% profit — do not wait for expiry
- After stock assignment (STOCK position appears), immediately Sell Covered Calls (CC) at or above cost basis
- Roll a threatened CSP or CC (delta > 0.30, deep ITM) out in time and/or down in strike for net credit
- Never allocate more than the stated per-position cap as collateral on a single position (tiered by account size — see POSITION SIZING RULES in the prompt)
- ALL collateral % in this prompt = (strike × 100 × contracts) ÷ NET VALUE — NOT buying power
- A position flagged ⚠OVER-CAP exceeds the per-position limit for this account size
- Respect SECTOR CAP — do not suggest new positions that would push a sector over its stated limit
- Reinvest freed capital into top screener picks immediately
- Fisher score 9+ and Wheel grade A/B are preferred for new entries

ENTRY TIMING RULES (QuantWheel framework — apply to all new CSP entries):
- IV Rank > 60%: premium is elevated relative to the past year — support entry
- IV Rank 30-60%: marginal — note the moderate premium environment in reasoning
- IV Rank < 30%: flag as poor timing, explicitly recommend waiting for IV expansion
- VIX < 15: note compressed market-wide premiums across candidates
- VIX > 25: elevated opportunity window — favorable conditions for premium selling
- Post-earnings window (1-3 days after earnings): IV crush still elevated, good entry timing
- SMA trend: prefer CSPs on stocks trading above their 200d SMA — flag below_200 as a warning
- Intraday: first 30 minutes after open (9:30-10:00 ET) has widest spreads — note if relevant

PREMIUM vs COLLATERAL — CRITICAL DISTINCTION:
- "Coll(1c)=$4,500" means the COLLATERAL required (strike × 100). This is NOT the premium.
- "ROC=4.5%" means Return on Collateral — the actual premium as a % of collateral.
- "EstPremium=$2.01" means the estimated credit received for selling 1 contract.
- In the "Est. Premium" column of your output table, ALWAYS show the credit received (e.g. $2.01), NOT the collateral ($4,500).

NUMERICAL REASONING RULE:
- Before writing any math conclusion, verify it. Example: "8% of $6,484 = $519. Is $6,700 below $519? No — it is OVER the cap."
- Never state that a large number is below a small number.

OUTPUT FORMAT — you must always respond with these exact sections:

## Position Actions
| Ticker | Type | P&L% | Recommendation | Reasoning |
|--------|------|-------|----------------|-----------|
(one row per position — use exact ticker+type from the prompt, do not invent positions)

## New Positions to Open
| Ticker | Action | Strike | Expiry | Est. Premium (credit) | Collateral | Coll% NAV | Reasoning |
|--------|--------|--------|--------|-----------------------|------------|-----------|-----------|
(top 2-3 new CSPs that FIT within 8% NAV cap — skip any where Coll%NAV > 8%)

## Portfolio Risk Assessment
2-3 sentences on overall exposure, concentration, and one key risk to watch.

Be specific with strikes, expiries, and dollar amounts. Do not give general advice.

GUARDRAIL REFERENCE:
A full 8-phase Wheel Strategy Playbook is embedded in this session.
Before making ANY suggestion, verify it against the relevant phase check.
Name the phase when citing a guardrail reason (e.g. "Phase A: earnings within window").

QUICK DECISION TREE — use this for every entry and exit routing decision:
__GUARDRAILS_QUICK_PLACEHOLDER__"""

# Resolve guardrail placeholder now that ADVISOR_SYSTEM is defined
# _GUARDRAILS_QUICK was loaded at module startup above
ADVISOR_SYSTEM = ADVISOR_SYSTEM.replace(
    "__GUARDRAILS_QUICK_PLACEHOLDER__",
    _GUARDRAILS_QUICK or "(data/guardrails.html not found — add it to the data/ folder)"
)


def build_advisor_prompt() -> str:
    """Build the user prompt from live session data."""
    acct      = _session.get("_last_account", {})
    positions = _session.get("_last_positions", [])
    cache     = _session.get("_screener_cache", {})

    # 30-day memory context — injected before positions block
    _mem_ctx = ""
    try:
        from bot.memory import build_memory_context as _bmc
        _mem_ctx = _bmc(days=30)
    except Exception as _me:
        logger.debug(f"memory context skipped: {_me}")

    nav   = acct.get("net_value", 0)    or 0
    bp    = acct.get("buying_power", 0) or 0
    cash  = acct.get("cash_balance", 0) or 0

    try:
        from bot.trade_rules import get_sizing_rules as _gsr3, concentration_summary
        _rules = _gsr3(nav)
        _pp    = _rules["per_position_pct"]
        _pt    = _rules["per_ticker_pct"]
        _ps    = _rules["sector_max_pct"]
    except Exception:
        _pp = _pt = 0.08; _ps = 0.30
    cap = nav * _pp

    lines  = []
    if _mem_ctx:
        lines.append(_mem_ctx)
    lines += [
        f"ACCOUNT: Net value (NAV) ${nav:,.0f} | Buying power ${bp:,.0f} | Cash ${cash:,.0f}",
        f"POSITION SIZING RULES (tiered for ${nav:,.0f} NAV):",
        f"  Per-position cap: {_pp*100:.0f}% = ${nav*_pp:,.0f}",
        f"  Per-ticker cap:   {_pt*100:.0f}% = ${nav*_pt:,.0f}",
        f"  Sector cap:       {_ps*100:.0f}% = ${nav*_ps:,.0f}",
        f"NOTE: all collateral % = (strike×100×contracts)÷NAV — NOT % of buying power",
        f"A position flagged ⚠OVER-CAP exceeds the {_pp*100:.0f}% per-position limit.\n",
    ]

    # Sector concentration summary
    if positions:
        try:
            conc = concentration_summary(positions, nav)
            if conc.get("per_sector"):
                lines.append("CURRENT SECTOR EXPOSURE:")
                for sec, info in sorted(conc["per_sector"].items(), key=lambda x:-x[1]["pct"]):
                    flag = " ⚠OVER-SECTOR-CAP" if info["over_cap"] else ""
                    lines.append(f"  {sec}: ${info['collateral']:,.0f} = {info['pct']}% of NAV{flag}")
                lines.append(f"Total deployed: {conc['total_deployed']}% of NAV\n")
        except Exception:
            pass

    if positions:
        lines.append("OPEN POSITIONS:")
        for p in positions:
            sc = cache.get(p["ticker"], {})
            f  = sc.get("fisher_score", "—")
            wg = sc.get("wheel_grade",  "—")
            ws = sc.get("wheel_score",  "—")
            wheel_str = f"Fisher:{f} Wheel:{wg}·{ws}" if wg != "—" else "Fisher:— Wheel:—"
            if p["type"] in ("CSP", "CC"):
                strike     = float(p.get("strike", 0) or 0)
                contracts  = int(p.get("contracts", 1) or 1)
                collateral = strike * 100 * contracts
                coll_pct   = round(collateral / nav * 100, 1) if nav > 0 else 0
                try:
                    from bot.trade_rules import get_sizing_rules as _gsr
                    _cap_pct = _gsr(_session.get("_net_value",0) or nav or 0)["per_position_pct"] * 100
                except Exception:
                    _cap_pct = 8.0
                cap_flag   = f" ⚠OVER-{_cap_pct:.0f}%-CAP" if coll_pct > _cap_pct else ""
                earnings   = p.get("earnings_date","")
                is_liquid  = p.get("is_liquid", True)
                earn_note  = f" | ⚠EARNINGS-{earnings}" if earnings else ""
                liq_note   = " | ⚠ILLIQUID(use 14 DTE)" if not is_liquid else ""
                lines.append(
                    f"  {p['ticker']} {p['type']} ${p['strike']} exp {p['expiry']} ×{contracts}c"
                    f" | Sold ${p['cost']} now ${p['current']}"
                    f" | {'+' if p['pnl']>=0 else ''}${p['pnl']} ({'+' if p['pnl_pct']>=0 else ''}{p['pnl_pct']}%)"
                    f" | collateral ${collateral:,.0f} = {coll_pct}% of NAV{cap_flag}"
                    f"{earn_note}{liq_note}"
                    f" | {p['action']} | {wheel_str}"
                )
            else:
                contracts  = int(p.get("contracts", 0) or 0)
                cost       = float(p.get("cost", 0) or 0)
                mkt_val    = round(cost * contracts, 2)
                val_pct    = round(mkt_val / nav * 100, 1) if nav > 0 else 0
                full_c   = contracts // 100
                leftover = contracts % 100
                if contracts < 50:
                    wheel_note = "NOT wheel-eligible — monitor only, do not suggest CC or roll"
                elif contracts < 100:
                    wheel_note = f"sub-lot — need {100-contracts} more shares for CC, do not suggest CC or roll"
                elif contracts % 100 == 0:
                    wheel_note = (f"wheel-eligible — sell {full_c} covered call"
                                  f"{'s' if full_c>1 else ''} ({full_c}×100sh = {full_c}c)")
                else:
                    wheel_note = (f"sell {full_c}c CC on {full_c*100}sh (full lots)"
                                  f" · {leftover}sh leftover sub-lot — monitor only")
                lines.append(
                    f"  {p['ticker']} STOCK {contracts}sh"
                    f" | Cost ${p['cost']} now ${p['current']}"
                    f" | {'+' if p['pnl']>=0 else ''}${p['pnl']} ({'+' if p['pnl_pct']>=0 else ''}{p['pnl_pct']}%)"
                    f" | mkt value ${mkt_val:,.0f} = {val_pct}% of NAV"
                    f" | {wheel_note}"
                )
    else:
        lines.append("OPEN POSITIONS: none")

    # Top screener picks
    top_picks = sorted(
        [v for v in cache.values() if v.get("wheel_grade") in ("A","B")],
        key=lambda x: (-x.get("wheel_score",0), -x.get("fisher_score",0))
    )[:5]
    if top_picks:
        lines.append("\nTOP SCREENER PICKS (live Yahoo Finance data, collateral = 1 contract ÷ NAV):")
        for p in top_picks:
            strike_p = float(p.get("csp_strike") or p.get("price") or 0)
            coll_p   = round(strike_p * 100 / nav * 100, 1) if nav > 0 and strike_p else 0
            try:
                from bot.trade_rules import get_sizing_rules as _gsr2
                _pcap = _gsr2(nav)["per_position_pct"] * 100
            except Exception:
                _pcap = 8.0
            cap_flag = f" ⚠OVER-{_pcap:.0f}%-CAP" if coll_p > _pcap else ""
            iv_p      = float(p.get("iv_pct", 25) or 25)
            prem_est  = round(p.get("premium_est") or iv_p / 500 * strike_p, 2)
            roc       = p.get("roc_est") or round(prem_est / strike_p * 100, 2) if strike_p else 0
            dte_earn   = p.get("days_to_earnings")
            earn_warn  = (f" ⚠EARNINGS-IN-{dte_earn}d" if dte_earn is not None
                          and 0 <= dte_earn <= 21 else "")
            liq_warn   = "" if p.get("is_liquid", True) else " ⚠ILLIQUID-14DTE"
            e_delta    = p.get("entry_delta")
            delta_str  = f" delta:{e_delta:.2f}" if e_delta else ""
            lines.append(
                f"  {p['ticker']} Fisher:{p.get('fisher_score','—')} "
                f"Wheel:{p.get('wheel_grade','—')}·{p.get('wheel_score','—')} "
                f"Price:${p.get('price','—')} Strike:${strike_p} "
                f"Expiry:{p.get('csp_expiry','—')} "
                f"IV:{iv_p}% ROC:{roc}%{delta_str} "
                f"EstPremium(1c)=${prem_est} "
                f"Coll(1c)=${strike_p*100:,.0f}={coll_p}%ofNAV{cap_flag}"
                f"{earn_warn}{liq_warn}"
            )

    lines.append("\nAnalyze these positions and provide your structured recommendations.")
    return "\n".join(lines)


# Conversation history — persists within server session
_conversation = []   # list of {role, content}


def _ollama_reachable():
    """Try 127.0.0.1 then localhost; update OLLAMA_URL and return bool."""
    global OLLAMA_URL
    import urllib.request
    for host in ["http://127.0.0.1:11434", "http://localhost:11434"]:
        try:
            urllib.request.urlopen(host, timeout=3)
            OLLAMA_URL = host + "/api/chat"
            return True
        except Exception:
            continue
    return False


_SENTINEL = object()   # signals end of queue

def _stream_ollama(messages):
    """Generator: stream tokens from Ollama via queue + heartbeat thread.

    A background thread reads from Ollama and puts items on a queue.
    The main (Flask response) thread pulls from the queue and yields SSE.
    A heartbeat puts a ping on the queue every 5s so Flask never idles long
    enough to trigger a WSGI/proxy timeout — even on slow CPU inference.
    """
    import json, time as _t, threading, queue as _queue, urllib.request, http.client

    q          = _queue.Queue()
    start_t    = _t.time()
    token_count= 0
    full_reply = []

    payload = json.dumps({
        "model":   _active_model["name"],
        "stream":  True,
        "messages": messages,
        "options": {"temperature": 0.3, "top_p": 0.9},
    }).encode()

    def ollama_reader():
        """Runs in background thread; puts {content}, {done}, {error} dicts."""
        try:
            req = urllib.request.Request(
                OLLAMA_URL, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=360) as resp:  # phi4 ~127s max observed
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    if _session.get("_advisor_abort"):
                        _session["_advisor_abort"] = False
                        q.put({"done": True, "aborted": True,
                               "elapsed": round(_t.time() - start_t, 1),
                               "model":   _active_model["name"]})
                        q.put(_SENTINEL)
                        return
                    try:
                        obj   = json.loads(line)
                        token = obj.get("message", {}).get("content", "")
                        done  = obj.get("done", False)
                        if token:
                            q.put({"content": token})
                        if done:
                            q.put({"done": True,
                                   "elapsed": round(_t.time() - start_t, 1),
                                   "model":   _active_model["name"]})
                            q.put(_SENTINEL)
                            return
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            q.put({"error": True, "content": f"⚠ Advisor error: {e}"})
            q.put(_SENTINEL)

    def heartbeat():
        """Puts a ping every 5s so the main thread never blocks > 5s."""
        while True:
            _t.sleep(5)
            if q.empty():
                q.put({"ping": True})

    threading.Thread(target=ollama_reader, daemon=True).start()
    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    while True:
        try:
            item = q.get(timeout=380)   # hard ceiling — phi4 needs ~127s, extra buffer
        except _queue.Empty:
            yield "data: " + json.dumps({"error": True,
                "content": "⚠ Timed out waiting for Ollama (>380s). Try a shorter prompt or switch to qwen2.5."}) + "\n\n"
            yield "data: [DONE]\n\n"
            return

        if item is _SENTINEL:
            break

        if item.get("ping"):
            yield ": heartbeat\n\n"   # SSE comment — keeps connection alive, invisible to JS
            continue

        if item.get("error"):
            yield "data: " + json.dumps(item) + "\n\n"
            yield "data: [DONE]\n\n"
            return

        if item.get("content"):
            token = item["content"]
            token_count += 1
            full_reply.append(token)
            yield "data: " + json.dumps({"content": token}) + "\n\n"

        if item.get("done"):
            full_text = "".join(full_reply)
            _conversation.append({"role": "assistant", "content": full_text})
            item["tokens"] = token_count
            # Persist recommendation to memory
            try:
                from bot.memory import record_recommendation as _rr
                _user_q = next((m["content"] for m in reversed(_conversation)
                                if m["role"] == "user"), "")
                _rr(_user_q, full_text, _active_model.get("name","?"), _session)
            except Exception as _rre:
                logger.debug(f"record_recommendation skipped: {_rre}")
            yield "data: " + json.dumps(item) + "\n\n"
            yield "data: [DONE]\n\n"
            return


# Keep GET /advisor/analyze for backward compat — wraps chat endpoint


# ---------------------------------------------------------------------------
# Signals — Insider / Political / Social
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bot projection data — feeds the 3-year projection page
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 2 — Token expiry endpoint
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Steps 3 & 4 — Data persistence helpers
# ---------------------------------------------------------------------------

import json as _json
from datetime import datetime, timezone

DATA_DIR      = os.path.join(_BASE_DIR, "data")
SNAPSHOTS_DIR = os.path.join(DATA_DIR, "snapshots")
TRADE_LOG     = os.path.join(DATA_DIR, "trade_log.json")


def _ensure_dirs():
    os.makedirs(DATA_DIR,      exist_ok=True)
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)


def _et_now_str() -> str:
    """Return current ET time as a readable string without requiring zoneinfo."""
    try:
        from bot.time_utils import now_et, timestamp_et
        return timestamp_et(now_et())
    except Exception:
        # Fallback: UTC offset approx (EDT = UTC-4, EST = UTC-5)
        import time as _t
        utc = datetime.now(timezone.utc)
        # Rough DST check: second Sunday March → first Sunday November
        import calendar
        y = utc.year
        # Second Sunday in March
        mar = datetime(y, 3, 8)
        dst_start = mar + __import__('datetime').timedelta(days=(6 - mar.weekday()) % 7)
        # First Sunday in November
        nov = datetime(y, 11, 1)
        dst_end = nov + __import__('datetime').timedelta(days=(6 - nov.weekday()) % 7)
        offset = -4 if dst_start <= utc.replace(tzinfo=None) < dst_end else -5
        et = utc + __import__('datetime').timedelta(hours=offset)
        return et.strftime("%Y-%m-%d %H:%M:%S ET")


def _append_trade_log(entry: dict):
    """Append one entry to data/trade_log.json (creates file if missing)."""
    _ensure_dirs()
    try:
        existing = []
        if os.path.exists(TRADE_LOG):
            with open(TRADE_LOG, "r") as f:
                existing = _json.load(f)
        existing.append(entry)
        # Keep last 2000 entries to prevent unbounded growth
        if len(existing) > 2000:
            existing = existing[-2000:]
        with open(TRADE_LOG, "w") as f:
            _json.dump(existing, f, indent=2)
    except Exception as e:
        logger.warning(f"trade_log write failed: {e}")


def _write_snapshot(entry: dict):
    """Append poll entry to data/snapshots/YYYY-MM-DD.json."""
    _ensure_dirs()
    try:
        date_str  = _et_now_str()[:10]   # "2026-06-09"
        snap_path = os.path.join(SNAPSHOTS_DIR, f"{date_str}.json")
        existing  = []
        if os.path.exists(snap_path):
            with open(snap_path, "r") as f:
                existing = _json.load(f)
        existing.append(entry)
        with open(snap_path, "w") as f:
            _json.dump(existing, f, indent=2)
    except Exception as e:
        logger.warning(f"snapshot write failed: {e}")


def _write_daily_summary():
    """Write end-of-day summary to data/snapshots/YYYY-MM-DD_summary.json."""
    _ensure_dirs()
    try:
        date_str  = _et_now_str()[:10]
        snap_path = os.path.join(SNAPSHOTS_DIR, f"{date_str}.json")
        polls     = []
        if os.path.exists(snap_path):
            with open(snap_path, "r") as f:
                polls = _json.load(f)

        if not polls:
            return

        open_nav  = polls[0].get("account", {}).get("net_value", 0)
        close_nav = polls[-1].get("account", {}).get("net_value", 0)

        # Collect all unique alerts across the day
        all_alerts = []
        for p in polls:
            all_alerts.extend(p.get("actions_flagged", []))

        summary = {
            "date":           date_str,
            "open_nav":       open_nav,
            "close_nav":      close_nav,
            "nav_change":     round(close_nav - open_nav, 2),
            "polls":          len(polls),
            "alerts_today":   list(dict.fromkeys(all_alerts)),   # deduplicated, ordered
            "generated_et":   _et_now_str(),
        }
        summary_path = os.path.join(SNAPSHOTS_DIR, f"{date_str}_summary.json")
        with open(summary_path, "w") as f:
            _json.dump(summary, f, indent=2)
        logger.info(f"Daily summary written: NAV {open_nav} → {close_nav} | {len(polls)} polls")
    except Exception as e:
        logger.warning(f"daily summary write failed: {e}")


def _persist_poll(trigger: str, positions: list, account: dict):
    """Build a poll entry, write to trade_log and daily snapshot."""
    ts = _et_now_str()
    actions_flagged = [
        f"{p['action']}: {p['ticker']}"
        for p in positions
        if p.get("action") in ("BTC_READY", "ROLL", "CLOSE_RECYCLE", "SELL_CC")
    ]
    entry = {
        "ts":              ts,
        "trigger":         trigger,
        "account":         {
            "net_value":    account.get("net_value", 0),
            "buying_power": account.get("buying_power", 0),
            "cash_balance": account.get("cash_balance", 0),
        },
        "position_count":  len(positions),
        "positions":       positions,
        "actions_flagged": actions_flagged,
    }
    _append_trade_log(entry)
    _write_snapshot(entry)
    if actions_flagged:
        logger.info(f"[POLL] {trigger} — {len(positions)} positions — ALERTS: {', '.join(actions_flagged)}")
    else:
        logger.info(f"[POLL] {trigger} — {len(positions)} positions — no alerts")
    return entry


# ---------------------------------------------------------------------------
# Endpoint: last N trade log entries (for future log viewer UI)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 1 — Scheduler thread (NYSE-aware, ET time)
# ---------------------------------------------------------------------------

import threading
import time as _time

# Schedule config mirrors config.json schedule_et
_SCHEDULE = {
    "premarket":  (9, 15),
    "morning":    (9, 35),
    "preclose":   (15, 45),
    "postclose":  (16, 15),
}
_MONITOR_INTERVAL_MINS = 30

# Track which named runs have fired today
_ran_today: dict = {}
_scheduler_lock = threading.Lock()

# Pending CC orders — prepared by scheduler, awaiting approval in UI
_pending_actions: list = []
_pending_lock = threading.Lock()

# CC strategy preferences
def _cc_config() -> dict:
    try:
        cfg_path = os.path.join(_BASE_DIR, "data", "config.json")
        with open(cfg_path) as f:
            return _json.load(f).get("cc_strategy", {})
    except Exception:
        return {}

_CC_DEFAULTS = {
    "otm_buffer":  1.05,
    "dte_min":     28,
    "dte_max":     75,
    "prioritize":  "balanced",
    "min_premium": 0.15,
}


def _et_hm() -> tuple[int, int]:
    """Return (hour, minute) in ET right now."""
    try:
        from bot.time_utils import now_et
        t = now_et()
        return t.hour, t.minute
    except Exception:
        import time as _t
        import calendar as _cal
        utc = datetime.now(timezone.utc)
        y = utc.year
        mar = datetime(y, 3, 8)
        dst_start = mar + __import__('datetime').timedelta(days=(6 - mar.weekday()) % 7)
        nov = datetime(y, 11, 1)
        dst_end   = nov + __import__('datetime').timedelta(days=(6 - nov.weekday()) % 7)
        offset    = -4 if dst_start <= utc.replace(tzinfo=None) < dst_end else -5
        et        = utc + __import__('datetime').timedelta(hours=offset)
        return et.hour, et.minute


def _et_date_str() -> str:
    return _et_now_str()[:10]


def _is_weekday() -> bool:
    try:
        from bot.time_utils import market_open_today
        return market_open_today()
    except Exception:
        from datetime import datetime as _dt
        return _dt.now().weekday() < 5


def _load_prev_snapshot() -> list:
    """Return positions from the most recent completed snapshot."""
    try:
        if not os.path.exists(SNAPSHOTS_DIR):
            return []
        files = sorted([f for f in os.listdir(SNAPSHOTS_DIR)
                        if f.endswith(".json") and "_summary" not in f])
        if len(files) < 2:
            return []
        with open(os.path.join(SNAPSHOTS_DIR, files[-2])) as f:
            return _json.load(f).get("positions", [])
    except Exception:
        return []


def _prepare_cc_order(ticker: str, cost_basis: float,
                       shares: int, current_price: float) -> dict | None:
    """
    Prepare a covered call order after assignment.
    Tries Ollama first (35s timeout), falls back to deterministic rule.
    Returns a pending-action dict or None.
    """
    import uuid
    cc_cfg     = {**_CC_DEFAULTS, **_cc_config()}
    prioritize = cc_cfg.get("prioritize", "balanced")
    nav        = _session.get("_net_value", 0) or 0
    contracts  = max(1, shares // 100)

    # Determine expiry (28–75 DTE)
    try:
        from bot.time_utils import next_monthly_expiry, days_to_expiry
        expiry = next_monthly_expiry(0)
        dte    = days_to_expiry(expiry)
        if dte < cc_cfg["dte_min"]:
            expiry = next_monthly_expiry(1)
            dte    = days_to_expiry(expiry)
        expiry_str = expiry.isoformat()
    except Exception:
        import datetime
        expiry_str = (datetime.date.today().replace(day=1) +
                      datetime.timedelta(days=45)).isoformat()
        dte        = 45

    min_strike = round(cost_basis * cc_cfg.get("otm_buffer", 1.05), 2)

    # ── Try Ollama (non-blocking) ─────────────────────────────────────
    model_result = None
    try:
        import sys, threading as _thr
        sys.path.insert(0, _BASE_DIR)
        from bot.ollama_brain import OllamaBrain
        brain = OllamaBrain()
        if brain.is_available():
            sc     = _session.get("_screener_cache", {}).get(ticker, {})
            iv_est = float(sc.get("iv_pct", 25) or 25)
            base   = max(min_strike, round(current_price))
            chain  = []
            for s in [base + i * 0.5 for i in range(0, 12)]:
                p = round(iv_est / 500 * s, 2)
                if p >= cc_cfg["min_premium"]:
                    chain.append({"strike": s, "bid": round(p*.9,2),
                                  "ask": round(p*1.1,2), "mid": p,
                                  "delta": round(max(0.05, 0.35-(s-base)*0.04),2),
                                  "open_interest": 200, "expiry": expiry_str})
            box = [None]
            def _ask():
                try:
                    box[0] = brain.select_cc_strike(
                        chain, cost_basis,
                        {"ticker": ticker, "stock_price": current_price,
                         "recent_change_pct": 0, "earnings_date": None,
                         "cost_basis": cost_basis, "prioritize": prioritize}
                    )
                except Exception:
                    pass
            th = _thr.Thread(target=_ask, daemon=True)
            th.start(); th.join(timeout=35)
            if box[0] and float(box[0].get("strike", 0)) >= min_strike:
                model_result = box[0]
                logger.info(f"[CC_PREP] Ollama: {ticker} ${model_result['strike']}C "
                            f"@ ${model_result.get('limit_price')} — "
                            f"{(model_result.get('reasoning') or '')[:60]}")
    except Exception as e:
        logger.debug(f"[CC_PREP] Ollama skip: {e}")

    # ── Deterministic fallback ────────────────────────────────────────
    if model_result:
        strike      = float(model_result["strike"])
        limit_price = float(model_result.get("limit_price", 0))
        source      = "model"
        reasoning   = model_result.get("reasoning", "Ollama recommendation")
    else:
        if prioritize == "upside":
            strike = max(min_strike, round(current_price * 1.07 / 0.5) * 0.5)
        elif prioritize == "premium":
            strike = min_strike
        else:
            strike = max(min_strike, round(current_price * 1.04 / 0.5) * 0.5)
        sc          = _session.get("_screener_cache", {}).get(ticker, {})
        iv_est      = float(sc.get("iv_pct", 25) or 25)
        limit_price = max(cc_cfg["min_premium"], round(iv_est / 500 * strike, 2))
        source      = "deterministic"
        reasoning   = (f"Rule: ${strike} ({prioritize}), "
                       f"est. ${limit_price} cr from IV {iv_est:.0f}%")

    if limit_price < cc_cfg["min_premium"]:
        return None

    return {
        "id":           f"CC_{ticker}_{expiry_str}_{__import__('uuid').uuid4().hex[:6].upper()}",
        "type":         "SELL_CC",
        "ticker":       ticker,
        "strike":       strike,
        "expiry":       expiry_str,
        "contracts":    contracts,
        "limit_price":  limit_price,
        "shares":       shares,
        "cost_basis":   round(cost_basis, 2),
        "current_price":round(current_price, 2),
        "tif":          "DAY",
        "source":       source,
        "reasoning":    reasoning,
        "prioritize":   prioritize,
        "dte":          dte,
        "min_strike":   min_strike,
        "prepared_et":  _et_now_str(),
        "status":       "pending",
    }


def _detect_and_queue_cc(current_positions: list, prev_positions: list):
    """Diff positions, queue CC orders for newly assigned or unhedged stocks."""
    prev_stocks = {p["ticker"]: p for p in prev_positions if p.get("type") == "STOCK"}
    curr_stocks = {p["ticker"]: p for p in current_positions if p.get("type") == "STOCK"}
    open_cc     = {p["ticker"] for p in current_positions if p.get("type") == "CC"}

    for ticker, pos in curr_stocks.items():
        shares = int(pos.get("contracts", 0) or 0)
        if shares < 100:
            continue
        if ticker in open_cc:
            continue   # CC already open
        with _pending_lock:
            already = any(a["ticker"] == ticker and a["type"] == "SELL_CC"
                          and a["status"] == "pending" for a in _pending_actions)
        if already:
            continue

        prev       = prev_stocks.get(ticker)
        prev_shares= int((prev or {}).get("contracts", 0) or 0)
        is_new     = (prev is None) or (shares > prev_shares)

        cost       = float(pos.get("cost", 0) or 0)
        current    = float(pos.get("current", cost) or cost)
        label      = "Assignment detected" if is_new else "Unhedged stock"
        logger.info(f"[CC_PREP] {label}: {ticker} {shares}sh cost ${cost} now ${current}")

        action = _prepare_cc_order(ticker, cost, shares, current)
        if action:
            if not is_new:
                action["reasoning"] = f"Unhedged position. {action['reasoning']}"
            with _pending_lock:
                _pending_actions.append(action)
            logger.info(f"[CC_PREP] Queued: {ticker} ${action['strike']}C "
                        f"{action['expiry']} ×{action['contracts']}c "
                        f"@ ${action['limit_price']} [{action['source']}]")
            _append_trade_log({
                "ts": _et_now_str(), "event": "CC_ORDER_PREPARED",
                "ticker": ticker, "action": action,
            })


def _do_poll(trigger: str, run_screener: bool = False):
    """Execute one poll cycle: fetch positions + account, optionally screener, persist."""
    if not _session.get("connected"):
        return
    if _session.get("_token_expired"):
        logger.info(f"[SCHEDULER] {trigger} skipped — token expired, awaiting re-auth")
        return

    logger.info(f"[SCHEDULER] firing {trigger} poll")
    _session["_last_trigger"] = trigger
    _session["_last_poll_et"] = _et_now_str()
    _session["_polls_today"]  = _session.get("_polls_today", 0) + 1

    # Calculate next poll time for UI display
    h, m   = _et_hm()
    nm     = (m + _MONITOR_INTERVAL_MINS) % 60
    nh     = h + (m + _MONITOR_INTERVAL_MINS) // 60
    _session["_next_poll_et"] = f"{nh:02d}:{nm:02d} ET"

    positions = []
    account   = {}

    # --- Fetch positions ---
    try:
        import pyetrade
        api = pyetrade.ETradeAccounts(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        raw = api.get_account_portfolio(
            _session["account_id"], resp_format="json", totals_required=True,
        )
        pr        = raw.get("PortfolioResponse", raw)
        acct_port = pr.get("AccountPortfolio", [])
        if isinstance(acct_port, dict):
            acct_port = [acct_port]

        for acct in acct_port:
            raw_pos = acct.get("Position", [])
            if isinstance(raw_pos, dict):
                raw_pos = [raw_pos]
            for p in raw_pos:
                product  = p.get("Product", {})
                quick    = p.get("Quick",   {}) or {}
                sec_type = product.get("securityType", "EQ")
                ticker   = product.get("symbol", "???")
                qty_raw  = float(p.get("quantity", 0) or 0)
                cost     = float(p.get("costPerShare", p.get("pricePaid", 0)) or 0)
                current  = float(quick.get("lastTrade", cost) or cost)
                pnl      = float(p.get("totalGain", p.get("daysGain", 0)) or 0)
                pnl_pct  = float(p.get("totalGainPct", 0) or 0)

                if sec_type == "OPTN":
                    call_put  = product.get("callPut", "")
                    pos_type  = "CSP" if call_put == "PUT" else "CC"
                    strike    = float(product.get("strikePrice", 0) or 0)
                    ey = int(product.get("expiryYear",  0) or 0)
                    em = int(product.get("expiryMonth", 0) or 0)
                    ed = int(product.get("expiryDay",   0) or 0)
                    expiry = (f"{ey}-{str(em).zfill(2)}-{str(ed).zfill(2)}"
                              if ey and em and ed else "")
                    contracts = int(abs(qty_raw)) or 1
                else:
                    pos_type  = "STOCK"
                    strike    = 0
                    expiry    = ""
                    contracts = int(abs(qty_raw))

                positions.append({
                    "ticker":    ticker,
                    "type":      pos_type,
                    "strike":    strike,
                    "expiry":    expiry,
                    "contracts": contracts,
                    "cost":      round(cost, 2),
                    "current":   round(current, 2),
                    "pnl":       round(pnl, 2),
                    "pnl_pct":   round(pnl_pct, 1),
                    "action":          _classify_position(
                                           p, qty_raw,
                                           cost    = cost,
                                           current = current,
                                           expiry  = expiry,
                                       ),
                    "full_contracts":   contracts // 100 if pos_type == "STOCK" else contracts,
                    "leftover_shares":  contracts % 100  if pos_type == "STOCK" else 0,
                    "sector":           _session.get("_sector_map",{}).get(ticker,""),
                })

        _session["_last_positions"] = positions
        _session["_token_expired"]  = False

        # Assignment / unhedged stock detection
        try:
            prev = _load_prev_snapshot()
            if prev or positions:
                _detect_and_queue_cc(positions, prev)
        except Exception as _ce:
            logger.debug(f"[CC_PREP] detect error: {_ce}")

    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err:
            _session["_token_expired"] = True
            logger.error(f"[SCHEDULER] {trigger} — 401 token expired, polling paused")
            return
        logger.error(f"[SCHEDULER] positions fetch error: {e}")

    # --- Fetch account ---
    try:
        import pyetrade
        api = pyetrade.ETradeAccounts(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        raw = api.get_account_balance(_session["account_id"], resp_format="json")
        br  = raw.get("BalanceResponse", raw)
        computed = br.get("Computed", {})
        rtv      = computed.get("RealTimeValues", {})
        account  = {
            "net_value":    float(rtv.get("totalAccountValue") or
                                  computed.get("accountBalance") or 0),
            "buying_power": float(computed.get("marginBuyingPower") or
                                  computed.get("cashBuyingPower") or 0),
            "cash_balance": float(computed.get("cashBalance") or 0),
        }
        _session["_net_value"]    = account["net_value"]
        _session["_last_account"] = account
    except Exception as e:
        logger.error(f"[SCHEDULER] account fetch error: {e}")
        account = _session.get("_last_account", {})

    # --- Optional screener refresh ---
    if run_screener:
        try:
            from wheel_screener import WheelScreener
            nav = account.get("net_value", 0) or 0
            result = WheelScreener(account_nav=nav, min_fisher=6, min_wheel_grade="C").run()
            _annotate_candidates(result)
            _session["_screener_cache"] = {"candidates": result, "by_ticker": {c["ticker"]: c for c in result}}
            logger.info(f"[SCHEDULER] screener refreshed — {len(result)} candidates")
        except Exception as e:
            logger.warning(f"[SCHEDULER] screener refresh failed: {e}")

    # --- Persist ---
    _persist_poll(trigger, positions, account)

    # --- Sprint 4b: wheel_bot autonomous monitoring cycle ─────────────────
    try:
        from bot.wheel_bot import run_cycle as _wb_run
        wb_summary = _wb_run(_session)
        n_q = len(wb_summary.get("queued",[])) + len(wb_summary.get("entries",[]))
        n_e = len(wb_summary.get("exits",[]))
        if n_q or n_e:
            logger.info(f"[SCHEDULER] wheel_bot — exits:{n_e} queued:{n_q}")
        if wb_summary.get("errors"):
            for err in wb_summary["errors"]:
                logger.warning(f"[SCHEDULER] wheel_bot error: {err}")
    except Exception as _wbe:
        logger.warning(f"[SCHEDULER] wheel_bot exception: {_wbe}")

    # --- Post-close daily summary ---
    if trigger == "postclose":
        _write_daily_summary()
        _session["_polls_today"] = 0   # reset counter for next day


def _scheduler_loop():
    """Background thread — checks ET time every 60s and fires polls on schedule."""
    logger.info("[SCHEDULER] thread started")
    _time.sleep(5)   # let Flask finish starting up

    while True:
        try:
            if not _is_weekday():
                _time.sleep(60)
                continue

            today = _et_date_str()
            h, m  = _et_hm()

            with _scheduler_lock:
                # Reset ran_today at midnight
                if _ran_today.get("_date") != today:
                    _ran_today.clear()
                    _ran_today["_date"] = today
                    logger.info(f"[SCHEDULER] new trading day: {today}")

                # Named runs
                for name, (sh, sm) in _SCHEDULE.items():
                    key = f"{name}_{today}"
                    if h == sh and m == sm and key not in _ran_today:
                        _ran_today[key] = True
                        run_scr = name in ("morning", "preclose", "postclose")
                        threading.Thread(
                            target=_do_poll,
                            args=(name, run_scr),
                            daemon=True
                        ).start()

                # 30-minute monitor runs during market hours (9:30–16:00)
                if 9 <= h < 16 and m in (0, 30):
                    # Skip if a named run just fired at this exact minute
                    named_this_min = any(
                        sh == h and sm == m
                        for sh, sm in _SCHEDULE.values()
                    )
                    key = f"monitor_{today}_{h:02d}{m:02d}"
                    if not named_this_min and key not in _ran_today:
                        _ran_today[key] = True
                        threading.Thread(
                            target=_do_poll,
                            args=("monitor", False),
                            daemon=True
                        ).start()

        except Exception as e:
            logger.error(f"[SCHEDULER] loop error: {e}")

        _time.sleep(60)


# Start scheduler thread on import (not just __main__)
_scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="ETradeScheduler")
_scheduler_thread.start()


# ---------------------------------------------------------------------------
# Single-ticker quote for custom CSP card
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cancel order
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pending CC actions — review and approve/reject from UI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Log export
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Option chain fetch — used by roll panel to show available strikes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Roll execution — BTC current + STO new strike/expiry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backtest endpoints
# ---------------------------------------------------------------------------

# Shared state for background backtest job
_backtest_state = {
    "running":      False,
    "progress":     0,
    "log":          [],
    "result":       None,
    "ran_at":       None,
    "error":        None,
}

class _LogCapture:
    """Redirect engine print() output to _backtest_state["log"]."""
    def __init__(self, orig):
        self._orig = orig
    def write(self, msg):
        self._orig.write(msg)
        stripped = msg.strip()
        if stripped:
            _backtest_state["log"].append(stripped)
    def flush(self):
        self._orig.flush()


# ---------------------------------------------------------------------------
# Entrypoint — must be last so all routes are registered before app.run()
# ---------------------------------------------------------------------------

