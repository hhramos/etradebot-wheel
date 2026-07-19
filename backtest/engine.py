"""
backtest/engine.py — Wheel Strategy Backtest Driver
====================================================
Replaces ETradeAPI with yfinance historical data and drives the
existing WheelEngine state machine through a full year of trading days.

What it simulates:
  • Monthly CSP entry on the first trading day after the prior expiry
  • Daily close-price check for 50% BTC target
  • Roll when stock closes below strike for 3 consecutive days
    (proxy for delta breach since we don't have historical option deltas)
  • Assignment when stock closes below strike on expiry Friday
  • Covered call entry the next trading day after assignment
  • CC called away when stock closes above CC strike on expiry Friday
  • Capital sizing via trade_rules.check_collateral (unchanged)

Premium estimation:
  Uses Black-Scholes with 30-day historical vol as IV proxy —
  the same method as estimate_iv_from_hist_vol() in wheel_screener.py,
  applied to the historical window ending on each entry date.

Usage:
  engine = BacktestEngine(start_date="2024-06-01", end_date="2025-06-13",
                           capital=10000)
  results = engine.run()
"""

import sys
import os
import datetime
import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Path setup so we can import from etradebot root ──────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backtest.ledger import BacktestLedger

# ── Constants ─────────────────────────────────────────────────────────────

RISK_FREE_RATE     = 0.05     # 2024-2025 approximate Fed funds
OTM_PCT            = 0.05     # 5% OTM for CSP strike selection
CC_OTM_BUFFER      = 1.05     # CC strike = cost_basis × 1.05
ROLL_DAYS_ITM      = 3        # consecutive days ITM before rolling
MIN_PREMIUM        = 0.15     # minimum credit (matches config.json)
MIN_DTE_ENTRY      = 21       # don't open a new position with < 21 DTE
COMMISSION         = 0.65     # per contract (matches server.py)

# Screening quality gate (matches config.json rules)
MIN_FISHER         = 7
MIN_WHEEL_GRADE    = "B"
GRADE_ORDER        = {"A": 0, "B": 1, "C": 2, "D": 3}


# ── Black-Scholes put pricer ──────────────────────────────────────────────

def _ncdf(x: float) -> float:
    """
    Standard normal CDF using math.erf — pure Python, no scipy required.
    Accurate to ~7 decimal places (sufficient for option pricing).
    """
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Black-Scholes European put price — pure Python (no scipy).
    S=stock price, K=strike, T=years to expiry, r=risk-free, sigma=annualised vol.
    Returns per-share price (not per-contract).
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.01
    import math
    d1  = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2  = d1 - sigma * math.sqrt(T)
    put = K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
    return round(max(float(put), 0.01), 2)


def bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes European call price — pure Python (no scipy)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.01
    import math
    d1   = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2   = d1 - sigma * math.sqrt(T)
    call = S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return round(max(float(call), 0.01), 2)


def hist_vol(closes: pd.Series, window: int = 30) -> float:
    """Annualised historical volatility over `window` trading days."""
    if len(closes) < window + 1:
        return 0.25
    returns = closes.pct_change().dropna().tail(window)
    hv = float(returns.std() * np.sqrt(252))
    # Apply same 1.15× IV premium as estimate_iv_from_hist_vol()
    return min(max(hv * 1.15, 0.08), 1.0)


# ── Strike selection (matches wheel_screener.select_csp_strike) ───────────

def select_strike(price: float, otm_pct: float = OTM_PCT) -> float:
    raw = price * (1 - otm_pct)
    if price < 20:
        return round(raw * 2) / 2
    return round(raw)


# ── Third Friday calculator ───────────────────────────────────────────────

def third_friday(year: int, month: int) -> datetime.date:
    d = datetime.date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:
            fridays += 1
            if fridays == 3:
                return d
        d += datetime.timedelta(days=1)


def expiry_dates_in_range(start: datetime.date,
                           end: datetime.date) -> list[datetime.date]:
    """All monthly 3rd-Friday expiries between start and end."""
    expiries = []
    y, m = start.year, start.month
    while True:
        tf = third_friday(y, m)
        if tf > end:
            break
        if tf >= start:
            expiries.append(tf)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return expiries


# ── Wheel position tracker (lightweight, parallel to WheelEngine) ─────────

class _Position:
    """Tracks one open wheel position during the simulation."""
    __slots__ = [
        "ticker", "phase", "entry_date",
        "csp_strike", "csp_expiry", "csp_premium", "contracts",
        "initial_delta", "btc_target",
        "days_itm",           # consecutive closes below CSP strike (roll proxy)
        "cost_basis",         # strike - premium_per_share
        "cc_strike", "cc_expiry", "cc_premium",
        "iv_at_entry",
    ]

    def __init__(self, ticker, entry_date, csp_strike, csp_expiry,
                 csp_premium, contracts, iv_at_entry):
        self.ticker       = ticker
        self.phase        = "CSP_OPEN"
        self.entry_date   = entry_date
        self.csp_strike   = csp_strike
        self.csp_expiry   = csp_expiry
        self.csp_premium  = csp_premium
        self.contracts    = contracts
        self.iv_at_entry  = iv_at_entry
        self.btc_target   = round(csp_premium * 0.50, 2)
        self.days_itm     = 0
        self.cost_basis   = 0.0
        self.cc_strike    = 0.0
        self.cc_expiry    = ""
        self.cc_premium   = 0.0
        self.initial_delta = 0.0


# ── Backtest results container ────────────────────────────────────────────

class BacktestResults:
    def __init__(self, ledger: BacktestLedger, start: datetime.date,
                 end: datetime.date, universe: list[str],
                 skipped: dict, capital: float):
        self.ledger   = ledger
        self.start    = start
        self.end      = end
        self.universe = universe
        self.skipped  = skipped   # ticker → reason screener rejected
        self.capital  = capital

    def build_context(self, alt_result: dict | None = None,
                      dte_exit: int = 0):
        """
        Build a BacktestAnalysisContext from raw ledger data.
        Intercepts BEFORE HTML rendering so Ollama gets individual trade
        records (CycleRecord), not just aggregate KPI totals.

        alt_result: thin summary dict from compare run {net_pnl, cycles, win_rate}
        dte_exit:   0=50%-only, 21=dual-trigger — drives the exit_rule label
        """
        from backtest.context import build_context_from_results
        return build_context_from_results(self, alt_result=alt_result,
                                          dte_exit=dte_exit)

    def export_html(self, path: str) -> None:
        """Generate HTML report at path."""
        from backtest.report import render_html
        render_html(self, path)
        print(f"  Report written → {path}")

    def export_json(self, path: str, include_curve: bool = False) -> None:
        """Export full analysis context as JSON for debugging or external tools."""
        import json
        ctx = self.build_context()
        data = {k: (str(v) if isinstance(v, __import__('datetime').date) else v)
                for k, v in vars(ctx).items()
                if k != 'curve' or include_curve}
        with open(path, 'w') as f:
            json.dump(data, f, default=str, indent=2)
        print(f"  Context JSON written → {path}")

    def print_summary(self) -> None:
        L = self.ledger
        closed = L.closed_cycles()
        print(f"\n{'═'*62}")
        print(f"  ETradeBot Wheel Backtest  {self.start} → {self.end}")
        print(f"{'═'*62}")
        print(f"  Starting capital:   ${self.capital:>10,.2f}")
        print(f"  Ending value:       ${self.capital + L.total_net_pnl():>10,.2f}")
        print(f"  Net P&L:            ${L.total_net_pnl():>+10,.2f}")
        print(f"  Total premium:      ${L.total_premium_collected():>10,.2f}")
        print(f"  Commissions:        ${L.total_commissions():>10,.2f}")
        print(f"  Closed cycles:      {len(closed):>10}")
        print(f"  Win rate:           {L.win_rate():>9.1f}%")
        print(f"  Assignment rate:    {L.assignment_rate():>9.1f}%")
        print(f"{'─'*62}")
        print("  Per-ticker breakdown:")
        for t, s in L.per_ticker_stats().items():
            print(f"    {t:<6}  {s['cycles']}c  "
                  f"P&L ${s['net_pnl']:>+7.2f}  "
                  f"win {s['win_rate']:>5.1f}%  "
                  f"asgn {s['assign_rate']:>5.1f}%  "
                  f"rolls {s['rolls']}")
        open_pos = L.open_positions_summary()
        if open_pos:
            print(f"{'─'*62}")
            print("  Open at end (carried forward to live bot):")
            for p in open_pos:
                phase_label = p["phase"]
                detail = (f"${p['cc_strike']} CC {p['cc_expiry']}"
                          if p.get("cc_strike") else
                          f"${p['csp_strike']} CSP {p['csp_expiry']}")
                print(f"    {p['ticker']:<6}  {phase_label:<12}  {detail}  ×{p['contracts']}c")
        print(f"{'═'*62}\n")

    def export_html(self, path: str) -> None:
        from backtest.report import render_html
        render_html(self, path)
        print(f"  Report written → {path}")


# ── Main backtest engine ───────────────────────────────────────────────────

class BacktestEngine:
    """
    Parameters
    ----------
    start_date : str | date
        First day of the simulation window (YYYY-MM-DD).
    end_date : str | date
        Last day (inclusive). Defaults to yesterday.
    capital : float
        Starting cash (NAV). Positions are sized against this.
    tickers : list[str] | None
        Override the universe. None = full universe.json list.
    max_positions : int
        Max concurrent wheels (matches config.json max_wheels=10).
    max_position_pct : float
        Max NAV% per position (matches config.json max_position_pct=0.08).
    min_fisher : int
        Minimum Fisher score to enter (matches config.json min_fisher=7).
    """

    def __init__(
        self,
        start_date:       str | datetime.date = "2024-06-01",
        end_date:         str | datetime.date | None = None,
        capital:          float = 10_000.0,
        tickers:          list[str] | None = None,
        max_positions:    int   = 10,
        max_position_pct: float = 0.08,
        min_fisher:       int   = MIN_FISHER,
    ):
        self.start = (datetime.date.fromisoformat(start_date)
                      if isinstance(start_date, str) else start_date)
        self.end   = (datetime.date.fromisoformat(end_date)
                      if isinstance(end_date, str)
                      else end_date or datetime.date.today() - datetime.timedelta(days=1))
        self.capital          = capital
        self.max_positions    = max_positions
        self.max_position_pct = max_position_pct
        self.min_fisher       = min_fisher

        # Load universe
        if tickers:
            self.universe = [t.upper() for t in tickers]
        else:
            self.universe = self._load_universe()

        self._ledger    = BacktestLedger(capital)
        self._positions: dict[str, _Position] = {}  # ticker → _Position
        self._cash      = capital
        self._skipped   = {}  # ticker → reason
        self._dte_exit  = 0   # 0=disabled  21=new dual-trigger rule

    # ── Universe ──────────────────────────────────────────────────────────

    def _load_universe(self) -> list[str]:
        universe_path = os.path.join(_ROOT, "data", "universe.json")
        if os.path.exists(universe_path):
            import json
            try:
                with open(universe_path) as f:
                    return json.load(f).get("tickers", [])
            except Exception:
                pass
        return ["KO", "USB", "CSCO", "BAC", "VZ", "PFE", "WFC", "MO",
                "SOFI", "F", "T", "IBM", "XOM", "CVX", "PEP", "MCD"]

    # ── Data download ─────────────────────────────────────────────────────

    def _download(self) -> dict[str, pd.DataFrame]:
        """Download OHLCV + 1 extra year for vol calculations."""
        import yfinance as yf
        fetch_start = self.start - datetime.timedelta(days=90)
        logger.info("Downloading %d tickers %s → %s …",
                    len(self.universe), fetch_start, self.end)
        raw = yf.download(
            self.universe,
            start=str(fetch_start),
            end=str(self.end + datetime.timedelta(days=1)),
            progress=False,
            auto_adjust=True,
            group_by="ticker",
        )
        data = {}
        for ticker in self.universe:
            try:
                if len(self.universe) == 1:
                    df = raw.copy()
                else:
                    df = raw[ticker].copy()
                df = df.dropna(subset=["Close"])
                df.index = pd.to_datetime(df.index).date
                if len(df) > 10:
                    data[ticker] = df
                else:
                    self._skipped[ticker] = "insufficient data"
            except Exception as e:
                self._skipped[ticker] = str(e)
        logger.info("Downloaded %d tickers; %d skipped",
                    len(data), len(self._skipped))
        return data

    # ── Screener gate (uses Fisher-style heuristics on yfinance info) ─────

    def _screen_ticker(self, ticker: str) -> Optional[str]:
        """
        Return None if ticker passes screening, or a rejection reason string.
        Uses a lightweight version of the Fisher/Wheel scoring from
        wheel_screener.py — no live screener call needed since we're
        working with historical data where real-time info isn't available.
        """
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info
            dy   = info.get("dividendYield") or 0
            beta = info.get("beta") or 1.0
            pe   = info.get("trailingPE") or 999

            # Quality gates — loosened to match live screener behaviour
            # beta > 3.0 only (CCL ~2.3, SOFI ~2.2 are valid wheel candidates)
            if beta > 3.0:
                return f"beta {beta:.1f} too high"
            # PE gate: only reject if PE is extreme AND no dividend AND not a REIT/BDC
            if pe > 200 and dy < 0.005:
                return f"PE {pe:.0f} no dividend"
            return None
        except Exception:
            return None   # don't reject on data errors

    # ── Capital sizing ─────────────────────────────────────────────────────

    def _contracts_for(self, strike: float) -> int:
        """
        Max contracts using tiered NAV-aware position sizing rules.
        Reads config.json position_sizing.tiers — same rules as live bot.
        Always allows 1 contract minimum when cash permits (matching live behaviour).
        """
        collateral_1c = strike * 100
        if collateral_1c <= 0:
            return 0
        try:
            import sys, os
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            from bot.trade_rules import get_sizing_rules
            rules   = get_sizing_rules(self.capital)
            pos_cap = rules["per_position_pct"]
        except Exception:
            pos_cap = self.max_position_pct  # fallback
        by_cash = int((self._cash - 300) / collateral_1c)
        by_nav  = int(self.capital * pos_cap / collateral_1c)
        if by_cash >= 1 and by_nav == 0:
            by_nav = 1   # 1-contract minimum — unavoidable at small account size
        n = min(by_cash, by_nav, 10)
        return max(n, 0)

    # ── Entry logic ────────────────────────────────────────────────────────

    def _try_open_csp(self, ticker: str, date: datetime.date,
                       df: pd.DataFrame, expiry: datetime.date) -> bool:
        """
        Attempt to open a new CSP position.
        Returns True if opened.
        """
        if ticker in self._positions:
            return False
        if len(self._positions) >= self.max_positions:
            return False

        # DTE check
        dte = (expiry - date).days
        if dte < MIN_DTE_ENTRY:
            return False

        # Stock price on entry date
        if date not in df.index:
            return False
        close = float(df.loc[date, "Close"])
        if close <= 0:
            return False

        # Strike
        strike = select_strike(close)
        if strike <= 0:
            return False

        # IV (historical vol over prior 30 trading days up to entry)
        prior = df[df.index < date]["Close"]
        iv    = hist_vol(prior, window=min(30, len(prior) - 1))

        # Premium via Black-Scholes
        T       = dte / 365.0
        premium = bs_put(close, strike, T, RISK_FREE_RATE, iv)

        if premium < MIN_PREMIUM:
            return False

        # Capital check
        n = self._contracts_for(strike)
        if n < 1:
            return False

        # Lock collateral
        collateral = strike * 100 * n
        self._cash -= collateral

        # Record
        pos = _Position(
            ticker      = ticker,
            entry_date  = date,
            csp_strike  = strike,
            csp_expiry  = expiry.isoformat(),
            csp_premium = premium,
            contracts   = n,
            iv_at_entry = iv,
        )
        self._positions[ticker] = pos
        self._ledger.open_cycle(date, ticker, strike, expiry.isoformat(),
                                 n, premium)
        self._ledger.record_phase(date, ticker, "CSP_OPEN")
        self._cash += premium * 100 * n   # credit received
        logger.debug("%s  %s  CSP $%.0f×%dc @$%.2f  (IV %.0f%%  DTE %d)",
                     date, ticker, strike, n, premium, iv * 100, dte)
        return True

    # ── Monitoring: CSP_OPEN ──────────────────────────────────────────────

    def _monitor_csp(self, ticker: str, pos: _Position,
                      date: datetime.date, df: pd.DataFrame,
                      expiry: datetime.date) -> None:
        if date not in df.index:
            return
        close = float(df.loc[date, "Close"])
        dte   = (expiry - date).days

        # Estimate current option value (Black-Scholes)
        prior = df[df.index < date]["Close"]
        iv    = hist_vol(prior, window=min(30, len(prior) - 1))
        T     = max(dte / 365.0, 0.001)
        current_opt = bs_put(close, pos.csp_strike, T, RISK_FREE_RATE, iv)

        # 50% profit target OR 21-DTE exit — whichever first
        dte_exit  = getattr(self, '_dte_exit', 0)
        hit_dte   = dte_exit > 0 and dte <= dte_exit and current_opt < pos.csp_premium
        if current_opt <= pos.btc_target or hit_dte:
            profit = (pos.csp_premium - current_opt) * 100 * pos.contracts
            profit -= COMMISSION * pos.contracts * 2   # open + close commissions
            cycle = self._ledger.close_btc(date, ticker, current_opt)
            self._cash += pos.csp_strike * 100 * pos.contracts   # collateral back
            self._cash -= current_opt * 100 * pos.contracts        # BTC cost
            del self._positions[ticker]
            logger.debug("%s  %s  BTC @$%.2f  profit $%.2f",
                         date, ticker, current_opt, profit)
            return

        # Roll proxy: 3 consecutive closes below strike
        if close < pos.csp_strike:
            pos.days_itm += 1
        else:
            pos.days_itm = 0

        if pos.days_itm >= ROLL_DAYS_ITM and dte > 7:
            # Roll: BTC current, STO new at 1 strike lower, next expiry
            btc_cost   = current_opt
            new_strike = pos.csp_strike - (1.0 if pos.csp_strike >= 20 else 0.5)
            next_exp   = _next_expiry_after(expiry)
            new_T      = (next_exp - date).days / 365.0
            new_prem   = bs_put(close, new_strike, new_T, RISK_FREE_RATE, iv)
            net_credit = new_prem - btc_cost
            # Only roll if we get a net credit or small debit (≤ $0.10)
            if net_credit >= -0.10:
                self._ledger.record_roll(date, ticker, new_strike,
                                          next_exp.isoformat(), net_credit)
                pos.csp_strike  = new_strike
                pos.csp_expiry  = next_exp.isoformat()
                pos.csp_premium = new_prem
                pos.btc_target  = round(new_prem * 0.50, 2)
                pos.days_itm    = 0
                logger.debug("%s  %s  ROLL → $%.0f %s  net cr $%.2f",
                             date, ticker, new_strike,
                             next_exp.isoformat(), net_credit)

    # ── Expiry processing ─────────────────────────────────────────────────

    def _process_csp_expiry(self, ticker: str, pos: _Position,
                              expiry: datetime.date,
                              df: pd.DataFrame) -> None:
        """Called on expiry Friday for CSP_OPEN positions."""
        exp_close = _close_on_or_before(df, expiry)
        if exp_close is None:
            return

        if exp_close < pos.csp_strike:
            # ITM at expiry → ASSIGNED
            self._ledger.record_assignment(expiry, ticker, pos.csp_strike)
            # Return collateral (will be converted to shares)
            self._cash += pos.csp_strike * 100 * pos.contracts
            # Deduct stock cost (strike × shares)
            self._cash -= pos.csp_strike * 100 * pos.contracts
            cost_basis = round(pos.csp_strike - pos.csp_premium, 2)
            pos.cost_basis = cost_basis
            pos.phase      = "ASSIGNED"
            self._ledger.record_phase(expiry, ticker, "ASSIGNED")
            logger.debug("%s  %s  ASSIGNED @ $%.0f  cost basis $%.2f",
                         expiry, ticker, pos.csp_strike, cost_basis)
        else:
            # OTM → expired worthless, keep full premium
            self._ledger.close_expired(expiry, ticker)
            self._cash += pos.csp_strike * 100 * pos.contracts   # collateral back
            del self._positions[ticker]
            logger.debug("%s  %s  EXPIRED worthless  full premium $%.2f",
                         expiry, ticker, pos.csp_premium)

    def _open_cc(self, ticker: str, pos: _Position,
                  date: datetime.date, df: pd.DataFrame,
                  expiry: datetime.date) -> None:
        """Open a covered call after assignment."""
        if date not in df.index:
            return
        close  = float(df.loc[date, "Close"])
        strike = round(pos.cost_basis * CC_OTM_BUFFER)
        # Ensure CC strike is above current price and cost basis
        strike = max(strike, round(close * 1.02), round(pos.cost_basis) + 1)
        dte    = (expiry - date).days
        if dte < 7:
            expiry = _next_expiry_after(expiry)
            dte    = (expiry - date).days

        prior  = df[df.index < date]["Close"]
        iv     = hist_vol(prior, window=min(30, len(prior) - 1))
        T      = max(dte / 365.0, 0.001)
        premium = bs_call(close, strike, T, RISK_FREE_RATE, iv)

        if premium < MIN_PREMIUM:
            premium = MIN_PREMIUM

        pos.cc_strike  = strike
        pos.cc_expiry  = expiry.isoformat()
        pos.cc_premium = premium
        pos.phase      = "CC_OPEN"
        self._ledger.open_cc(date, ticker, strike, expiry.isoformat(),
                              pos.contracts, premium)
        self._ledger.record_phase(date, ticker, "CC_OPEN")
        self._cash += premium * 100 * pos.contracts   # CC credit
        logger.debug("%s  %s  CC STO $%.0f×%dc @$%.2f  DTE %d",
                     date, ticker, strike, pos.contracts, premium, dte)

    def _process_cc_expiry(self, ticker: str, pos: _Position,
                            expiry: datetime.date,
                            df: pd.DataFrame) -> None:
        """Called on expiry Friday for CC_OPEN positions."""
        exp_close = _close_on_or_before(df, expiry)
        if exp_close is None:
            return

        if exp_close >= pos.cc_strike:
            # Stock called away
            self._ledger.close_cc_called(expiry, ticker, pos.cost_basis)
            self._cash += pos.cc_strike * 100 * pos.contracts   # sale proceeds
            del self._positions[ticker]
            logger.debug("%s  %s  CALLED AWAY @ $%.0f",
                         expiry, ticker, pos.cc_strike)
        else:
            # CC expired worthless — keep premium, sell new CC next cycle
            self._ledger.close_cc_expired(expiry, ticker)
            pos.cc_strike  = 0.0
            pos.cc_expiry  = ""
            pos.cc_premium = 0.0
            pos.phase      = "ASSIGNED"
            self._ledger.record_phase(expiry, ticker, "ASSIGNED")
            logger.debug("%s  %s  CC expired worthless  sell new CC",
                         expiry, ticker)

    # ── Main simulation loop ───────────────────────────────────────────────

    def run(self) -> "BacktestResults":
        """Run the full backtest. Returns BacktestResults."""
        try:
            import yfinance  # noqa
        except ImportError:
            raise RuntimeError(
                "yfinance not installed. Run: pip install yfinance numpy pandas"
            )

        print(f"\n  ETradeBot Backtest  {self.start} → {self.end}")
        print(f"  Universe: {len(self.universe)} tickers  |  Capital: ${self.capital:,.0f}\n")

        # Download all data
        data = self._download()

        # Pre-screen tickers (quality gate, non-blocking)
        print("  Screening tickers…")
        for ticker in list(data.keys()):
            reason = self._screen_ticker(ticker)
            if reason:
                self._skipped[ticker] = reason
                del data[ticker]
        screened = list(data.keys())
        print(f"  {len(screened)} tickers passed screening  "
              f"({len(self._skipped)} skipped)\n")

        # Build sorted list of all trading days in the window
        all_dates: list[datetime.date] = []
        for df in data.values():
            for d in df.index:
                if self.start <= d <= self.end:
                    all_dates.append(d)
        trading_days = sorted(set(all_dates))

        # Build expiry calendar
        expiries = expiry_dates_in_range(self.start, self.end)
        expiry_set = set(expiries)

        # Build {expiry → next_expiry} map
        next_exp_map: dict[datetime.date, datetime.date] = {}
        for i, e in enumerate(expiries[:-1]):
            next_exp_map[e] = expiries[i + 1]

        # Assign each ticker to an entry expiry round-robin
        # (avoids all tickers entering on the same day)
        ticker_expiry_offset: dict[str, int] = {
            t: i % max(len(expiries), 1)
            for i, t in enumerate(screened)
        }

        print(f"  Simulating {len(trading_days)} trading days…")
        prev_portfolio = self.capital
        for day in trading_days:
            # ── Check expiry events ────────────────────────────────────────
            if day in expiry_set:
                for ticker, pos in list(self._positions.items()):
                    if ticker not in data:
                        continue
                    df  = data[ticker]
                    exp = datetime.date.fromisoformat(
                        pos.csp_expiry if pos.phase in ("CSP_OPEN",)
                        else pos.cc_expiry
                    )
                    if exp != day:
                        continue
                    if pos.phase == "CSP_OPEN":
                        self._process_csp_expiry(ticker, pos, day, df)
                    elif pos.phase == "CC_OPEN":
                        self._process_cc_expiry(ticker, pos, day, df)

            # ── Open new CSPs ──────────────────────────────────────────────
            # Entry day = first trading day after prior expiry
            # We attempt entry in the week following each expiry
            for ticker in screened:
                if ticker in self._positions:
                    continue
                if ticker not in data:
                    continue
                df = data[ticker]

                # Find which expiry this ticker targets
                offset     = ticker_expiry_offset.get(ticker, 0)
                if offset >= len(expiries):
                    continue
                target_exp = expiries[min(offset, len(expiries) - 1)]

                # Advance to the expiry that's still in the future
                while target_exp <= day and target_exp in next_exp_map:
                    target_exp = next_exp_map[target_exp]
                    ticker_expiry_offset[ticker] = expiries.index(target_exp)

                if target_exp <= day:
                    continue

                dte = (target_exp - day).days
                # Enter in the window 28–50 DTE (matches config.json dte_range)
                if 28 <= dte <= 50:
                    self._try_open_csp(ticker, day, df, target_exp)

            # ── Open CCs for assigned positions ───────────────────────────
            for ticker, pos in list(self._positions.items()):
                if pos.phase == "ASSIGNED":
                    if ticker not in data:
                        continue
                    df = data[ticker]
                    # Find next expiry
                    future = [e for e in expiries if e > day]
                    if future:
                        self._open_cc(ticker, pos, day, df, future[0])

            # ── Monitor CSP_OPEN positions ─────────────────────────────────
            for ticker, pos in list(self._positions.items()):
                if pos.phase != "CSP_OPEN":
                    continue
                if ticker not in data:
                    continue
                df  = data[ticker]
                exp = datetime.date.fromisoformat(pos.csp_expiry)
                self._monitor_csp(ticker, pos, day, df, exp)

            # ── Record daily portfolio value ───────────────────────────────
            portfolio = self._cash
            # Add unrealised option value for open positions
            for ticker, pos in self._positions.items():
                if pos.phase == "CSP_OPEN" and ticker in data:
                    df    = data[ticker]
                    close = _close_on_or_before(df, day)
                    if close:
                        exp  = datetime.date.fromisoformat(pos.csp_expiry)
                        dte  = max((exp - day).days, 1)
                        prior = df[df.index < day]["Close"]
                        iv   = hist_vol(prior, window=min(30, len(prior) - 1))
                        opt  = bs_put(close, pos.csp_strike, dte / 365.0,
                                      RISK_FREE_RATE, iv)
                        # Unrealised = collateral + (premium - current_opt)×100×n
                        collateral = pos.csp_strike * 100 * pos.contracts
                        unrealised = collateral + (pos.csp_premium - opt) * 100 * pos.contracts
                        portfolio += unrealised
                elif pos.phase in ("CC_OPEN", "ASSIGNED") and ticker in data:
                    df    = data[ticker]
                    close = _close_on_or_before(df, day)
                    if close:
                        # Stock value at cost basis
                        portfolio += pos.cost_basis * 100 * pos.contracts
            self._ledger.record_portfolio_value(day, portfolio)

        # ── Close out open positions at end ────────────────────────────────
        for ticker, pos in list(self._positions.items()):
            df    = data.get(ticker)
            close = _close_on_or_before(df, self.end) if df is not None else None
            self._ledger.close_open_position(
                self.end, ticker, close or 0,
                note="carried_forward_to_live"
            )
            logger.info("Open at end: %s  %s  $%.0f",
                        ticker, pos.phase, pos.csp_strike)

        print("  Simulation complete.\n")
        return BacktestResults(
            ledger   = self._ledger,
            start    = self.start,
            end      = self.end,
            universe = screened,
            skipped  = self._skipped,
            capital  = self.capital,
        )


# ── Helpers ───────────────────────────────────────────────────────────────

def _close_on_or_before(df: pd.DataFrame,
                          date: datetime.date) -> Optional[float]:
    """Most recent closing price on or before `date`."""
    sub = df[df.index <= date]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["Close"])


def _next_expiry_after(expiry: datetime.date) -> datetime.date:
    """The 3rd Friday of the month after `expiry`."""
    m = expiry.month + 1
    y = expiry.year
    if m > 12:
        m = 1
        y += 1
    return third_friday(y, m)
