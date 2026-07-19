"""
backtest/ledger.py — Trade Recording & P&L Accounting
======================================================
Records every synthetic fill during the backtest and computes:
  - Per-cycle P&L (CSP premium + CC premium - BTC cost - commissions)
  - Per-ticker win rate, average yield, assignment rate
  - Portfolio value curve (one data point per trading day)
  - Phase timeline for each ticker (for the HTML report)

No changes to WheelEngine required — this sits alongside it and
listens to the same events the BacktestEngine fires.
"""

import datetime
from dataclasses import dataclass, field
from typing import Optional


# E-Trade standard commission per contract (matches server.py preview endpoint)
COMMISSION_PER_CONTRACT = 0.65


# ── Event types (mirrors WheelEngine audit log) ───────────────────────────

@dataclass
class TradeEvent:
    date:      datetime.date
    ticker:    str
    event:     str          # CSP_PLACED | BTC_FILLED | EXPIRED | ASSIGNED |
                            # CC_PLACED | CC_CALLED | CC_EXPIRED | ROLL
    strike:    Optional[float] = None
    expiry:    Optional[str]   = None
    contracts: int             = 1
    premium:   Optional[float] = None   # per-share credit/debit
    price:     Optional[float] = None   # stock price at event
    pnl:       Optional[float] = None   # realised P&L for cycle-closing events
    note:      str             = ""


@dataclass
class CycleRecord:
    ticker:         str
    cycle_num:      int
    start_date:     datetime.date
    end_date:       Optional[datetime.date]   = None
    csp_strike:     float                     = 0.0
    csp_expiry:     str                       = ""
    csp_premium:    float                     = 0.0
    csp_contracts:  int                       = 1
    btc_cost:       float                     = 0.0     # per-share; 0 if expired
    assigned:       bool                      = False
    cc_strike:      float                     = 0.0
    cc_expiry:      str                       = ""
    cc_premium:     float                     = 0.0
    cc_contracts:   int                       = 1
    called_away:    bool                      = False
    rolls:          int                       = 0
    gross_pnl:      float                     = 0.0
    commissions:    float                     = 0.0
    net_pnl:        float                     = 0.0
    outcome:        str                       = ""      # BTC | EXPIRED | ASSIGNED_CC_CALLED | ASSIGNED_CC_HELD | OPEN


# ── Phase snapshot for timeline chart ────────────────────────────────────

@dataclass
class PhaseSnapshot:
    date:   datetime.date
    ticker: str
    phase:  str   # matches WheelEngine Phase values


# ── Main ledger ───────────────────────────────────────────────────────────

class BacktestLedger:
    def __init__(self, starting_capital: float):
        self.starting_capital = starting_capital
        self.events:    list[TradeEvent]    = []
        self.cycles:    list[CycleRecord]   = []
        self.snapshots: list[PhaseSnapshot] = []

        # Portfolio value curve: list of (date, value)
        self._daily_value: list[tuple[datetime.date, float]] = []
        self._current_value = starting_capital

        # Open cycle tracking (ticker → CycleRecord)
        self._open: dict[str, CycleRecord] = {}
        self._cycle_count: dict[str, int] = {}

    # ── Event recording ───────────────────────────────────────────────────

    def record(self, event: TradeEvent) -> None:
        self.events.append(event)

    def record_phase(self, date: datetime.date, ticker: str, phase: str) -> None:
        self.snapshots.append(PhaseSnapshot(date, ticker, phase))

    def record_portfolio_value(self, date: datetime.date, value: float) -> None:
        self._current_value = value
        self._daily_value.append((date, value))

    # ── Cycle lifecycle ───────────────────────────────────────────────────

    def open_cycle(self, date: datetime.date, ticker: str,
                   strike: float, expiry: str, contracts: int,
                   premium: float) -> None:
        n = self._cycle_count.get(ticker, 0) + 1
        self._cycle_count[ticker] = n
        cycle = CycleRecord(
            ticker       = ticker,
            cycle_num    = n,
            start_date   = date,
            csp_strike   = strike,
            csp_expiry   = expiry,
            csp_premium  = premium,
            csp_contracts = contracts,
            commissions  = COMMISSION_PER_CONTRACT * contracts,
        )
        self._open[ticker] = cycle
        self.record(TradeEvent(
            date=date, ticker=ticker, event="CSP_PLACED",
            strike=strike, expiry=expiry, contracts=contracts, premium=premium,
        ))

    def close_btc(self, date: datetime.date, ticker: str,
                  btc_price: float) -> Optional[CycleRecord]:
        cycle = self._open.pop(ticker, None)
        if not cycle:
            return None
        credit_total = cycle.csp_premium * cycle.csp_contracts * 100
        btc_total    = btc_price * cycle.csp_contracts * 100
        commissions  = cycle.commissions + COMMISSION_PER_CONTRACT * cycle.csp_contracts
        gross        = credit_total - btc_total
        net          = gross - commissions
        cycle.btc_cost    = btc_price
        cycle.end_date    = date
        cycle.gross_pnl   = round(gross, 2)
        cycle.commissions = round(commissions, 2)
        cycle.net_pnl     = round(net, 2)
        cycle.outcome     = "BTC"
        self.cycles.append(cycle)
        self.record(TradeEvent(
            date=date, ticker=ticker, event="BTC_FILLED",
            premium=btc_price, pnl=net,
        ))
        self._current_value += net
        return cycle

    def close_expired(self, date: datetime.date, ticker: str) -> Optional[CycleRecord]:
        cycle = self._open.pop(ticker, None)
        if not cycle:
            return None
        credit_total = cycle.csp_premium * cycle.csp_contracts * 100
        net          = credit_total - cycle.commissions
        cycle.end_date  = date
        cycle.gross_pnl = round(credit_total, 2)
        cycle.net_pnl   = round(net, 2)
        cycle.outcome   = "EXPIRED"
        self.cycles.append(cycle)
        self.record(TradeEvent(
            date=date, ticker=ticker, event="EXPIRED", pnl=net,
        ))
        self._current_value += net
        return cycle

    def record_assignment(self, date: datetime.date, ticker: str,
                          strike: float) -> None:
        cycle = self._open.get(ticker)
        if cycle:
            cycle.assigned = True
        self.record(TradeEvent(
            date=date, ticker=ticker, event="ASSIGNED", strike=strike,
        ))

    def open_cc(self, date: datetime.date, ticker: str,
                strike: float, expiry: str, contracts: int,
                premium: float) -> None:
        cycle = self._open.get(ticker)
        if cycle:
            cycle.cc_strike    = strike
            cycle.cc_expiry    = expiry
            cycle.cc_premium   = premium
            cycle.cc_contracts = contracts
            cycle.commissions += COMMISSION_PER_CONTRACT * contracts
        self.record(TradeEvent(
            date=date, ticker=ticker, event="CC_PLACED",
            strike=strike, expiry=expiry, contracts=contracts, premium=premium,
        ))

    def close_cc_called(self, date: datetime.date, ticker: str,
                         cost_basis: float) -> Optional[CycleRecord]:
        cycle = self._open.pop(ticker, None)
        if not cycle:
            return None
        csp_credit   = cycle.csp_premium * cycle.csp_contracts * 100
        cc_credit    = cycle.cc_premium  * cycle.cc_contracts  * 100
        stock_gain   = (cycle.cc_strike - cost_basis) * (cycle.csp_contracts * 100)
        gross        = csp_credit + cc_credit + stock_gain
        net          = gross - cycle.commissions
        cycle.called_away = True
        cycle.end_date    = date
        cycle.gross_pnl   = round(gross, 2)
        cycle.net_pnl     = round(net, 2)
        cycle.outcome     = "ASSIGNED_CC_CALLED"
        self.cycles.append(cycle)
        self.record(TradeEvent(
            date=date, ticker=ticker, event="CC_CALLED",
            strike=cycle.cc_strike, pnl=net,
        ))
        self._current_value += net
        return cycle

    def close_cc_expired(self, date: datetime.date, ticker: str) -> None:
        """CC expired worthless — premium kept, back to ASSIGNED for new CC."""
        cycle = self._open.get(ticker)
        if cycle:
            # Bank the CC premium, reset for new CC round
            cc_credit = cycle.cc_premium * cycle.cc_contracts * 100
            self._current_value += cc_credit
            cycle.cc_premium  = 0.0
            cycle.cc_strike   = 0.0
            cycle.cc_expiry   = ""
        self.record(TradeEvent(
            date=date, ticker=ticker, event="CC_EXPIRED",
        ))

    def record_roll(self, date: datetime.date, ticker: str,
                    new_strike: float, new_expiry: str,
                    net_credit: float) -> None:
        cycle = self._open.get(ticker)
        if cycle:
            cycle.rolls += 1
            cycle.csp_strike = new_strike
            cycle.csp_expiry = new_expiry
            # Accumulate the net credit from the roll
            cycle.csp_premium = cycle.csp_premium + net_credit
        self.record(TradeEvent(
            date=date, ticker=ticker, event="ROLL",
            strike=new_strike, expiry=new_expiry, premium=net_credit,
        ))
        if net_credit > 0:
            self._current_value += net_credit * (cycle.csp_contracts if cycle else 1) * 100

    def close_open_position(self, date: datetime.date, ticker: str,
                             current_price: float, note: str = "sim_end") -> None:
        """Mark any position still open at backtest end — value at market."""
        cycle = self._open.pop(ticker, None)
        if not cycle:
            return
        # Unrealised: credit collected minus current option value (estimated)
        cycle.end_date = date
        cycle.outcome  = "OPEN"
        cycle.note     = note  # type: ignore[attr-defined]
        self.cycles.append(cycle)

    # ── Analytics ─────────────────────────────────────────────────────────

    def portfolio_curve(self) -> list[tuple[datetime.date, float]]:
        return self._daily_value

    def total_net_pnl(self) -> float:
        return round(self._current_value - self.starting_capital, 2)

    def total_premium_collected(self) -> float:
        return round(sum(
            c.csp_premium * c.csp_contracts * 100 +
            c.cc_premium  * c.cc_contracts  * 100
            for c in self.cycles
        ), 2)

    def total_commissions(self) -> float:
        return round(sum(c.commissions for c in self.cycles), 2)

    def closed_cycles(self) -> list[CycleRecord]:
        return [c for c in self.cycles if c.outcome != "OPEN"]

    def win_rate(self) -> float:
        closed = self.closed_cycles()
        if not closed:
            return 0.0
        wins = sum(1 for c in closed if c.net_pnl > 0)
        return round(wins / len(closed) * 100, 1)

    def assignment_rate(self) -> float:
        closed = self.closed_cycles()
        if not closed:
            return 0.0
        assigned = sum(1 for c in closed if c.assigned)
        return round(assigned / len(closed) * 100, 1)

    def per_ticker_stats(self) -> dict:
        stats = {}
        for c in self.cycles:
            t = c.ticker
            if t not in stats:
                stats[t] = {
                    "cycles": 0, "net_pnl": 0.0, "premium": 0.0,
                    "wins": 0, "assignments": 0, "rolls": 0,
                    "outcomes": [],
                }
            s = stats[t]
            s["cycles"]   += 1
            s["net_pnl"]  += c.net_pnl
            s["premium"]  += c.csp_premium * c.csp_contracts * 100
            s["rolls"]    += c.rolls
            s["outcomes"].append(c.outcome)
            if c.net_pnl > 0:
                s["wins"] += 1
            if c.assigned:
                s["assignments"] += 1
        for t, s in stats.items():
            n = s["cycles"]
            s["net_pnl"]      = round(s["net_pnl"], 2)
            s["premium"]      = round(s["premium"], 2)
            s["win_rate"]     = round(s["wins"] / n * 100, 1) if n else 0
            s["assign_rate"]  = round(s["assignments"] / n * 100, 1) if n else 0
        return dict(sorted(stats.items(), key=lambda x: -x[1]["net_pnl"]))

    def monthly_income(self) -> dict:
        """Aggregate net P&L by calendar month (YYYY-MM)."""
        monthly: dict[str, float] = {}
        for c in self.cycles:
            if c.end_date and c.outcome != "OPEN":
                key = c.end_date.strftime("%Y-%m")
                monthly[key] = round(monthly.get(key, 0) + c.net_pnl, 2)
        return dict(sorted(monthly.items()))

    def open_positions_summary(self) -> list[dict]:
        """Positions still open at backtest end — what the live bot inherits."""
        result = []
        for ticker, cycle in self._open.items():
            result.append({
                "ticker":      ticker,
                "phase":       "CC_OPEN" if cycle.cc_strike else "CSP_OPEN" if not cycle.assigned else "ASSIGNED",
                "csp_strike":  cycle.csp_strike,
                "csp_expiry":  cycle.csp_expiry,
                "csp_premium": cycle.csp_premium,
                "cc_strike":   cycle.cc_strike,
                "cc_expiry":   cycle.cc_expiry,
                "assigned":    cycle.assigned,
                "contracts":   cycle.csp_contracts,
                "since":       str(cycle.start_date),
            })
        return result
