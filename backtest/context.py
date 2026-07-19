"""
backtest/context.py — Rich Analysis Context for Ollama
=======================================================
Intercepts BacktestResults BEFORE HTML rendering and builds a structured
context object from the raw CycleRecord data in the ledger.

This is the fix for the "thin summary" problem:
  BEFORE: Ollama received 10 aggregate fields (net_pnl, win_rate, cycles count...)
  AFTER:  Ollama receives all 18 fields per CycleRecord × all closed trades,
          including worst/best individual trades, roll counts, skipped tickers,
          and per-ticker breakdown with avg P&L per cycle.

The build_ollama_context() method produces the same plain-text format as
build_advisor_prompt() in server.py — the format phi4 and qwen2.5 follow well.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BacktestAnalysisContext:
    """
    All data needed for Ollama to give specific, trade-level backtest analysis.
    Built from BacktestResults.ledger before HTML rendering.
    """

    # ── Metadata ───────────────────────────────────────────────────────────
    start:              str
    end:                str
    capital:            float
    tickers_used:       list
    tickers_skipped:    dict       # ticker → reason (beta too high, etc.)
    exit_rule:          str        # "50% only" | "50% OR 21 DTE"
    dte_exit:           int        # 0 = 50% only, 21 = dual trigger

    # ── Top-level results ──────────────────────────────────────────────────
    end_value:          float
    net_pnl:            float
    total_return_pct:   float
    total_premium:      float
    total_commissions:  float
    win_rate:           float
    assignment_rate:    float
    total_cycles:       int
    closed_cycles:      int

    # ── Exit rule comparison (populated if compare=True) ───────────────────
    alt_net_pnl:        Optional[float] = None
    alt_cycles:         Optional[int]   = None
    alt_win_rate:       Optional[float] = None
    alt_label:          str             = "50% only"   # the alternative rule

    # ── Per-ticker stats ───────────────────────────────────────────────────
    # List of dicts: ticker, cycles, net_pnl, premium, win_rate, assign_rate,
    #                rolls, avg_pnl_per_cycle
    per_ticker:         list = field(default_factory=list)

    # ── Raw trade records ──────────────────────────────────────────────────
    # All closed CycleRecord objects serialised to dicts — the key addition
    all_cycles:         list = field(default_factory=list)

    # ── Pre-sorted for quick-question prompts ──────────────────────────────
    worst_trades:       list = field(default_factory=list)   # 5 worst by net_pnl
    best_trades:        list = field(default_factory=list)   # 5 best by net_pnl

    # ── Monthly income ─────────────────────────────────────────────────────
    monthly:            dict = field(default_factory=dict)   # YYYY-MM → net_pnl

    # ── Open at end (what live bot inherits) ──────────────────────────────
    open_positions:     list = field(default_factory=list)

    # ── Portfolio curve (excluded from Ollama context by default) ─────────
    curve:              list = field(default_factory=list)   # (date, value) tuples

    # ── Derived stats computed at build time ──────────────────────────────
    avg_pnl_per_cycle:  float = 0.0
    best_month:         str   = ""
    worst_month:        str   = ""
    best_month_pnl:     float = 0.0
    worst_month_pnl:    float = 0.0
    total_rolls:        int   = 0

    # ──────────────────────────────────────────────────────────────────────

    def build_ollama_context(self, include_curve: bool = False) -> str:
        """
        Build a structured plain-text context string for Ollama.
        Format mirrors build_advisor_prompt() — the format phi4 and qwen2.5
        follow reliably per AI_comparison.txt (Jun 22 2026).

        include_curve=False by default — adds ~40KB with no analytical value
        for the five quick-question prompts.
        """
        lines = []

        # ── Header ──────────────────────────────────────────────────────────
        lines.append(f"BACKTEST RESULTS: {self.start} → {self.end}")
        lines.append(f"Capital: ${self.capital:,.0f} → ${self.end_value:,.2f} "
                     f"({self.total_return_pct:+.1f}%, {self.net_pnl:+.2f} net)")
        lines.append(f"Exit rule: {self.exit_rule}")
        used_str = ", ".join(str(t) for t in self.tickers_used[:10])
        lines.append(f"Universe: {len(self.tickers_used)} tickers used ({used_str})")
        if self.tickers_skipped:
            skip_str = "  ".join(f"{t}({r})" for t, r in
                                 list(self.tickers_skipped.items())[:5])
            lines.append(f"Skipped:  {skip_str}")
        lines.append("")

        # ── Summary metrics ────────────────────────────────────────────────
        lines.append("SUMMARY METRICS:")
        lines.append(f"  Closed cycles:     {self.closed_cycles}  "
                     f"(total incl. open: {self.total_cycles})")
        lines.append(f"  Win rate:          {self.win_rate}%")
        lines.append(f"  Assignment rate:   {self.assignment_rate}%")
        lines.append(f"  Premium collected: ${self.total_premium:,.2f}  "
                     f"minus ${self.total_commissions:.2f} commissions")
        lines.append(f"  Avg P&L / cycle:   ${self.avg_pnl_per_cycle:+.2f}")
        lines.append(f"  Total rolls:       {self.total_rolls}")
        if self.best_month:
            lines.append(f"  Best month:        {self.best_month} "
                         f"(${self.best_month_pnl:+.2f})")
        if self.worst_month:
            lines.append(f"  Worst month:       {self.worst_month} "
                         f"(${self.worst_month_pnl:+.2f})")
        lines.append("")

        # ── Exit rule comparison ───────────────────────────────────────────
        if self.alt_net_pnl is not None:
            lines.append("EXIT RULE COMPARISON:")
            lines.append(f"  {self.alt_label:<25} "
                         f"${self.alt_net_pnl:+,.2f}  "
                         f"{self.alt_cycles} cycles  "
                         f"{self.alt_win_rate}% win")
            lines.append(f"  {self.exit_rule:<25} "
                         f"${self.net_pnl:+,.2f}  "
                         f"{self.closed_cycles} cycles  "
                         f"{self.win_rate}% win  ← THIS RUN")
            diff = self.net_pnl - (self.alt_net_pnl or 0)
            lines.append(f"  Difference: ${diff:+,.2f} in favour of "
                         f"{'this run' if diff >= 0 else self.alt_label}")
            lines.append("")

        # ── Per-ticker performance ─────────────────────────────────────────
        if self.per_ticker:
            lines.append("PER-TICKER PERFORMANCE (sorted by net P&L):")
            for s in self.per_ticker:
                t   = s.get("ticker", s.get("name","?"))
                n   = s.get("cycles", 0)
                pnl = s.get("net_pnl", 0)
                wr  = s.get("win_rate", 0)
                ar  = s.get("assign_rate", 0)
                rl  = s.get("rolls", 0)
                avg = round(pnl / n, 2) if n > 0 else 0
                lines.append(
                    f"  {t:<6} {n:>2} cycles  "
                    f"{pnl:>+8.2f}  "
                    f"{wr:>5.1f}% win  "
                    f"{ar:>4.1f}% assign  "
                    f"{rl:>2} rolls  "
                    f"avg ${avg:+.2f}/cycle"
                )
            lines.append("")

        # ── Worst trades — key for "what failed" analysis ─────────────────
        if self.worst_trades:
            lines.append("WORST 5 TRADES:")
            for c in self.worst_trades:
                lines.append(_fmt_cycle(c))
            lines.append("")

        # ── Best trades ────────────────────────────────────────────────────
        if self.best_trades:
            lines.append("BEST 5 TRADES:")
            for c in self.best_trades:
                lines.append(_fmt_cycle(c))
            lines.append("")

        # ── Monthly income ─────────────────────────────────────────────────
        if self.monthly:
            lines.append("MONTHLY NET INCOME:")
            chunks = [f"{m}: ${v:+.0f}" for m, v in self.monthly.items()]
            # 4 per row for readability
            for i in range(0, len(chunks), 4):
                lines.append("  " + "  ".join(chunks[i:i+4]))
            lines.append("")

        # ── Open positions at end ──────────────────────────────────────────
        if self.open_positions:
            lines.append("OPEN POSITIONS AT END (live bot inherits these):")
            for p in self.open_positions:
                lines.append(f"  {p.get('ticker')}  {p.get('phase')}  "
                             f"${p.get('csp_strike',0)}  exp {p.get('csp_expiry','')}")
        else:
            lines.append("OPEN AT END: none (all cycles closed within backtest window)")

        if include_curve and self.curve:
            lines.append(f"\nPORTFOLIO CURVE: {len(self.curve)} daily values "
                         f"(omitted from context — use export_json() for full data)")

        return "\n".join(lines)


# ── Formatting helper ──────────────────────────────────────────────────────

def _fmt_cycle(c: dict) -> str:
    """One-line summary of a CycleRecord dict for worst/best trade lists."""
    ticker  = c.get("ticker", "?")
    outcome = c.get("outcome", "?")
    strike  = c.get("csp_strike", 0)
    expiry  = str(c.get("csp_expiry", ""))[:10]
    sold    = c.get("csp_premium", 0)
    btc     = c.get("btc_cost", 0)
    rolls   = c.get("rolls", 0)
    net     = c.get("net_pnl", 0)
    start   = str(c.get("start_date", ""))[:10]
    assigned = "ASSIGNED" if c.get("assigned") else ""
    return (f"  {ticker:<6} {start}  CSP ${strike}  exp {expiry}  "
            f"sold ${sold:.2f}  BTC ${btc:.2f}  "
            f"net ${net:+.2f}  rolls:{rolls}  {outcome} {assigned}").rstrip()


# ── Factory function ───────────────────────────────────────────────────────

def build_context_from_results(results, alt_result: dict | None = None,
                                dte_exit: int = 0) -> BacktestAnalysisContext:
    """
    Build a BacktestAnalysisContext from a BacktestResults object.
    Called by BacktestResults.build_context() — don't call directly.

    alt_result: the thin summary dict from the comparison run (50%-only)
                {net_pnl, cycles, win_rate}
    dte_exit:   0 = 50% only, 21 = dual trigger — determines exit_rule label
    """
    L      = results.ledger
    closed = L.closed_cycles()
    stats  = L.per_ticker_stats()

    # Unique tickers that had at least one closed cycle
    tickers_used = list(dict.fromkeys(c.ticker for c in closed))

    # Sort cycles for worst/best
    sorted_closed = sorted(closed, key=lambda c: c.net_pnl)
    worst = sorted_closed[:5]
    best  = sorted_closed[-5:][::-1]

    # Monthly stats
    monthly = L.monthly_income()
    best_month  = max(monthly, key=monthly.get) if monthly else ""
    worst_month = min(monthly, key=monthly.get) if monthly else ""

    # Avg P&L per cycle
    avg_pnl = (round(sum(c.net_pnl for c in closed) / len(closed), 2)
               if closed else 0.0)

    # Total rolls across all cycles
    total_rolls = sum(c.rolls for c in closed)

    # Per-ticker list with ticker name included
    per_ticker_list = []
    for ticker, s in stats.items():
        s_copy = dict(s)
        s_copy["ticker"] = ticker
        per_ticker_list.append(s_copy)

    # Exit rule label
    if dte_exit > 0:
        exit_rule = f"50% profit OR {dte_exit} DTE (whichever first)"
        alt_label = "50% profit only"
    else:
        exit_rule = "50% profit only"
        alt_label = f"50% OR 21 DTE"

    # Alt comparison
    alt_pnl  = alt_result.get("net_pnl")  if alt_result else None
    alt_cyc  = alt_result.get("cycles")   if alt_result else None
    alt_wr   = alt_result.get("win_rate") if alt_result else None

    net_pnl = L.total_net_pnl()

    return BacktestAnalysisContext(
        start             = str(results.start),
        end               = str(results.end),
        capital           = results.capital,
        tickers_used      = tickers_used,
        tickers_skipped   = dict(results.skipped),
        exit_rule         = exit_rule,
        dte_exit          = dte_exit,
        end_value         = round(results.capital + net_pnl, 2),
        net_pnl           = net_pnl,
        total_return_pct  = round(net_pnl / results.capital * 100, 1)
                            if results.capital > 0 else 0.0,
        total_premium     = L.total_premium_collected(),
        total_commissions = L.total_commissions(),
        win_rate          = L.win_rate(),
        assignment_rate   = L.assignment_rate(),
        total_cycles      = len(L.cycles),
        closed_cycles     = len(closed),
        alt_net_pnl       = alt_pnl,
        alt_cycles        = alt_cyc,
        alt_win_rate      = alt_wr,
        alt_label         = alt_label,
        per_ticker        = per_ticker_list,
        all_cycles        = [vars(c) if hasattr(c,'__dict__') else dict(c)
                             for c in closed],
        worst_trades      = [vars(c) if hasattr(c,'__dict__') else dict(c)
                             for c in worst],
        best_trades       = [vars(c) if hasattr(c,'__dict__') else dict(c)
                             for c in best],
        monthly           = monthly,
        open_positions    = L.open_positions_summary(),
        curve             = L.portfolio_curve(),
        avg_pnl_per_cycle = avg_pnl,
        best_month        = best_month,
        worst_month       = worst_month,
        best_month_pnl    = monthly.get(best_month, 0.0),
        worst_month_pnl   = monthly.get(worst_month, 0.0),
        total_rolls       = total_rolls,
    )
