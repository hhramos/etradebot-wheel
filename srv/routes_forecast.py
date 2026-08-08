"""
routes_forecast.py — /forecast endpoint
=========================================
Models the wheel cycle forward 6 months using real positions.

Exit rule (mirrors bot/trade_rules.py):
  Each position exits at the EARLIER of:
    1. 50% premium decay (BTC at 50% of entry premium)
    2. 21 DTE — if position is in profit at that date
  Net income per close = entry_premium × 50% × 100 × contracts
  (The 50% holds for both exits: at 21 DTE theta has typically consumed ~50%
   of a 30-45 DTE option's time value.)

Months 2-6: freed collateral reinvests into top screener picks,
  same 8%-NAV / 10-contract caps as the live bot.

Running account value = NAV + cumulative net income each month.
"""
import datetime
from srv.core import _session, app, jsonify, logger

MAX_POSITION_PCT = 0.08
MAX_CONTRACTS    = 10
DTE_EXIT         = 21       # exit threshold (days before expiry)
PROFIT_CAPTURE   = 0.50     # fraction of premium kept at either exit trigger


def _next_monthly_expiry(offset: int) -> datetime.date:
    """3rd Friday of the month `offset` months from today."""
    try:
        from bot.time_utils import next_monthly_expiry
        return next_monthly_expiry(offset)
    except Exception:
        today = datetime.date.today()
        year, month = today.year, today.month + offset
        while month > 12:
            month -= 12
            year  += 1
        d = datetime.date(year, month, 1)
        fridays = 0
        while True:
            if d.weekday() == 4:
                fridays += 1
                if fridays == 3:
                    return d
            d += datetime.timedelta(days=1)


def _max_contracts(strike: float, capital: float, nav: float) -> int:
    if strike <= 0 or capital <= 0:
        return 0
    col1   = strike * 100
    by_cap = int(capital / col1)
    by_pos = int((nav * MAX_POSITION_PCT) / col1) if nav > 0 else by_cap
    return min(by_cap, by_pos, MAX_CONTRACTS)


def _premium_est(candidate: dict) -> float:
    """Per-share premium estimate from screener data."""
    p = float(candidate.get("premium_est") or 0)
    if p > 0:
        return p
    roc    = float(candidate.get("roc_est") or 0)
    strike = float(candidate.get("csp_strike") or candidate.get("price") or 0)
    if roc > 0 and strike > 0:
        return round(roc / 100 * strike, 2)
    iv = float(candidate.get("iv_pct") or 20)
    if strike > 0:
        return round(iv / 500 * strike, 2)
    return 0.0


def _exit_month_index(exp_date: datetime.date, expiry_dates: list) -> int:
    """
    Given a position's expiry, determine which calendar month bucket it exits into.
    Exit at 21 DTE means the position closes ~21 days before expiry.
    """
    exit_date = exp_date - datetime.timedelta(days=DTE_EXIT)
    today     = datetime.date.today()
    # If already past 21 DTE or BTC-ready, exits this month (index 0)
    if exit_date <= today:
        return 0
    for mi, ed in enumerate(expiry_dates):
        if exit_date <= ed:
            return mi
    return len(expiry_dates) - 1


@app.route("/forecast", methods=["GET"])
def forecast():
    today     = datetime.date.today()
    positions = _session.get("_last_positions", [])
    account   = _session.get("_last_account", {})
    nav       = float(account.get("net_value") or _session.get("_net_value") or 0)

    # Screener candidates
    sc_raw = _session.get("_screener_cache", {})
    candidates = list(sc_raw.values()) if isinstance(sc_raw, dict) else list(sc_raw or [])
    candidates = [c for c in candidates if isinstance(c, dict)]
    if nav > 0:
        candidates = [
            c for c in candidates
            if float(c.get("csp_strike") or c.get("price") or 0) * 100 <= nav * MAX_POSITION_PCT
        ]
    candidates.sort(key=lambda c: float(c.get("wheel_score") or 0), reverse=True)

    expiry_dates = [_next_monthly_expiry(i) for i in range(6)]

    # Per-month buckets
    freed_collateral = [0.0] * 6   # collateral available to reinvest in month i
    net_income       = [0.0] * 6   # net premium profit collected in month i

    # ── Process current positions ─────────────────────────────────────────
    m0_items = []
    for p in positions:
        ptype     = p.get("type", "")
        ticker    = p.get("ticker", "")
        strike    = float(p.get("strike") or 0)
        contracts = int(p.get("contracts") or 0)
        expiry_s  = p.get("expiry", "")
        cost      = float(p.get("cost") or 0)        # entry premium per share
        pnl_pct   = float(p.get("pnl_pct") or 0)

        if ptype == "STOCK":
            m0_items.append({
                "type": "stock", "ticker": ticker,
                "detail": f"{contracts}sh @ ${p.get('cost','?')}",
                "right": "awaiting CC", "source": "live",
                "income": 0,
            })
            continue

        if ptype not in ("CSP", "CC"):
            continue

        try:
            exp_date = datetime.date.fromisoformat(expiry_s[:10])
        except Exception:
            exp_date = expiry_dates[0]

        dte       = (exp_date - today).days
        label     = "put" if ptype == "CSP" else "call"
        collateral = strike * 100 * contracts

        # Determine exit trigger
        btc_ready = pnl_pct >= 45
        at_21dte  = dte <= DTE_EXIT
        if btc_ready:
            exit_trigger = "50% profit ✓ BTC ready"
            exit_mi = 0
        elif at_21dte:
            exit_trigger = f"21 DTE exit ({dte}d left)"
            exit_mi = 0
        else:
            # Estimate: exit at 21 DTE date
            exit_mi = _exit_month_index(exp_date, expiry_dates)
            days_to_exit = (exp_date - datetime.timedelta(days=DTE_EXIT) - today).days
            exit_trigger = f"21 DTE exit in ~{days_to_exit}d"

        # Net income = 50% of entry premium × contracts × 100
        pos_income = round(cost * PROFIT_CAPTURE * 100 * contracts, 2) if cost > 0 else 0
        net_income[exit_mi]       += pos_income
        freed_collateral[exit_mi] += collateral

        # Show HOLD line
        m0_items.append({
            "type": "hold", "ticker": ticker,
            "detail": f"${strike} {label} ×{contracts}c  entry ${cost}",
            "right": f"exp {expiry_s}",
            "source": "live", "income": 0,
        })
        # Show exit target
        btc_price = round(cost * 0.50, 2) if cost > 0 else None
        btc_detail = "BTC ready — place market order" if btc_ready or at_21dte else f"GTC @ ${btc_price}"
        income_note = f"  +${pos_income:,.0f} net" if pos_income else ""
        m0_items.append({
            "type": "btc", "ticker": ticker,
            "detail": btc_detail + income_note,
            "right": exit_trigger,
            "source": "live",
            "income": pos_income,
            "exit_month": exit_mi,
        })

    # Running NAV projection — starts at current NAV
    running_nav = nav

    months = []
    months.append({
        "label":            expiry_dates[0].strftime("%b %Y"),
        "expiry_date":      expiry_dates[0].isoformat(),
        "is_current":       True,
        "net_income":       round(net_income[0], 0),
        "running_nav":      round(running_nav + net_income[0], 0),
        "items":            m0_items,
    })
    running_nav += net_income[0]

    # ── Months 1-5: reinvest freed collateral ─────────────────────────────
    for i in range(1, 6):
        items    = []
        capital  = freed_collateral[i - 1]   # what freed last month
        running_nav_start = running_nav       # NAV at start of this month

        if capital > 300 and candidates:
            remaining    = capital
            used_tickers = set()
            for cand in candidates:
                if remaining < 300:
                    break
                t      = cand.get("ticker", "")
                if t in used_tickers:
                    continue
                strike = float(cand.get("csp_strike") or cand.get("price") or 0)
                if strike <= 0:
                    continue
                n = _max_contracts(strike, remaining, running_nav_start)
                if n == 0:
                    continue
                prem     = _premium_est(cand)
                exp_str  = expiry_dates[i].strftime("%b %-d")
                col_used = strike * 100 * n

                # This position exits at 21 DTE in month i+1 (or month i if short cycle)
                exit_mi_proj = min(i + 1, 5)
                pos_income   = round(prem * PROFIT_CAPTURE * 100 * n, 2) if prem else 0
                net_income[exit_mi_proj]       += pos_income
                freed_collateral[exit_mi_proj] += col_used

                btc_price = round(prem * 0.50, 2) if prem else None
                sto_detail = f"${strike} put ×{n}c  @ ${prem:.2f} est." if prem else f"${strike} put ×{n}c"
                items.append({
                    "type": "sto", "ticker": t,
                    "detail": sto_detail,
                    "right": exp_str,
                    "source": "projected", "income": 0,
                })
                if prem:
                    items.append({
                        "type": "btc", "ticker": t,
                        "detail": f"GTC @ ${btc_price}  +${pos_income:,.0f} net",
                        "right": "21 DTE / 50% target",
                        "source": "projected",
                        "income": pos_income,
                        "exit_month": exit_mi_proj,
                    })

                remaining -= col_used
                used_tickers.add(t)
                if len(used_tickers) >= 4:
                    break

        elif not candidates and capital > 0:
            items.append({
                "type": "sto", "ticker": "—",
                "detail": f"${capital:,.0f} ready — run screener to project orders",
                "right": expiry_dates[i].strftime("%b %-d"),
                "source": "estimated", "income": 0,
            })

        month_net = round(net_income[i], 0)
        running_nav += month_net
        months.append({
            "label":       expiry_dates[i].strftime("%b %Y"),
            "expiry_date": expiry_dates[i].isoformat(),
            "is_current":  False,
            "net_income":  month_net,
            "running_nav": round(running_nav, 0),
            "items":       items,
        })

    total_income = round(sum(net_income), 0)

    return jsonify({
        "months":          months,
        "as_of":           datetime.datetime.now().isoformat(timespec="seconds"),
        "positions_count": len([p for p in positions if p.get("type") in ("CSP", "CC", "STOCK")]),
        "nav":             nav,
        "total_income":    total_income,
        "data_source":     "live_positions" if positions else "estimated",
    })
