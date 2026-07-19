"""
backtest/run_backtest.py — CLI entry point
==========================================
Drop this into your etradebot/ root and run:

    python backtest/run_backtest.py

Options
-------
--start DATE        First date of simulation  (default: 1 year ago)
--end   DATE        Last date  (default: yesterday)
--capital FLOAT     Starting cash             (default: 10000)
--tickers T1 T2 …  Override universe         (default: data/universe.json)
--max-pos INT       Max concurrent positions  (default: 10)
--no-report         Skip HTML report          (default: generates report)
--save-state        Write wheel_state.json for live bot continuation
--report-path PATH  Output HTML path          (default: backtest/report.html)
--verbose           Show DEBUG logging

Examples
--------
# Full universe, last year, $10k
python backtest/run_backtest.py

# Specific tickers, custom capital
python backtest/run_backtest.py --tickers KO BAC USB CSCO --capital 25000

# Custom date range + save wheel state for live continuation
python backtest/run_backtest.py --start 2024-01-01 --end 2025-06-13 --save-state

# Verbose debug output
python backtest/run_backtest.py --tickers SOFI --verbose
"""

import sys
import os
import argparse
import datetime
import logging

# ── Path setup ────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Dependency check ──────────────────────────────────────────────────────
def _check_deps() -> None:
    missing = []
    for pkg in ("yfinance", "scipy", "numpy", "pandas"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"\n  Missing packages: {', '.join(missing)}")
        print(f"  Run:  pip install {' '.join(missing)}\n")
        sys.exit(1)

_check_deps()

from backtest.engine import BacktestEngine   # noqa: E402  (after path setup)


# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> None:
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    one_year_ago = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()

    p = argparse.ArgumentParser(
        description="ETradeBot Wheel Strategy Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--start",       default=one_year_ago, metavar="DATE",
                   help=f"Start date YYYY-MM-DD (default: {one_year_ago})")
    p.add_argument("--end",         default=yesterday,    metavar="DATE",
                   help=f"End date YYYY-MM-DD   (default: {yesterday})")
    p.add_argument("--capital",     type=float, default=10_000.0,
                   help="Starting capital (default: 10000)")
    p.add_argument("--tickers",     nargs="*", default=None, metavar="T",
                   help="Tickers to simulate (default: data/universe.json)")
    p.add_argument("--max-pos",     type=int,   default=10,
                   help="Max concurrent positions (default: 10)")
    p.add_argument("--no-report",   action="store_true",
                   help="Skip HTML report generation")
    p.add_argument("--save-state",  action="store_true",
                   help="Write wheel_state.json so live bot continues from backtest end")
    p.add_argument("--report-path", default=os.path.join(_HERE, "report.html"),
                   metavar="PATH", help="HTML report output path")
    p.add_argument("--verbose",     action="store_true",
                   help="Enable DEBUG logging")
    args = p.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Run
    engine = BacktestEngine(
        start_date       = args.start,
        end_date         = args.end,
        capital          = args.capital,
        tickers          = args.tickers,
        max_positions    = args.max_pos,
    )
    results = engine.run()

    # Print summary
    results.print_summary()

    # HTML report
    if not args.no_report:
        report_path = args.report_path
        results.export_html(report_path)
        # Try to open in browser
        try:
            import webbrowser
            webbrowser.open(f"file://{os.path.abspath(report_path)}")
        except Exception:
            pass

    # Save wheel state for live bot continuation
    if args.save_state:
        _save_wheel_state(results)

    return results


def _save_wheel_state(results) -> None:
    """
    Write data/wheel_state.json populated with the positions still open
    at the end of the backtest. The live WheelEngine loads this on startup
    and continues from exactly where the simulation left off.
    """
    import json
    import datetime as _dt

    L        = results.ledger
    open_pos = L.open_positions_summary()

    if not open_pos:
        print("  No open positions to carry forward — wheel_state.json not written.")
        return

    # Build the wheel state structure expected by WheelEngine
    wheels = {}
    for p in open_pos:
        ticker = p["ticker"]
        wheels[ticker] = {
            "ticker":          ticker,
            "phase":           p["phase"],
            "csp_strike":      p["csp_strike"],
            "csp_expiry":      p["csp_expiry"],
            "csp_contracts":   p["contracts"],
            "csp_premium":     p["csp_premium"],
            "btc_limit":       round(p["csp_premium"] * 0.50, 2),
            "csp_initial_delta": 0.28,   # assumed at entry
            "cc_strike":       p.get("cc_strike") or None,
            "cc_expiry":       p.get("cc_expiry") or None,
            "cc_contracts":    p["contracts"],
            "cc_premium":      0.0,
            "shares_held":     p["contracts"] * 100 if p.get("assigned") else 0,
            "cost_basis":      p["csp_strike"],   # conservative; no CC premium yet
            "total_premium":   p["csp_premium"] * p["contracts"] * 100,
            "assignment_date": None,
            "cycle_start_et":  str(p["since"]),
            "cycle_end_et":    None,
            "cycle_pnl":       None,
            "roll_count":      0,
            "roll_credits":    [],
            "csp_order_id":    "BACKTEST",
            "cc_order_id":     None,
            "last_updated":    str(_dt.datetime.now()),
        }

    state = {
        "_description": (
            "ETradeBot wheel state — initialised from backtest. "
            f"Backtest window: {results.start} → {results.end}."
        ),
        "wheels":       wheels,
        "idle_capital": {
            "amount":          0,
            "since_et":        None,
            "pending_reinvest": None,
        },
        "ran_today":   {},
        "last_run":    None,
        "last_run_mode": "backtest_init",
        "cycle_pnl":   {},
        "total_premium_collected": L.total_premium_collected(),
        "total_cycles_completed":  len(L.closed_cycles()),
    }

    path = os.path.join(_ROOT, "data", "wheel_state.json")
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)

    print(f"\n  ✓ wheel_state.json written → {path}")
    print(f"    {len(wheels)} position{'s' if len(wheels) != 1 else ''} carried forward:")
    for ticker, w in wheels.items():
        print(f"    {ticker:<6}  {w['phase']:<12}  "
              f"${w['csp_strike']} CSP {w['csp_expiry']}")
    print("\n  Start the live bot — it will continue from these positions.\n")


if __name__ == "__main__":
    main()
