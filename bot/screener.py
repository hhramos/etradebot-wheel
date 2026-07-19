"""
screener.py — Bot Screener (Reinvestment-Aware)
================================================
Wraps wheel_screener.py for use by the autonomous bot.
Adds earnings blackout filtering, delta/DTE pre-checks,
and the reinvestment ranking engine from trade_rules.py.

Usage:
    from bot.screener import BotScreener
    s = BotScreener(nav=6500, buying_power=1800)
    candidates = s.get_candidates()
    ranked     = s.rank_for_reinvestment(freed_capital=520,
                                          original_ticker="SOFI")
"""

import os
import sys
import json
import logging
import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Allow import from parent directory (wheel_screener.py lives there)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

UNIVERSE_FILE = os.path.join(_ROOT, "data", "universe.json")
CONFIG_FILE   = os.path.join(_ROOT, "data", "config.json")


# ── Universe management ───────────────────────────────────────────────────

def load_universe() -> list[str]:
    """
    Load bot trading universe from data/universe.json.
    Falls back to DEFAULT_UNIVERSE from wheel_screener.py if file missing.
    """
    if os.path.exists(UNIVERSE_FILE):
        try:
            with open(UNIVERSE_FILE) as f:
                data = json.load(f)
            tickers = data.get("tickers", [])
            if tickers:
                return tickers
        except Exception as e:
            logger.warning("universe.json load failed: %s", e)

    try:
        from wheel_screener import DEFAULT_UNIVERSE
        return DEFAULT_UNIVERSE
    except ImportError:
        return ["SOFI", "NOK", "F", "SOUN", "PFE", "DOCS", "CCL"]


def load_config() -> dict:
    """Load config.json, return defaults if missing."""
    defaults = {
        "rules": {
            "min_fisher":       7,
            "min_wheel_grade":  "B",
            "earnings_blackout": 7,
        },
        "reinvestment": {
            "min_fisher":       7,
            "min_wheel_grade":  "B",
            "deploy_if_idle_mins": 60,
        },
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            # Deep merge with defaults
            for key, val in defaults.items():
                if key not in data:
                    data[key] = val
                elif isinstance(val, dict):
                    for k, v in val.items():
                        data[key].setdefault(k, v)
            return data
        except Exception as e:
            logger.warning("config.json load failed: %s — using defaults", e)
    return defaults


def add_to_universe(ticker: str) -> bool:
    """Add a ticker to the bot universe. Returns True on success."""
    ticker = ticker.upper().strip()
    os.makedirs(os.path.dirname(UNIVERSE_FILE), exist_ok=True)

    existing = []
    if os.path.exists(UNIVERSE_FILE):
        try:
            with open(UNIVERSE_FILE) as f:
                data = json.load(f)
            existing = data.get("tickers", [])
        except Exception:
            data = {}
    else:
        data = {}

    if ticker in existing:
        logger.info("%s already in universe", ticker)
        return False

    existing.append(ticker)
    data["tickers"]    = existing
    data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with open(UNIVERSE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Added %s to universe (%d tickers total)", ticker, len(existing))
    return True


def remove_from_universe(ticker: str) -> bool:
    """Remove a ticker from the bot universe."""
    ticker = ticker.upper().strip()
    if not os.path.exists(UNIVERSE_FILE):
        return False
    try:
        with open(UNIVERSE_FILE) as f:
            data = json.load(f)
        tickers = data.get("tickers", [])
        if ticker not in tickers:
            return False
        tickers.remove(ticker)
        data["tickers"]    = tickers
        data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(UNIVERSE_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Removed %s from universe", ticker)
        return True
    except Exception as e:
        logger.error("remove_from_universe failed: %s", e)
        return False


# ── Earnings calendar ─────────────────────────────────────────────────────

def get_earnings_date(ticker: str) -> Optional[datetime.date]:
    """
    Fetch next earnings date from yfinance.
    Returns None if unavailable or yfinance not installed.
    """
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        cal  = t.calendar
        if cal is None:
            return None

        # calendar returns a dict or DataFrame depending on yfinance version
        if hasattr(cal, "columns"):
            # DataFrame format
            if "Earnings Date" in cal.columns:
                dates = cal["Earnings Date"]
                if len(dates) > 0:
                    d = dates.iloc[0]
                    if hasattr(d, "date"):
                        return d.date()
                    return d
        elif isinstance(cal, dict):
            earnings = cal.get("Earnings Date", [None])[0]
            if earnings:
                if hasattr(earnings, "date"):
                    return earnings.date()
                try:
                    return datetime.date.fromisoformat(str(earnings)[:10])
                except Exception:
                    pass
    except Exception as e:
        logger.debug("get_earnings_date(%s) failed: %s", ticker, e)
    return None


# ── BotScreener ───────────────────────────────────────────────────────────

class BotScreener:
    """
    Screener for the autonomous bot.
    Wraps WheelScreener from wheel_screener.py and adds:
    - Earnings blackout pre-filter
    - Config-driven quality thresholds
    - Reinvestment ranking via trade_rules.py
    """

    def __init__(self, nav: float = 0, buying_power: float = 0):
        self.nav           = nav
        self.buying_power  = buying_power
        self.config        = load_config()
        self.universe      = load_universe()
        self._candidates   = []   # cache from last run
        self._ran_at       = None

    def _rules_config(self) -> dict:
        return self.config.get("rules", {})

    def get_candidates(self, force: bool = False) -> list[dict]:
        """
        Run the live screener and return filtered candidates.
        Cached for 30 minutes unless force=True.
        """
        cache_stale = (
            self._ran_at is None or
            (datetime.datetime.now(datetime.timezone.utc) - self._ran_at).seconds > 1800
        )
        if not force and not cache_stale and self._candidates:
            return self._candidates

        rc  = self._rules_config()
        nav = self.nav

        try:
            from wheel_screener import WheelScreener
            screener = WheelScreener(
                universe         = self.universe,
                account_nav      = nav,
                min_fisher       = rc.get("min_fisher",       7),
                min_wheel_grade  = rc.get("min_wheel_grade",  "B"),
            )
            raw = screener.run()
        except Exception as e:
            logger.error("WheelScreener.run() failed: %s", e)
            raw = []

        # Post-filter: earnings blackout
        blackout = rc.get("earnings_blackout", 7)
        filtered = []
        for c in raw:
            ticker   = c.get("ticker", "")
            earnings = get_earnings_date(ticker)
            if earnings:
                from bot.trade_rules import is_in_earnings_blackout_date
                diff = abs((earnings - datetime.date.today()).days)
                if diff <= blackout:
                    logger.info("Skipping %s — earnings in %d days (%s)",
                                ticker, diff, earnings)
                    c["earnings_warning"] = str(earnings)
                    c["earnings_days"]    = diff
                    continue
            filtered.append(c)

        self._candidates = filtered
        self._ran_at     = datetime.datetime.now(datetime.timezone.utc)
        logger.info("Screener returned %d candidates (%d filtered for earnings)",
                    len(filtered), len(raw) - len(filtered))
        return filtered

    def rank_for_reinvestment(self,
                               freed_capital: float,
                               original_ticker: str,
                               active_tickers: list[str] = None,
                               idle_minutes: int = 0) -> list[dict]:
        """
        Rank all screener candidates for reinvestment of freed capital.
        Favors lower-priced stocks that fit more contracts within the
        8% NAV cap, while maintaining quality standards.

        Returns ranked list — winner at index 0.
        """
        candidates = self.get_candidates()
        if not candidates:
            logger.warning("No screener candidates available for reinvestment ranking")
            return []

        from bot.trade_rules import rank_reinvestment_candidates
        ranked = rank_reinvestment_candidates(
            candidates      = candidates,
            freed_capital   = freed_capital,
            nav             = self.nav,
            original_ticker = original_ticker,
            active_tickers  = active_tickers or [],
            idle_minutes    = idle_minutes,
        )

        logger.info(
            "Reinvestment ranking: %d candidates scored for $%.0f freed capital (NAV $%.0f)",
            len(ranked), freed_capital, self.nav
        )
        if ranked:
            w = ranked[0]
            logger.info(
                "Winner: %s score=%.3f %dc ~$%,.0f/mo %s",
                w["ticker"], w["score"], w["contracts"],
                w["monthly_est"],
                "(lower-price alt)" if w.get("is_lower_price_alt") else ""
            )
        return ranked

    def get_best_entry(self,
                        freed_capital: float,
                        original_ticker: str,
                        active_tickers: list[str] = None,
                        idle_minutes: int = 0) -> Optional[dict]:
        """
        Return the single best reinvestment candidate, or None if nothing qualifies.
        This is what the bot calls to decide where to deploy freed capital.
        """
        ranked = self.rank_for_reinvestment(
            freed_capital   = freed_capital,
            original_ticker = original_ticker,
            active_tickers  = active_tickers,
            idle_minutes    = idle_minutes,
        )
        return ranked[0] if ranked else None

    def get_cc_strike(self, ticker: str,
                       cost_basis: float,
                       api=None) -> Optional[dict]:
        """
        Find the best covered call strike for an assigned stock.
        Strike must be at or above cost basis.
        Returns {strike, expiry, premium, delta, reason} or None.
        """
        from bot.time_utils import next_monthly_expiry, days_to_expiry
        from bot.trade_rules import (DTE_RANGE, DELTA_RANGE,
                                      MIN_OPEN_INTEREST, MIN_PREMIUM)
        if api is None:
            logger.warning("get_cc_strike: no API client provided")
            return None

        try:
            expiry = next_monthly_expiry(0)
            dte    = days_to_expiry(expiry)
            if dte < DTE_RANGE[0]:
                expiry = next_monthly_expiry(1)
                dte    = days_to_expiry(expiry)

            chain = api.get_option_chain(ticker, expiry, option_type="CALL")
            if not chain:
                return None

            # Filter: strike ≥ cost basis, delta in range, liquidity
            candidates = [
                s for s in chain
                if (s["strike"] >= cost_basis and
                    s["open_interest"] >= MIN_OPEN_INTEREST and
                    s["mid"] >= MIN_PREMIUM and
                    DELTA_RANGE[0] <= s.get("delta", 0.30) <= DELTA_RANGE[1])
            ]
            if not candidates:
                # Relax delta — just need strike ≥ cost basis with any premium
                candidates = [
                    s for s in chain
                    if s["strike"] >= cost_basis and s["mid"] >= MIN_PREMIUM
                ]

            if not candidates:
                return None

            # Pick the highest premium that still meets criteria
            best = max(candidates, key=lambda x: x["mid"])
            return {
                "strike":    best["strike"],
                "expiry":    expiry.isoformat(),
                "premium":   best["mid"],
                "delta":     best.get("delta", 0),
                "bid":       best["bid"],
                "ask":       best["ask"],
                "reason":    (
                    f"Strike ${best['strike']} ≥ cost basis ${cost_basis}, "
                    f"delta {best.get('delta',0):.2f}, "
                    f"premium ${best['mid']:.2f}"
                )
            }
        except Exception as e:
            logger.error("get_cc_strike(%s) failed: %s", ticker, e)
            return None


# ── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    s = BotScreener(nav=6500, buying_power=1800)
    print(f"\nUniverse: {s.universe[:8]}... ({len(s.universe)} tickers)")
    print(f"Config rules: {s._rules_config()}")

    print("\n── Getting candidates (live yfinance) ──")
    try:
        candidates = s.get_candidates()
        print(f"  {len(candidates)} candidates returned")
        for c in candidates[:3]:
            print(f"  {c['ticker']:6} Fisher:{c['fisher_score']} "
                  f"Wheel:{c['wheel_grade']}·{c['wheel_score']} "
                  f"Strike:${c['csp_strike']} ROC:{c.get('roc_est',0):.1f}%")
    except Exception as e:
        print(f"  Screener error (expected if offline): {e}")

    print("\n── Reinvestment ranking (no live data needed) ──")
    mock_candidates = [
        {"ticker":"NOK", "csp_strike":5.0, "fisher_score":8,
         "wheel_grade":"C", "wheel_score":65, "iv_pct":28,
         "premium_est":0.28, "csp_expiry":"2026-07-17"},
        {"ticker":"F",   "csp_strike":10.0,"fisher_score":7,
         "wheel_grade":"B", "wheel_score":74, "iv_pct":24,
         "premium_est":0.46, "csp_expiry":"2026-07-17"},
        {"ticker":"SOFI","csp_strike":15.0,"fisher_score":7,
         "wheel_grade":"B", "wheel_score":72, "iv_pct":35,
         "premium_est":0.57, "csp_expiry":"2026-07-17"},
    ]
    s._candidates = mock_candidates
    ranked = s.rank_for_reinvestment(
        freed_capital   = 520,
        original_ticker = "SOFI",
        active_tickers  = [],
    )
    print(f"  Freed $520, NAV $6,500:")
    for i, r in enumerate(ranked):
        alt = " ← lower-price alt" if r.get("is_lower_price_alt") else ""
        print(f"  #{i+1} {r['ticker']:6} score={r['score']:.3f} "
              f"{r['contracts']}c ~${r['monthly_est']:.0f}/mo{alt}")
