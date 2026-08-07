"""
routes_forecast.py — /forecast endpoint
=========================================
Returns a 6-month forward calendar built from live positions and screener data.

Month 0: current open positions (HOLD + GTC BTC targets).
Months 1-5: projected reinvestment cycles using freed collateral + screener.
"""
import datetime
from srv.core import _session, app, jsonify, logger

MAX_POSITION_PCT   = 0.08
MAX_CONTRACTS      = 10


def _next_monthly_expiry(offset: int) -> datetime.date:
    """3rd Friday of the month `offset` months from today."""
    try:
        from bot.time_utils import next_monthly_expiry
        return next_monthly_expiry(offset)
    except Exception:
        # Fallback: compute inline
        today = datetime.date.today()
        year  = today.year
        month = today.month + offset
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
    col1 = strike * 100
    by_cap = int(capital / col1)
    by_pos = int((nav * MAX_POSITION_PCT) / col1)
    return min(by_cap, by_pos, MAX_CONTRACTS)


def _premium_est(candidate: dict) -> float:
    """Best-effort premium estimate from screener data (per contract)."""
    p = float(candidate.get("premium_est") or 0)
    if p > 0:
        return p
    roc = float(candidate.get("roc_est") or 0)
    strike = float(candidate.get("csp_strike") or candidate.get("price") or 0)
    if roc > 0 and strike > 0:
        return round(roc / 100 * strike, 2)
    iv = float(candidate.get("iv_pct") or 20)
    if strike > 0:
        return round(iv / 500 * strike, 2)
    return 0.0


@app.route("/forecast", methods=["GET"])
def forecast():
    positions = _session.get("_last_positions", [])
    account   = _session.get("_last_account", {})
    nav       = float(account.get("net_value") or _session.get("_net_value") or 0)
    buying_power = float(account.get("buying_power") or 0)

    # Screener candidates — dict keyed by ticker, or list
    sc_raw = _session.get("_screener_cache", {})
    if isinstance(sc_raw, dict):
        candidates = list(sc_raw.values())
    else:
        candidates = list(sc_raw) if sc_raw else []
    # Filter to eligible candidates (collateral ≤ 8% of NAV when NAV known)
    if nav > 0:
        candidates = [
            c for c in candidates
            if isinstance(c, dict)
            and float(c.get("csp_strike") or c.get("price") or 0) * 100 <= nav * MAX_POSITION_PCT
        ]
    else:
        candidates = [c for c in candidates if isinstance(c, dict)]
    candidates.sort(key=lambda c: float(c.get("wheel_score") or 0), reverse=True)

    # Build month expiry dates
    expiry_dates = [_next_monthly_expiry(i) for i in range(6)]

    months = []
    # Track projected collateral freed per month for reinvestment
    freed_by_month = {i: 0.0 for i in range(6)}

    # ── Month 0: live positions ────────────────────────────────────────────
    m0_items = []
    for p in positions:
        if p.get("type") not in ("CSP", "CC", "STOCK"):
            continue
        ptype    = p.get("type", "")
        ticker   = p.get("ticker", "")
        strike   = float(p.get("strike") or 0)
        contracts = int(p.get("contracts") or 0)
        expiry   = p.get("expiry", "")
        cost     = float(p.get("cost") or 0)
        pnl_pct  = float(p.get("pnl_pct") or 0)

        if ptype == "STOCK":
            m0_items.append({
                "type": "stock",
                "ticker": ticker,
                "detail": f"{contracts}sh @ ${p.get('cost','?')}",
                "right": "assigned",
                "source": "live",
            })
            continue

        label = "put" if ptype == "CSP" else "call"
        m0_items.append({
            "type": "hold",
            "ticker": ticker,
            "detail": f"${strike} {label} ×{contracts}c",
            "right": f"exp {expiry}",
            "source": "live",
        })

        # GTC BTC target
        if cost > 0:
            btc_price = round(cost * 0.50, 2)
            btc_label = "BTC ready" if pnl_pct >= 45 else f"GTC @ ${btc_price}"
            m0_items.append({
                "type": "btc",
                "ticker": ticker,
                "detail": btc_label,
                "right": "50% target",
                "source": "live",
            })

        # Freed capital — estimate which month this expires into
        collateral = strike * 100 * contracts
        try:
            exp_date = datetime.date.fromisoformat(expiry[:10])
        except Exception:
            exp_date = expiry_dates[0]
        # Find which projected month is closest after expiry
        for mi in range(6):
            if exp_date <= expiry_dates[mi]:
                freed_by_month[mi] += collateral
                break
        else:
            freed_by_month[5] += collateral

    m0_income = sum(
        float(p.get("cost", 0)) * 100 * int(p.get("contracts") or 0)
        for p in positions
        if p.get("type") in ("CSP", "CC")
    )
    months.append({
        "label": expiry_dates[0].strftime("%b %Y"),
        "expiry_date": expiry_dates[0].isoformat(),
        "is_current": True,
        "estimated_income": round(m0_income, 0),
        "items": m0_items,
    })

    # ── Months 1-5: projected reinvestment ───────────────────────────────
    pick_idx = 0
    for i in range(1, 6):
        items = []
        month_income = 0.0
        capital_this_month = freed_by_month.get(i - 1, 0)

        if capital_this_month > 300 and candidates:
            # Assign top screener picks to fill freed capital
            remaining = capital_this_month
            used_tickers = set()
            for cand in candidates:
                if remaining < 300:
                    break
                ticker = cand.get("ticker", "")
                if ticker in used_tickers:
                    continue
                strike = float(cand.get("csp_strike") or cand.get("price") or 0)
                if strike <= 0:
                    continue
                n = _max_contracts(strike, remaining, nav) if nav > 0 else max(1, int(remaining / (strike * 100)))
                if n == 0:
                    continue
                prem = _premium_est(cand)
                exp_str = expiry_dates[i].strftime("%b %-d")
                items.append({
                    "type": "sto",
                    "ticker": ticker,
                    "detail": f"${strike} put ×{n}c" + (f" @ ${prem:.2f} est." if prem else ""),
                    "right": exp_str,
                    "source": "projected",
                })
                if prem:
                    items.append({
                        "type": "btc",
                        "ticker": ticker,
                        "detail": f"GTC @ ${round(prem*0.50,2):.2f}",
                        "right": "50% target",
                        "source": "projected",
                    })
                month_income += prem * 100 * n
                remaining -= strike * 100 * n
                used_tickers.add(ticker)
                freed_by_month[i] += strike * 100 * n  # carries forward next cycle
                if len(used_tickers) >= 4:
                    break
        elif not candidates:
            # No screener data — show generic placeholder
            items.append({
                "type": "sto",
                "ticker": "—",
                "detail": "screener data needed",
                "right": expiry_dates[i].strftime("%b %-d"),
                "source": "estimated",
            })

        months.append({
            "label": expiry_dates[i].strftime("%b %Y"),
            "expiry_date": expiry_dates[i].isoformat(),
            "is_current": False,
            "estimated_income": round(month_income, 0),
            "items": items,
        })

    return jsonify({
        "months": months,
        "as_of": datetime.datetime.now().isoformat(timespec="seconds"),
        "positions_count": len([p for p in positions if p.get("type") in ("CSP", "CC", "STOCK")]),
        "nav": nav,
        "data_source": "live_positions" if positions else "estimated",
    })
