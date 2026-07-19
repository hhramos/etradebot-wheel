"""
wheel_screener.py — Live Wheel Strategy Screener
=================================================
Pulls real-time data from Yahoo Finance (via yfinance) and scores each
candidate for the options wheel strategy.

Scoring model
-------------
  Fisher score   (0–15)  momentum + valuation composite
  Wheel score    (0–100) yield + stability + liquidity + IV
  Wheel grade    A / B / C / D

Install:
    pip install yfinance pandas numpy

Usage (standalone):
    python wheel_screener.py

Usage (from server.py /screener endpoint):
    from wheel_screener import WheelScreener
    candidates = WheelScreener().run()   # returns list[dict]
"""

import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default universe — dividend-paying, optionable, liquid stocks
# Customise this list to add/remove candidates
# ---------------------------------------------------------------------------
DEFAULT_UNIVERSE = [
    # Blue-chip dividend payers — classic wheel candidates
    "KO",   "PEP",  "MCD",  "JNJ",  "PG",
    "VZ",   "T",    "MO",   "PM",   "BAC",
    "WFC",  "USB",  "JPM",  "C",    "GS",
    "PFE",  "ABBV", "MRK",  "BMY",  "AMGN",
    "CSCO", "INTC", "IBM",  "QCOM", "TXN",
    "XOM",  "CVX",  "COP",  "OXY",  "SLB",
    "DUK",  "SO",   "NEE",  "D",    "AEP",
    "WMT",  "TGT",  "COST", "HD",   "LOW",
]

# ---------------------------------------------------------------------------
# Next monthly options expiry (3rd Friday)
# ---------------------------------------------------------------------------
def next_monthly_expiry(offset_months: int = 0) -> str:
    """
    Thin wrapper around the canonical implementation in bot/time_utils.py.
    (Previously a duplicate copy — the past-date bug of Jun 30 had to be
    fixed in both places. Single source of truth now.)
    Returns ISO string, guaranteed future-dated.
    """
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from bot.time_utils import next_monthly_expiry as _canonical
    return _canonical(offset_months).isoformat()


# ---------------------------------------------------------------------------
# Fisher score — composite of valuation + momentum (0–15)
# ---------------------------------------------------------------------------
def fisher_score(info: dict) -> int:
    score = 0

    pe   = info.get("trailingPE")
    fpe  = info.get("forwardPE")
    pb   = info.get("priceToBook")
    beta = info.get("beta")
    dy   = info.get("dividendYield") or 0
    peg  = info.get("pegRatio")
    roe  = info.get("returnOnEquity") or 0
    de   = info.get("debtToEquity")

    # P/E in reasonable range
    if pe and 5 < pe < 25:    score += 2
    elif pe and 25 <= pe < 35: score += 1

    # Forward P/E lower than trailing (earnings growth)
    if pe and fpe and fpe < pe: score += 1

    # P/B < 3
    if pb and pb < 3:  score += 1
    elif pb and pb < 5: score += 0

    # Beta 0.5–1.2 (stable but not boring)
    if beta and 0.5 <= beta <= 1.2: score += 2
    elif beta and beta < 1.5:       score += 1

    # Dividend yield > 1%
    if dy > 0.04:   score += 3
    elif dy > 0.02: score += 2
    elif dy > 0.01: score += 1

    # PEG < 2
    if peg and peg < 1.5: score += 2
    elif peg and peg < 2: score += 1

    # ROE > 15%
    if roe > 0.20:  score += 2
    elif roe > 0.10: score += 1

    # D/E manageable
    if de is not None and de < 100: score += 1

    return min(score, 15)


# ---------------------------------------------------------------------------
# Wheel score (0–100) and grade
# ---------------------------------------------------------------------------
def wheel_score_and_grade(info: dict, iv_pct: float) -> tuple[int, str]:
    score = 0

    dy    = (info.get("dividendYield") or 0) * 100
    beta  = info.get("beta") or 1.0
    mcap  = info.get("marketCap") or 0
    vol   = info.get("averageVolume") or 0
    price = info.get("currentPrice") or info.get("regularMarketPrice") or 0

    # IV (implied volatility) — sweet spot 15–40%
    if 15 <= iv_pct <= 40:   score += 30
    elif 10 <= iv_pct < 15:  score += 20
    elif 40 < iv_pct <= 55:  score += 15
    else:                    score += 5

    # Dividend yield
    if dy >= 4:    score += 20
    elif dy >= 2:  score += 15
    elif dy >= 1:  score += 8

    # Beta stability
    if beta <= 0.8:         score += 15
    elif beta <= 1.1:       score += 12
    elif beta <= 1.3:       score += 8
    else:                   score += 3

    # Market cap (large cap = more stable)
    if mcap >= 100e9:        score += 15
    elif mcap >= 20e9:       score += 10
    elif mcap >= 5e9:        score += 5

    # Price (lower price = smaller collateral per contract)
    if 10 <= price <= 60:    score += 10
    elif 60 < price <= 100:  score += 7
    elif price > 100:        score += 3

    # Volume (liquidity)
    if vol >= 5_000_000:     score += 10
    elif vol >= 1_000_000:   score += 7
    elif vol >= 500_000:     score += 3

    score = min(score, 100)

    if score >= 85:   grade = "A"
    elif score >= 70: grade = "B"
    elif score >= 50: grade = "C"
    else:             grade = "D"

    return score, grade


# ---------------------------------------------------------------------------
# CSP strike selection — OTM put ~5% below current price, rounded to $1
# ---------------------------------------------------------------------------
def select_csp_strike(price: float, otm_pct: float = 0.05) -> float:
    raw = price * (1 - otm_pct)
    # Round to nearest dollar (most stocks) or nearest $0.50 for sub-$20
    if price < 20:
        return round(raw * 2) / 2
    return round(raw)


# ---------------------------------------------------------------------------
# Estimate IV from historical volatility (annualised) if options data
# unavailable — yfinance doesn't always expose IV directly
# ---------------------------------------------------------------------------
def estimate_iv_from_hist_vol(ticker_obj, hist=None) -> float:
    """Annualised 30-day historical vol as IV proxy, in percent.
    Pass a pre-fetched daily DataFrame via `hist` to avoid a network call;
    the last ~63 rows (≈3 months) are used."""
    try:
        if hist is None:
            hist = ticker_obj.history(period="3mo")
        else:
            hist = hist.tail(63)
        if hist.empty:
            return 25.0
        import numpy as np
        returns = hist["Close"].pct_change().dropna()
        hv = float(returns.std() * (252 ** 0.5) * 100)
        # IV typically trades at a 10–30% premium to HV
        return round(min(max(hv * 1.15, 8), 100), 1)
    except Exception:
        return 25.0


# ---------------------------------------------------------------------------
# Main screener class
# ---------------------------------------------------------------------------
class WheelScreener:
    def __init__(
        self,
        universe: list[str] = None,
        account_nav: float = 0,
        max_collateral_pct: float = 0.05,
        min_fisher: int = 6,
        min_wheel_grade: str = "C",
        expiry_offset_months: int = 0,
    ):
        self.universe            = universe or DEFAULT_UNIVERSE
        self.account_nav         = account_nav
        self.max_collateral_pct  = max_collateral_pct
        self.min_fisher          = min_fisher
        self.min_wheel_grade     = min_wheel_grade
        self.expiry              = next_monthly_expiry(expiry_offset_months)
        self._grade_order        = {"A": 0, "B": 1, "C": 2, "D": 3}

    def _fetch_one(self, symbol: str) -> Optional[dict]:
        try:
            import yfinance as yf
            t    = yf.Ticker(symbol)
            info = t.info

            price = float(
                info.get("currentPrice")
                or info.get("regularMarketPrice")
                or info.get("previousClose")
                or 0
            )
            if price <= 0:
                logger.warning(f"{symbol}: no price, skipping")
                return None

            # ONE 1-year history fetch — reused by IV estimate, IV Rank, and SMA
            try:
                _hist_1y = t.history(period="1y")
            except Exception:
                _hist_1y = None
            iv_pct     = estimate_iv_from_hist_vol(t, hist=_hist_1y)
            f_score    = fisher_score(info)
            w_score, w_grade = wheel_score_and_grade(info, iv_pct)

            # Filter early
            if f_score < self.min_fisher:
                return None
            if self._grade_order.get(w_grade, 9) > self._grade_order.get(self.min_wheel_grade, 9):
                return None

            strike       = select_csp_strike(price)
            collateral   = strike * 100
            coll_pct     = round(collateral / self.account_nav * 100, 2) if self.account_nav > 0 else None

            dy           = float(info.get("dividendYield") or 0)
            pe           = info.get("trailingPE")
            premium_est  = round(iv_pct / 500 * strike, 2)   # rough Black-Scholes proxy
            roc_est      = round(premium_est / strike * 100, 2)

            # ── Earnings date ──────────────────────────────────────────
            earnings_date = None
            try:
                cal = t.calendar
                if cal is not None and not cal.empty:
                    ed = cal.get("Earnings Date")
                    if ed is not None and len(ed) > 0:
                        import datetime as _dt2
                        ed_val = ed.iloc[0] if hasattr(ed, 'iloc') else ed[0]
                        if hasattr(ed_val, 'date'):
                            earnings_date = ed_val.date().isoformat()
                        else:
                            earnings_date = str(ed_val)[:10]
            except Exception:
                pass

            # Days until next earnings (None if unknown)
            days_to_earnings = None
            if earnings_date:
                try:
                    import datetime as _dt3
                    ed_obj = _dt3.date.fromisoformat(earnings_date[:10])
                    days_to_earnings = (ed_obj - _dt3.date.today()).days
                except Exception:
                    pass

            # Entry delta estimate (Black-Scholes approximation at 5% OTM)
            import math as _math
            try:
                T_entry = (len([x for x in [28,30,35,38,45] if x > 0][-1:]) or 38) / 365
                T_entry = 38 / 365   # standard entry DTE
                d1 = (_math.log(price / strike) + (0.05 + 0.5*(iv_pct/100)**2)*T_entry)                      / ((iv_pct/100) * _math.sqrt(T_entry))
                # CDF approximation
                def _ncdf(x):
                    return 0.5*(1+_math.erf(x/_math.sqrt(2)))
                entry_delta = round(_ncdf(d1) - 1, 3)   # put delta is negative
            except Exception:
                entry_delta = None

            # Liquidity proxy
            mktcap = (info.get("marketCap") or 0) / 1e9
            is_liquid = iv_pct >= 15 and mktcap >= 2.0

            # ── Feature 1: IV Rank (52-week range) ─────────────────────────
            iv_rank = None
            try:
                hist_1y = _hist_1y if _hist_1y is not None else t.history(period="1y")
                if not hist_1y.empty:
                    import numpy as np
                    rets_1y = hist_1y["Close"].pct_change().dropna()
                    # Compute rolling 30d HV for each day, scale to IV
                    hv_series = rets_1y.rolling(21).std() * (252**0.5) * 100 * 1.15
                    hv_series = hv_series.dropna()
                    if len(hv_series) >= 20:
                        hv_min = float(hv_series.min())
                        hv_max = float(hv_series.max())
                        if hv_max > hv_min:
                            iv_rank = round((iv_pct - hv_min) / (hv_max - hv_min) * 100, 1)
                            iv_rank = max(0.0, min(100.0, iv_rank))
            except Exception:
                iv_rank = None

            # ── Feature 3: SMA trend indicator ─────────────────────────────
            sma_trend = None   # "above_both" | "between" | "below_200" | None
            sma_50 = None
            sma_200 = None
            try:
                hist_sma = _hist_1y if _hist_1y is not None else t.history(period="1y")
                if not hist_sma.empty and len(hist_sma) >= 50:
                    closes = hist_sma["Close"]
                    sma_50  = float(closes.rolling(50).mean().iloc[-1])
                    sma_200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None
                    if sma_200:
                        if price > sma_50 and price > sma_200:
                            sma_trend = "above_both"
                        elif price > sma_200:
                            sma_trend = "between"
                        else:
                            sma_trend = "below_200"
                    else:
                        sma_trend = "above_50" if price > sma_50 else "below_50"
            except Exception:
                sma_trend = None

            # ── Feature 6: Post-earnings IV crush window ────────────────────
            post_earnings_window = False
            try:
                if earnings_date:
                    import datetime as _dt4
                    ed_obj2 = _dt4.date.fromisoformat(earnings_date[:10])
                    days_since = (_dt4.date.today() - ed_obj2).days
                    if 1 <= days_since <= 3:
                        post_earnings_window = True
            except Exception:
                post_earnings_window = False

            return {
                "ticker":               symbol,
                "price":                round(price, 2),
                "pe":                   round(pe, 1) if pe else None,
                "annual_div_pct":       round(dy * 100, 2),
                "iv_pct":               iv_pct,
                "fisher_score":         f_score,
                "wheel_score":          w_score,
                "wheel_grade":          w_grade,
                "csp_strike":           strike,
                "csp_expiry":           self.expiry,
                "csp_collateral":       collateral,
                "wheel_collateral_pct": coll_pct,
                "premium_est":          premium_est,
                "roc_est":              roc_est,
                "pctOfPortfolio":       round(collateral / self.account_nav, 4) if self.account_nav > 0 else 0,
                "beta":                 round(float(info.get("beta") or 1.0), 2),
                "market_cap_b":         round(mktcap, 1),
                "sector":               info.get("sector", ""),
                # New fields
                "earnings_date":        earnings_date,
                "days_to_earnings":     days_to_earnings,
                "entry_delta":          entry_delta,
                "is_liquid":            is_liquid,
                "iv_rank":              iv_rank,
                "sma_trend":            sma_trend,
                "sma_50":               round(sma_50, 2) if sma_50 else None,
                "sma_200":              round(sma_200, 2) if sma_200 else None,
                "post_earnings_window": post_earnings_window,
            }

        except Exception as e:
            logger.warning(f"{symbol}: fetch failed — {e}")
            return None

    def run(self) -> list[dict]:
        """
        Fetch all tickers and return ranked candidates.
        Runs synchronously — for large universes consider threading.
        """
        import concurrent.futures

        results = []
        logger.info(f"Screening {len(self.universe)} tickers…")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._fetch_one, sym): sym for sym in self.universe}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)

        # Sort: grade → wheel_score desc
        results.sort(key=lambda x: (
            self._grade_order.get(x["wheel_grade"], 9),
            -x["wheel_score"],
            -x["fisher_score"],
        ))

        logger.info(f"Screener returned {len(results)} candidates")
        return results


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    screener = WheelScreener(
        universe=["KO", "USB", "BAC", "PFE", "CSCO", "VZ", "WFC", "MO"],
        account_nav=10000,
        min_fisher=6,
        min_wheel_grade="B",
    )
    candidates = screener.run()
    print(f"\n{'='*60}")
    print(f"  {len(candidates)} candidates found")
    print(f"{'='*60}")
    for c in candidates:
        flag = "★ " if c["wheel_grade"] == "A" else "  "
        print(
            f"{flag}{c['ticker']:<6} Grade:{c['wheel_grade']} "
            f"Score:{c['wheel_score']:>3}  Fisher:{c['fisher_score']:>2}/15  "
            f"Price:${c['price']:<7.2f} IV:{c['iv_pct']:>5.1f}%  "
            f"Strike:${c['csp_strike']:<6}  ROC:{c['roc_est']}%"
        )
    print()


# ---------------------------------------------------------------------------
# Signal fetchers — Insider trades, Political trades, Social buzz
# ---------------------------------------------------------------------------

import urllib.request as _urllib
import json as _json
import datetime as _dt
import threading as _threading

_signals_cache   = {}   # { ticker: {signals, fetched_at} }
_CACHE_TTL_SEC   = 3600 * 2   # 2h for SEC / congressional
_SOCIAL_TTL_SEC  = 900        # 15min for StockTwits


def _fetch_insider(ticker: str) -> dict:
    """SEC EDGAR Form 4 filings — last 30 days, net buy/sell direction."""
    try:
        end   = _dt.date.today()
        start = end - _dt.timedelta(days=30)
        url   = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
            f"&dateRange=custom&startdt={start}&enddt={end}"
            f"&forms=4&hits.hits.total.value=true"
        )
        req = _urllib.Request(url, headers={"User-Agent": "ETradeBot/1.0 research@example.com"})
        with _urllib.urlopen(req, timeout=8) as r:
            data  = _json.loads(r.read())
            total = data.get("hits", {}).get("total", {})
            count = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
            if count == 0:
                return {"signal": "none", "count": 0, "label": "—", "url": ""}
            edgar_url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
                         f"?action=getcompany&CIK={ticker}&type=4&owner=include&count=40")
            hits  = data.get("hits", {}).get("hits", [])
            buys  = sum(1 for h in hits if "purchase" in str(h).lower() or "acquisition" in str(h).lower())
            sells = count - buys
            if buys > sells:
                return {"signal": "buy",  "count": count, "label": f"BUY ×{count}", "url": edgar_url}
            elif sells > buys:
                return {"signal": "sell", "count": count, "label": f"SELL ×{count}", "url": edgar_url}
            else:
                return {"signal": "mixed","count": count, "label": f"MIX ×{count}",  "url": edgar_url}
    except Exception:
        return {"signal": "none", "count": 0, "label": "—", "url": ""}


# ── Political signal — module-level cache ─────────────────────────────────
_SENATE_WATCHER_CACHE: dict = {}   # ticker → list[trade]
_SENATE_WATCHER_TS:    float = 0   # epoch seconds of last full fetch
_SENATE_WATCHER_TTL:   int   = 4 * 3600   # refresh every 4 hours
_CAPITOL_TRADES_CACHE: dict = {}   # ticker → list[trade]
_CAPITOL_TRADES_TS:    dict = {}   # ticker → epoch seconds


def _norm_date(raw: str) -> str:
    """Normalise MM/DD/YYYY or YYYY-MM-DD → YYYY-MM-DD."""
    if raw and "/" in raw:
        p = raw.split("/")
        if len(p) == 3:
            return f"{p[2]}-{p[0].zfill(2)}-{p[1].zfill(2)}"
    return raw[:10] if raw else ""


def _build_political_result(all_trades: list, source: str, ticker: str = "") -> dict:
    """Convert a list of normalised trade dicts into the political signal dict."""
    ticker_upper = ticker.upper()
    # Source-specific deep-link URLs
    if source == "capitoltrades" and ticker_upper:
        pol_url = f"https://www.capitoltrades.com/issuers/{ticker_upper}"
    elif ticker_upper:
        pol_url = f"https://efdsearch.senate.gov/search/?q={ticker_upper}"
    else:
        pol_url = "https://www.capitoltrades.com/trades"

    if not all_trades:
        return {"signal": "none", "label": "—", "source": source, "url": ""}
    buys  = [t for t in all_trades
              if any(k in t["type"].lower() for k in ("purchase","buy"))]
    sells = [t for t in all_trades
              if any(k in t["type"].lower() for k in ("sale","sell"))]
    names = list({t["rep"].split(",")[0].strip()
                  for t in all_trades if t.get("rep")})[:2]
    label = ", ".join(n for n in names if n) or "Congress"
    count = len(all_trades)
    chambers = sorted({t.get("chamber","") for t in all_trades if t.get("chamber")})
    ch_str   = "/".join(chambers) if chambers else "Congress"
    if buys and not sells:
        return {"signal":"buy",   "label":f"🏛 BUY ×{count} {ch_str} ({label})",   "source":source, "url":pol_url}
    elif sells and not buys:
        return {"signal":"sell",  "label":f"🏛 SELL ×{count} {ch_str} ({label})",  "source":source, "url":pol_url}
    elif buys and sells:
        return {"signal":"mixed", "label":f"🏛 MIX ×{count} {ch_str} ({label})",   "source":source, "url":pol_url}
    else:
        return {"signal":"mixed", "label":f"🏛 ACT ×{count} {ch_str} ({label})",   "source":source, "url":pol_url}


def _source1_capitol_trades(ticker: str, cutoff: str) -> list:
    """
    Source 1: Capitol Trades internal API — both chambers, no auth required.
    Per-ticker cache of 30 minutes so the screener can call this for 40 tickers
    without hammering the endpoint.
    Endpoint: GET /api/trades?ticker=KO&txDate=90d&pageSize=100
    Returns list of normalised trade dicts.
    """
    import time as _time
    global _CAPITOL_TRADES_CACHE, _CAPITOL_TRADES_TS
    ticker_upper = ticker.upper()
    ttl = 30 * 60   # 30-minute per-ticker cache
    if (ticker_upper in _CAPITOL_TRADES_CACHE
            and _time.time() - _CAPITOL_TRADES_TS.get(ticker_upper, 0) < ttl):
        trades = _CAPITOL_TRADES_CACHE[ticker_upper]
        return [t for t in trades if t["date"] >= cutoff]

    try:
        url = (f"https://www.capitoltrades.com/api/trades"
               f"?ticker={ticker_upper}&txDate=90d&pageSize=100")
        req = _urllib.Request(url, headers={
            "Accept":        "application/json",
            "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer":       "https://www.capitoltrades.com/trades",
            "Cache-Control": "no-cache",
        })
        with _urllib.urlopen(req, timeout=10) as r:
            body = r.read()
            if r.status != 200:
                return []
            payload = _json.loads(body)

        # Handle both {"data":[...]} and top-level list responses
        raw_trades = (payload.get("data") or payload.get("trades")
                      or (payload if isinstance(payload, list) else []))
        if not raw_trades:
            _CAPITOL_TRADES_CACHE[ticker_upper] = []
            _CAPITOL_TRADES_TS[ticker_upper] = _time.time()
            return []

        trades = []
        for t in raw_trades:
            tx_type = (t.get("txType") or t.get("transaction_type")
                       or t.get("type") or "").strip()
            tx_date = _norm_date(t.get("txDate") or t.get("transaction_date")
                                 or t.get("date") or "")
            pol_name = (t.get("politician") or t.get("politician_name")
                        or t.get("representative") or t.get("name") or "")
            if hasattr(pol_name, "get"):   # sometimes nested object
                pol_name = (pol_name.get("name") or pol_name.get("fullName")
                            or pol_name.get("firstName","") + " "
                            + pol_name.get("lastName","")).strip()
            chamber = (t.get("chamber") or t.get("house") or "Congress")
            if not tx_date:
                continue
            trades.append({
                "rep": str(pol_name).strip() or "Member",
                "type":    tx_type,
                "date":    tx_date,
                "chamber": str(chamber).strip(),
            })

        _CAPITOL_TRADES_CACHE[ticker_upper] = trades
        _CAPITOL_TRADES_TS[ticker_upper] = _time.time()
        logger.debug("Capitol Trades: %d trades for %s", len(trades), ticker_upper)
        return [t for t in trades if t["date"] >= cutoff]

    except Exception as e:
        logger.debug("Capitol Trades fetch failed for %s: %s", ticker_upper, e)
        return []


def _source2_senate_watcher(ticker: str, cutoff: str) -> list:
    """
    Source 2: Senate Stock Watcher GitHub aggregate JSON — Senate only.
    Full file (~several MB) fetched once and cached in memory for 4 hours.
    All 40 screener tickers can be filtered client-side after one download.
    URL: timothycarambat/senate-stock-watcher-data aggregate all_transactions.json
    """
    import time as _time
    global _SENATE_WATCHER_CACHE, _SENATE_WATCHER_TS
    ticker_upper = ticker.upper()

    # Refresh cache if stale
    if _time.time() - _SENATE_WATCHER_TS > _SENATE_WATCHER_TTL:
        agg_url = ("https://raw.githubusercontent.com/timothycarambat/"
                   "senate-stock-watcher-data/master/aggregate/all_transactions.json")
        try:
            req = _urllib.Request(agg_url,
                headers={"User-Agent": "ETradeBot/1.0 (open source wheel strategy)"})
            with _urllib.urlopen(req, timeout=20) as r:
                raw = _json.loads(r.read())
            new_cache: dict = {}
            # Aggregate file is a list of transactions with a "ticker" field
            for txn in (raw if isinstance(raw, list) else []):
                tkr = str(txn.get("ticker","")).upper().replace("$","").strip()
                if not tkr:
                    continue
                new_cache.setdefault(tkr, []).append(txn)
            _SENATE_WATCHER_CACHE = new_cache
            _SENATE_WATCHER_TS    = _time.time()
            logger.info("Senate Watcher cache refreshed: %d tickers", len(new_cache))
        except Exception as e:
            logger.warning("Senate Watcher aggregate fetch failed: %s", e)
            # Don't reset TS — will retry on next screener run

    raw_trades = _SENATE_WATCHER_CACHE.get(ticker_upper, [])
    trades = []
    for txn in raw_trades:
        fname = txn.get("first_name","")
        lname = txn.get("last_name","")
        name  = f"{fname} {lname}".strip() or txn.get("senator","Senator")
        tx_type = txn.get("type","")
        tx_date = _norm_date(txn.get("transaction_date",""))
        if not tx_date or tx_date < cutoff:
            continue
        trades.append({
            "rep":     name,
            "type":    tx_type,
            "date":    tx_date,
            "chamber": "Senate",
        })
    return trades


def _fetch_political(ticker: str) -> dict:
    """
    Congressional trading signal — two-source cascade.

    Source 1: Capitol Trades internal API (capitoltrades.com)
      - Both chambers (House + Senate)
      - No API key, no auth
      - Per-ticker 30-min cache — safe for 40-ticker screener
      - Fragile: undocumented internal endpoint, may change

    Source 2: Senate Stock Watcher GitHub aggregate (fallback)
      - Senate only
      - Single bulk JSON download, 4-hour in-memory cache
      - Free, no key, MIT-licensed open source, proven stable
      - Covers all tickers after one download

    Lookback: 90 days (STOCK Act requires disclosure within 45 days,
    so 90 days gives 1-2 reporting cycles of signal depth).
    """
    ticker_upper = ticker.upper()
    cutoff       = (_dt.date.today() - _dt.timedelta(days=90)).isoformat()

    # ── Source 1: Capitol Trades ──────────────────────────────────────
    trades = _source1_capitol_trades(ticker_upper, cutoff)
    if trades:
        return _build_political_result(trades, "capitoltrades", ticker_upper)

    # ── Source 2: Senate Stock Watcher aggregate ──────────────────────
    trades = _source2_senate_watcher(ticker_upper, cutoff)
    if trades:
        return _build_political_result(trades, "senatewatcher", ticker_upper)

    return {"signal": "none", "label": "—", "source": "none"}

def _fetch_social(ticker: str) -> dict:
    """Social buzz from two sources:
    1. Reddit r/wallstreetbets + r/stocks — via Reddit public JSON API (no auth)
    2. Yahoo Finance news count — via yfinance (already installed)
    Combines mention count and news volume into a buzz score.
    """
    ticker_upper = ticker.upper()
    mention_count = 0
    bull_signals  = 0
    bear_signals  = 0

    # ── Source 1: Reddit public JSON (no auth needed) ──────────────────
    for subreddit in ["wallstreetbets", "stocks", "options"]:
        try:
            url = (f"https://www.reddit.com/r/{subreddit}/search.json"
                   f"?q={ticker_upper}&sort=new&limit=25&t=week")
            req = _urllib.Request(url, headers={
                "User-Agent": "python:etradebot.wheel:v1.0 (by /u/wheelstrategybot)"
            })
            with _urllib.urlopen(req, timeout=8) as r:
                data  = _json.loads(r.read())
                posts = data.get("data", {}).get("children", [])
                for post in posts:
                    d     = post.get("data", {})
                    title = (d.get("title","") + " " + d.get("selftext","")).lower()
                    # Count only posts that actually mention the ticker as a word
                    if f" {ticker_upper.lower()} " in f" {title} " or f"${ticker_upper.lower()}" in title:
                        mention_count += 1
                        score = float(d.get("score", 0) or 0)
                        if any(w in title for w in ["bull","calls","moon","long","buy"]):
                            bull_signals += 1
                        if any(w in title for w in ["bear","puts","short","sell","crash"]):
                            bear_signals += 1
        except Exception:
            pass

    # ── Source 2: Yahoo Finance news count ─────────────────────────────
    news_count = 0
    try:
        import yfinance as yf
        t     = yf.Ticker(ticker_upper)
        news  = t.news or []
        news_count = min(len(news), 10)   # cap contribution at 10
        mention_count += news_count
    except Exception:
        pass

    # ── Score ───────────────────────────────────────────────────────────
    url_reddit = f"https://www.reddit.com/search/?q={ticker_upper}&sort=new&t=week"
    url_news   = f"https://finance.yahoo.com/quote/{ticker_upper}/news/"

    if mention_count == 0:
        return {"signal": "none", "score": 0, "label": "—",
                "url_reddit": url_reddit, "url_news": url_news}

    total_sentiment = bull_signals + bear_signals
    bull_pct = round(bull_signals / total_sentiment * 100) if total_sentiment > 0 else 50

    if mention_count >= 8:
        buzz_level = "high"
        icon = "🔥"
    elif mention_count >= 3:
        buzz_level = "medium"
        icon = "📊"
    else:
        buzz_level = "low"
        icon = "📉"

    sentiment_str = f"{bull_pct}% bull" if total_sentiment > 0 else "neutral"
    label = f"{icon} {mention_count} mentions · {sentiment_str}"

    return {
        "signal":   buzz_level,
        "score":    mention_count,
        "bull_pct": bull_pct,
        "label":    label,
        "url_reddit": url_reddit,
        "url_news":   url_news,
    }


def fetch_signals(ticker: str, force: bool = False) -> dict:
    """Fetch all three signals for a ticker, with caching."""
    now    = _dt.datetime.utcnow().timestamp()
    cached = _signals_cache.get(ticker)
    if cached and not force:
        age = now - cached["fetched_at"]
        if age < _CACHE_TTL_SEC:
            return cached["signals"]

    signals = {
        "insider":   _fetch_insider(ticker),
        "political": _fetch_political(ticker),
        "social":    _fetch_social(ticker),
    }
    _signals_cache[ticker] = {"signals": signals, "fetched_at": now}
    return signals


def fetch_signals_batch(tickers: list, max_workers: int = 6) -> dict:
    """Fetch signals for multiple tickers concurrently."""
    results = {}
    lock    = _threading.Lock()

    def _fetch(t):
        sig = fetch_signals(t)
        with lock:
            results[t] = sig

    threads = [_threading.Thread(target=_fetch, args=(t,), daemon=True) for t in tickers]
    for th in threads[:max_workers]:
        th.start()
    for th in threads[:max_workers]:
        th.join(timeout=12)
    return results
