"""
trade_rules.py — Hard Guardrails & Deterministic Logic
=======================================================
These rules are NEVER overridden by the Ollama model.
Every trade must pass all applicable checks before execution.

Also contains the reinvestment opportunity scorer — the engine
that decides whether to re-enter the original stock or favor a
lower-priced alternative with better capital efficiency.
"""

import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Strategy constants ────────────────────────────────────────────────────

MAX_WHEELS            = 10       # max concurrent wheel positions
MAX_POSITION_PCT      = 0.08     # max 8% of NAV collateral per position
MAX_DEPLOYED_PCT      = 0.80     # never exceed 80% total collateral deployed
MIN_CASH_RESERVE      = 300      # always keep $300 cash floor
MIN_DEPLOY_AMOUNT     = 300      # minimum capital worth deploying

# Option quality filters
DELTA_RANGE           = (0.18, 0.35)   # target delta for new CSP entries
DTE_RANGE             = (28, 50)       # acceptable days-to-expiry range
DTE_ROLL_THRESHOLD    = 14             # roll when DTE falls below this
PROFIT_TARGET_PCT     = 0.50    # BTC at 50% premium decay
DTE_EXIT_THRESHOLD    = 21      # Exit at 21 DTE if in profit (whichever comes first)
DTE_ROLL_THRESHOLD    = 14      # Roll at 14 DTE if at loss
MIN_ENTRY_DTE         = 25      # Never enter a new position with less than 25 DTE
EARNINGS_BLACKOUT_NEW = 7       # Days — refuse NEW entry within this window of earnings
EARNINGS_CLOSE_DAYS   = 5       # Days before earnings — close/exit existing position
MAX_ENTRY_DELTA       = -0.30   # Maximum delta at entry (more negative = deeper ITM)
MIN_ENTRY_DELTA       = -0.15   # Minimum delta at entry (less negative = too far OTM)
ILLIQUID_DTE_OVERRIDE = 14      # For illiquid options use 14 DTE instead of 21
ROLL_DELTA_MULT       = 1.5            # roll when delta hits 1.5× initial
EARNINGS_BLACKOUT     = 7              # days before/after earnings
MIN_OPEN_INTEREST     = 100            # minimum open interest
MAX_SPREAD_PCT        = 0.30           # max bid-ask spread as % of mid
MIN_PREMIUM           = 0.15           # minimum credit worth collecting
MAX_CONTRACTS_TICKER  = 10             # hard cap per ticker

# Contract scaling
CONTRACT_INCREMENT    = 1              # add 1 contract at a time
SCALE_UP_TRIGGER_PCT  = 0.15           # scale if idle capital > 15% of NAV

# Reinvestment engine
DEPLOY_IF_IDLE_MINS      = 60          # if cash idle > 60 min, force evaluate
QUALITY_FALLBACK_MINS    = 120         # lower quality bar after 2h idle
FORCE_REENTER_MINS       = 240         # force re-enter original after 4h
FRIDAY_CUTOFF_ET         = datetime.time(15, 0)   # no new entries after 3PM Friday

# Reinvestment scoring weights
EFFICIENCY_WEIGHT     = 0.40
QUALITY_WEIGHT        = 0.40
FIT_BONUS_WEIGHT      = 0.20
MIN_FISHER_DEFAULT    = 7
MIN_WHEEL_GRADE       = "B"
MIN_FISHER_FALLBACK   = 5
MIN_WHEEL_FALLBACK    = "C"

# Grade ordering (A is best)
GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}


# ── Hard rule checks ──────────────────────────────────────────────────────

class RuleViolation(Exception):
    """Raised when a trade fails a hard rule check."""
    def __init__(self, rule: str, detail: str):
        self.rule   = rule
        self.detail = detail
        super().__init__(f"Rule violation [{rule}]: {detail}")


def check_collateral(strike: float, contracts: int,
                     nav: float, buying_power: float) -> None:
    """
    Verify a new CSP fits within position size and cash limits.
    Raises RuleViolation if check fails.
    """
    collateral = strike * 100 * contracts
    pct_of_nav = collateral / nav if nav > 0 else 1.0

    if pct_of_nav > MAX_POSITION_PCT:
        raise RuleViolation(
            "MAX_POSITION_PCT",
            f"{contracts}c on ${strike} strike = ${collateral:,.0f} "
            f"({pct_of_nav*100:.1f}% of NAV ${nav:,.0f}). "
            f"Max allowed: {MAX_POSITION_PCT*100:.0f}% = ${nav*MAX_POSITION_PCT:,.0f}."
        )

    if collateral > buying_power - MIN_CASH_RESERVE:
        raise RuleViolation(
            "INSUFFICIENT_CAPITAL",
            f"Collateral ${collateral:,.0f} exceeds available "
            f"${buying_power - MIN_CASH_RESERVE:,.0f} "
            f"(buying power ${buying_power:,.0f} minus reserve ${MIN_CASH_RESERVE})."
        )


def check_max_wheels(active_count: int) -> None:
    if active_count >= MAX_WHEELS:
        raise RuleViolation(
            "MAX_WHEELS",
            f"Already at {active_count}/{MAX_WHEELS} active wheels."
        )


def check_deployed_pct(total_deployed: float, nav: float,
                        new_collateral: float) -> None:
    after_pct = (total_deployed + new_collateral) / nav if nav > 0 else 1.0
    if after_pct > MAX_DEPLOYED_PCT:
        raise RuleViolation(
            "MAX_DEPLOYED_PCT",
            f"Adding ${new_collateral:,.0f} would bring total deployed to "
            f"{after_pct*100:.1f}% of NAV. Max: {MAX_DEPLOYED_PCT*100:.0f}%."
        )


def check_earnings_blackout(earnings_date: datetime.date | str | None,
                             ticker: str) -> None:
    if earnings_date is None:
        return
    if isinstance(earnings_date, str):
        try:
            earnings_date = datetime.date.fromisoformat(earnings_date)
        except ValueError:
            return
    from bot.time_utils import today_et
    diff = abs((earnings_date - today_et()).days)
    if diff <= EARNINGS_BLACKOUT:
        raise RuleViolation(
            "EARNINGS_BLACKOUT",
            f"{ticker} earnings in {diff} days ({earnings_date}). "
            f"No new positions within {EARNINGS_BLACKOUT} days of earnings."
        )


def check_option_quality(strike: float, bid: float, ask: float,
                          delta: float, dte: int,
                          open_interest: int, mid: float) -> None:
    """Check that an option strike meets quality minimums."""
    if delta != 0 and not (DELTA_RANGE[0] <= abs(delta) <= DELTA_RANGE[1]):
        raise RuleViolation(
            "DELTA_RANGE",
            f"Delta {delta:.2f} outside target range "
            f"{DELTA_RANGE[0]}–{DELTA_RANGE[1]}."
        )

    if not (DTE_RANGE[0] <= dte <= DTE_RANGE[1]):
        raise RuleViolation(
            "DTE_RANGE",
            f"DTE {dte} outside target range {DTE_RANGE[0]}–{DTE_RANGE[1]} days."
        )

    if open_interest < MIN_OPEN_INTEREST:
        raise RuleViolation(
            "MIN_OPEN_INTEREST",
            f"Open interest {open_interest} below minimum {MIN_OPEN_INTEREST}."
        )

    if mid > 0:
        spread     = ask - bid
        spread_pct = spread / mid
        if spread_pct > MAX_SPREAD_PCT:
            raise RuleViolation(
                "MAX_SPREAD_PCT",
                f"Bid-ask spread ${spread:.2f} is {spread_pct*100:.1f}% of mid ${mid:.2f}. "
                f"Max: {MAX_SPREAD_PCT*100:.0f}%."
            )

    if mid < MIN_PREMIUM:
        raise RuleViolation(
            "MIN_PREMIUM",
            f"Premium ${mid:.2f} below minimum ${MIN_PREMIUM}."
        )


def check_max_contracts_ticker(ticker: str,
                                current_contracts: int,
                                adding: int) -> None:
    after = current_contracts + adding
    if after > MAX_CONTRACTS_TICKER:
        raise RuleViolation(
            "MAX_CONTRACTS_TICKER",
            f"Adding {adding}c to {ticker} would reach {after}c. "
            f"Max per ticker: {MAX_CONTRACTS_TICKER}c."
        )


def should_roll_position(current_delta: float,
                          initial_delta: float,
                          dte: int,
                          pnl_pct: float) -> tuple[bool, str]:
    """
    Determine if a CSP position should be rolled.
    Returns (should_roll, reason).
    """
    if initial_delta and abs(current_delta) >= abs(initial_delta) * ROLL_DELTA_MULT:
        return True, (
            f"Delta breach: current {current_delta:.2f} ≥ "
            f"{ROLL_DELTA_MULT}× initial {initial_delta:.2f}"
        )

    if dte <= DTE_ROLL_THRESHOLD and pnl_pct < 0:
        return True, (
            f"Expiry approaching ({dte} DTE) with loss {pnl_pct:.1f}%"
        )

    return False, ""


def btc_target_hit(cost_basis: float, current_price: float) -> bool:
    """True if position has reached the 50% profit target."""
    if cost_basis <= 0:
        return False
    decay_pct = (cost_basis - current_price) / cost_basis
    return decay_pct >= PROFIT_TARGET_PCT


def should_exit_position(
    cost_basis:     float,
    current_price:  float,
    dte:            int,
    earnings_date:  "str | None" = None,
    is_liquid:      bool         = True,
    ticker:         str          = "",
) -> "tuple[bool, str]":
    """
    Multi-factor exit: 50% decay OR DTE trigger OR earnings exit.
    Priority order:
      1. Earnings close window (always fires regardless of profit)
      2. 50% profit target
      3. 21 DTE (liquid) / 14 DTE (illiquid) if in profit

    Returns (should_exit, reason_string).
    """
    if cost_basis <= 0:
        return False, ""

    # ── Earnings exit — highest priority ─────────────────────────────────
    if earnings_date:
        try:
            import datetime
            ed  = datetime.date.fromisoformat(str(earnings_date)[:10])
            import datetime as _dt_ec
            dte_to_earnings = (ed - _dt_ec.date.today()).days
            if 0 <= dte_to_earnings <= EARNINGS_CLOSE_DAYS:
                return True, (f"Earnings in {dte_to_earnings}d ({earnings_date}) "
                              f"— closing {EARNINGS_CLOSE_DAYS}d before event")
        except Exception:
            pass

    decay_pct  = (cost_basis - current_price) / cost_basis
    in_profit  = current_price < cost_basis

    # ── 50% profit target ─────────────────────────────────────────────────
    if decay_pct >= PROFIT_TARGET_PCT:
        return True, f"50% profit target ({decay_pct*100:.1f}% decay)"

    # ── DTE exit — adjusted for liquidity ────────────────────────────────
    dte_threshold = ILLIQUID_DTE_OVERRIDE if not is_liquid else DTE_EXIT_THRESHOLD
    if dte <= dte_threshold and in_profit:
        liq_note = " [illiquid — using 14 DTE]" if not is_liquid else ""
        return True, (f"{dte_threshold} DTE exit{liq_note} "
                      f"({dte}d left, {decay_pct*100:.1f}% banked)")

    return False, ""


def max_contracts_for_capital(strike: float, capital: float,
                               nav: float) -> int:
    """How many contracts can we run given available capital and position cap?"""
    if strike <= 0 or capital <= 0:
        return 0
    collateral_1c    = strike * 100
    by_capital       = int(capital / collateral_1c)
    by_position_cap  = int((nav * MAX_POSITION_PCT) / collateral_1c)
    return min(by_capital, by_position_cap, MAX_CONTRACTS_TICKER)


# ── Reinvestment opportunity scorer ──────────────────────────────────────

def opportunity_score(candidate: dict,
                       freed_capital: float,
                       nav: float,
                       min_fisher: int = MIN_FISHER_DEFAULT,
                       min_grade: str  = MIN_WHEEL_GRADE) -> dict | None:
    """
    Score a screener candidate for reinvestment.
    Returns a result dict or None if the candidate is ineligible.

    Scoring formula:
        score = (efficiency × 0.40) + (quality × 0.40) + (fit_bonus × 0.20)

    efficiency = monthly_premium / collateral_deployed
    quality    = weighted Fisher + Wheel score
    fit_bonus  = reward for fitting more contracts (up to 3)
    """
    # ── Eligibility gates ──────────────────────────────────────────────
    fisher = candidate.get("fisher_score", 0) or 0
    grade  = candidate.get("wheel_grade",  "D") or "D"
    wheel  = candidate.get("wheel_score",  0) or 0

    if fisher < min_fisher:
        return None

    if GRADE_ORDER.get(grade, 9) > GRADE_ORDER.get(min_grade, 9):
        return None

    strike = float(candidate.get("csp_strike") or candidate.get("price") or 0)
    if strike <= 0:
        return None

    collateral_1c = strike * 100
    contracts = max_contracts_for_capital(strike, freed_capital, nav)

    if contracts == 0:
        return None   # can't afford even 1 contract

    # ── Scoring ───────────────────────────────────────────────────────
    premium_est    = float(candidate.get("premium_est", 0) or
                           candidate.get("roc_est", 0) / 100 * strike or 0)
    if premium_est <= 0:
        # Estimate from IV if direct premium not available
        iv = float(candidate.get("iv_pct", 20) or 20)
        premium_est = round(iv / 500 * strike, 2)

    monthly_premium = premium_est * 100 * contracts
    deployed        = collateral_1c * contracts

    efficiency  = monthly_premium / deployed if deployed > 0 else 0
    quality     = (fisher / 15) * 0.4 + (wheel / 100) * 0.6
    fit_bonus   = min(contracts / 3.0, 1.0)

    score = (efficiency * EFFICIENCY_WEIGHT +
             quality    * QUALITY_WEIGHT    +
             fit_bonus  * FIT_BONUS_WEIGHT)

    reason = (
        f"{candidate['ticker']} scores {score:.3f}: "
        f"{contracts}c × ${strike} = ${deployed:,.0f} deployed, "
        f"~${monthly_premium:.0f}/mo ({efficiency*100:.1f}% efficiency), "
        f"Fisher:{fisher} Wheel:{grade}·{wheel}"
    )

    return {
        "ticker":       candidate.get("ticker", ""),
        "score":        round(score, 4),
        "contracts":    contracts,
        "deployed":     deployed,
        "monthly_est":  round(monthly_premium, 2),
        "efficiency":   round(efficiency * 100, 2),
        "premium_est":  premium_est,
        "strike":       strike,
        "expiry":       candidate.get("csp_expiry", ""),
        "fisher":       fisher,
        "wheel_grade":  grade,
        "wheel_score":  wheel,
        "reason":       reason,
    }


def rank_reinvestment_candidates(candidates: list[dict],
                                  freed_capital: float,
                                  nav: float,
                                  original_ticker: str,
                                  active_tickers: list[str] = None,
                                  idle_minutes: int = 0) -> list[dict]:
    """
    Score and rank all candidates for reinvestment.
    Applies quality fallback if capital has been idle too long.
    Returns ranked list with winner at index 0.

    Args:
        candidates:       screener candidate list
        freed_capital:    dollars available to deploy
        nav:              current account net asset value
        original_ticker:  the stock whose wheel just completed
        active_tickers:   tickers already in active wheels (skip them)
        idle_minutes:     how long capital has been sitting idle
    """
    active = set(active_tickers or [])

    # Determine quality threshold based on idle time
    if idle_minutes >= QUALITY_FALLBACK_MINS:
        min_fisher = MIN_FISHER_FALLBACK
        min_grade  = MIN_WHEEL_FALLBACK
        logger.info(
            "Capital idle %d min — lowering quality bar to Fisher≥%d, Grade≥%s",
            idle_minutes, min_fisher, min_grade
        )
    else:
        min_fisher = MIN_FISHER_DEFAULT
        min_grade  = MIN_WHEEL_GRADE

    scored = []
    for c in candidates:
        ticker = c.get("ticker", "")
        if ticker in active:
            continue   # already running a wheel on this stock

        result = opportunity_score(c, freed_capital, nav,
                                   min_fisher=min_fisher,
                                   min_grade=min_grade)
        if result is None:
            continue

        # Slight boost for original ticker (continuity, already know the stock)
        if ticker == original_ticker:
            result["score"] += 0.005
            result["reason"] += " [+continuity bonus]"

        # Note if this is a lower-priced alternative
        orig_candidate = next((c2 for c2 in candidates
                                if c2.get("ticker") == original_ticker), None)
        if (orig_candidate and
                ticker != original_ticker and
                result["strike"] < float(orig_candidate.get("csp_strike", 999) or 999)):
            result["is_lower_price_alt"] = True
            result["reason"] += " [lower-price alternative]"
        else:
            result["is_lower_price_alt"] = False

        scored.append(result)

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)

    if not scored and idle_minutes >= FORCE_REENTER_MINS:
        # Force re-enter original ticker regardless of quality
        orig = next((c for c in candidates if c.get("ticker") == original_ticker), None)
        if orig:
            result = opportunity_score(orig, freed_capital, nav,
                                       min_fisher=0, min_grade="D")
            if result:
                result["reason"] += " [FORCED RE-ENTRY — no alternatives found after 4h]"
                scored = [result]

    return scored


def explain_reinvestment_choice(winner: dict,
                                 original_ticker: str,
                                 runner_up: dict = None) -> str:
    """
    Build a human-readable explanation of the reinvestment decision.
    Used in the daily summary and trade log.
    """
    lines = []
    if winner["ticker"] == original_ticker:
        lines.append(
            f"Re-entering {original_ticker} (best score {winner['score']:.3f}). "
            f"{winner['contracts']}c × ${winner['strike']} strike, "
            f"~${winner['monthly_est']:.0f}/mo."
        )
    else:
        lines.append(
            f"Switching to {winner['ticker']} instead of re-entering {original_ticker}. "
            f"Score: {winner['score']:.3f} vs "
            f"{runner_up['score']:.3f if runner_up else 'N/A'}. "
            f"Reason: {winner['reason']}"
        )
    if runner_up and runner_up["ticker"] != winner["ticker"]:
        lines.append(
            f"Runner-up: {runner_up['ticker']} (score {runner_up['score']:.3f})"
        )
    return " ".join(lines)


# ── No-idle enforcement ───────────────────────────────────────────────────

def is_deployable_capital(buying_power: float, nav: float) -> bool:
    """True if there's enough idle capital worth deploying."""
    idle = buying_power - MIN_CASH_RESERVE
    return (idle >= MIN_DEPLOY_AMOUNT and
            idle / nav >= 0.08 if nav > 0 else False)


def is_friday_cutoff() -> bool:
    """True after 3 PM ET on Friday — don't open new positions."""
    from bot.time_utils import now_et
    now = now_et()
    return now.weekday() == 4 and now.time() >= FRIDAY_CUTOFF_ET


# ── Summary helper ────────────────────────────────────────────────────────

def print_rules_summary():
    print("─" * 50)
    print("  ETradeBot Trade Rules")
    print("─" * 50)
    print(f"  Max wheels:          {MAX_WHEELS}")
    print(f"  Max position size:   {MAX_POSITION_PCT*100:.0f}% of NAV per position")
    print(f"  Max deployed:        {MAX_DEPLOYED_PCT*100:.0f}% of NAV total")
    print(f"  Cash reserve:        ${MIN_CASH_RESERVE}")
    print(f"  Delta range:         {DELTA_RANGE[0]}–{DELTA_RANGE[1]}")
    print(f"  DTE range:           {DTE_RANGE[0]}–{DTE_RANGE[1]} days")
    print(f"  Profit target:       {PROFIT_TARGET_PCT*100:.0f}% premium decay")
    print(f"  Roll trigger:        {ROLL_DELTA_MULT}× initial delta or "
          f"<{DTE_ROLL_THRESHOLD} DTE with loss")
    print(f"  Earnings blackout:   {EARNINGS_BLACKOUT} days")
    print(f"  Min open interest:   {MIN_OPEN_INTEREST}")
    print(f"  Max spread:          {MAX_SPREAD_PCT*100:.0f}% of mid")
    print(f"  Min premium:         ${MIN_PREMIUM}")
    print("─" * 50)


# ── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print_rules_summary()

    # Test collateral check
    print("\n── Collateral checks ──")
    try:
        check_collateral(strike=5.0, contracts=1, nav=6500, buying_power=1800)
        print("  NOK $5 1c: PASS")
    except RuleViolation as e:
        print(f"  NOK $5 1c: FAIL — {e}")

    try:
        check_collateral(strike=43.0, contracts=1, nav=6500, buying_power=6200)
        print("  VZ $43 1c: PASS")
    except RuleViolation as e:
        print(f"  VZ $43 1c: FAIL — {e}")

    # Test opportunity scoring
    print("\n── Opportunity scoring ──")
    candidates = [
        {"ticker":"NOK", "csp_strike":5.0, "fisher_score":8,
         "wheel_grade":"C", "wheel_score":65, "iv_pct":28, "csp_expiry":"2026-07-17"},
        {"ticker":"SOFI","csp_strike":15.0,"fisher_score":7,
         "wheel_grade":"B", "wheel_score":72, "iv_pct":35, "csp_expiry":"2026-07-17"},
        {"ticker":"T",   "csp_strike":22.0,"fisher_score":9,
         "wheel_grade":"A", "wheel_score":100,"iv_pct":20, "csp_expiry":"2026-07-17"},
    ]
    ranked = rank_reinvestment_candidates(candidates,
                                           freed_capital=520,
                                           nav=6500,
                                           original_ticker="SOFI")
    print(f"  Freed: $520, NAV: $6,500")
    for i, r in enumerate(ranked):
        print(f"  #{i+1}: {r['ticker']} score={r['score']:.3f} "
              f"{r['contracts']}c deployed=${r['deployed']:,} "
              f"~${r['monthly_est']:.0f}/mo")

    print("\n── Roll check ──")
    should, reason = should_roll_position(0.52, 0.28, 10, -12.0)
    print(f"  Delta 0.52, initial 0.28, DTE 10: roll={should} ({reason})")

    print("\n── BTC target check ──")
    print(f"  Cost $0.57, current $0.28: hit={btc_target_hit(0.57, 0.28)}")
    print(f"  Cost $0.57, current $0.35: hit={btc_target_hit(0.57, 0.35)}")


# ---------------------------------------------------------------------------
# Tiered position sizing — NAV-aware concentration rules
# ---------------------------------------------------------------------------

def get_sizing_rules(nav: float) -> dict:
    """
    Return position sizing limits for the current NAV bracket.
    Reads from data/config.json position_sizing.tiers.
    Falls back to safe defaults if config is unavailable.
    """
    defaults = {"per_position_pct": 0.08, "per_ticker_pct": 0.12, "sector_max_pct": 0.30}
    try:
        import json, os
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "data", "config.json")
        with open(cfg_path) as f:
            tiers = json.load(f).get("position_sizing", {}).get("tiers", [])
        for tier in sorted(tiers, key=lambda t: t["nav_max"]):
            if nav <= tier["nav_max"]:
                return {
                    "per_position_pct": tier["per_position_pct"],
                    "per_ticker_pct":   tier["per_ticker_pct"],
                    "sector_max_pct":   tier["sector_max_pct"],
                }
    except Exception:
        pass
    return defaults


def get_sector(ticker: str) -> str:
    """Return sector for a ticker from config.json sector_map."""
    try:
        import json, os
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "data", "config.json")
        with open(cfg_path) as f:
            return json.load(f).get("sector_map", {}).get(ticker.upper(), "Other")
    except Exception:
        return "Other"


def validate_entry(
    ticker:          str,
    entry_dte:       int,
    earnings_date:   "str | None" = None,
    entry_delta:     "float | None" = None,
    is_liquid:       bool = True,
    iv_pct:          "float | None" = None,
) -> "tuple[bool, list[str], list[str]]":
    """
    Pre-entry validation: DTE minimum, earnings blackout, delta range, liquidity.
    Returns (allowed, errors, warnings).
    Separate from check_position_allowed (concentration) — run both before entry.
    """
    errors:   list = []
    warnings: list = []

    # ── Minimum entry DTE ─────────────────────────────────────────────────
    if entry_dte < MIN_ENTRY_DTE:
        errors.append(
            f"Entry DTE {entry_dte}d is below {MIN_ENTRY_DTE}d minimum — "
            f"21 DTE exit rule would fire on day {entry_dte - DTE_EXIT_THRESHOLD}"
        )

    # ── Earnings blackout ─────────────────────────────────────────────────
    if earnings_date:
        try:
            import datetime
            ed  = datetime.date.fromisoformat(str(earnings_date)[:10])
            import datetime as _dt_eb
            dte_to_earnings = (ed - _dt_eb.date.today()).days
            if 0 <= dte_to_earnings <= EARNINGS_BLACKOUT_NEW:
                errors.append(
                    f"{ticker} earnings in {dte_to_earnings}d ({earnings_date}) "
                    f"— no new entries within {EARNINGS_BLACKOUT_NEW}d of earnings"
                )
            elif EARNINGS_BLACKOUT_NEW < dte_to_earnings <= 21:
                warnings.append(
                    f"{ticker} earnings in {dte_to_earnings}d — "
                    f"position may be force-closed before 50% target"
                )
        except Exception:
            pass

    # ── Entry delta range ─────────────────────────────────────────────────
    if entry_delta is not None:
        if entry_delta < MAX_ENTRY_DELTA:   # e.g. -0.45 < -0.30 (deeper ITM)
            warnings.append(
                f"Entry delta {entry_delta:.2f} is deeper ITM than "
                f"{MAX_ENTRY_DELTA:.2f} guideline — higher assignment risk"
            )
        elif entry_delta > MIN_ENTRY_DELTA:   # e.g. -0.10 > -0.15 (too far OTM)
            warnings.append(
                f"Entry delta {entry_delta:.2f} is too far OTM — "
                f"premium may be insufficient relative to risk"
            )

    # ── Liquidity ─────────────────────────────────────────────────────────
    if not is_liquid:
        warnings.append(
            f"{ticker} options may be illiquid (low IV or small market cap) — "
            f"expect wider bid-ask, effective exit trigger reduced to {ILLIQUID_DTE_OVERRIDE} DTE"
        )

    # ── Low IV advisory ───────────────────────────────────────────────────
    if iv_pct is not None and iv_pct < 15:
        warnings.append(
            f"IV {iv_pct:.1f}% is very low — premium will be thin and "
            f"50% target may not be reached before {DTE_EXIT_THRESHOLD} DTE fires"
        )

    allowed = len(errors) == 0
    return allowed, errors, warnings


def check_position_allowed(
    ticker:            str,
    strike:            float,
    contracts:         int,
    nav:               float,
    current_positions: list,
    option_type:       str = "CSP",
) -> "tuple[bool, list[str], list[str]]":
    """
    Check whether a new position fits within tiered concentration rules.

    Returns:
        allowed (bool)     — True if no hard block
        errors  (list[str]) — hard violations (block the trade)
        warnings(list[str]) — soft flags (allow but caution)
    """
    if nav <= 0:
        return True, [], []

    rules      = get_sizing_rules(nav)
    sector     = get_sector(ticker)
    collateral = strike * 100 * contracts
    pos_pct    = collateral / nav
    errors:    list = []
    warnings:  list = []

    # ── 1. Per-position cap ───────────────────────────────────────────────
    pos_cap = rules["per_position_pct"]
    if pos_pct > pos_cap:
        max_c = max(1, int(nav * pos_cap / (strike * 100)))
        if max_c < contracts:
            errors.append(
                f"Position {pos_pct*100:.1f}% of NAV exceeds {pos_cap*100:.0f}% cap "
                f"(max {max_c}c at ${strike} — or reduce to fit)"
            )
        else:
            # Single contract minimum — can't go lower; flag as advisory
            warnings.append(
                f"1-contract minimum at ${strike} = {pos_pct*100:.1f}% NAV "
                f"(above {pos_cap*100:.0f}% cap — unavoidable at this account size)"
            )

    # ── 2. Per-ticker cap (total exposure this ticker) ────────────────────
    existing_coll = sum(
        p.get("strike", 0) * 100 * p.get("contracts", 0)
        for p in current_positions
        if p.get("ticker","").upper() == ticker.upper()
        and p.get("type","") in ("CSP","CC")
    )
    ticker_pct = (existing_coll + collateral) / nav
    ticker_cap = rules["per_ticker_pct"]
    if existing_coll > 0 and ticker_pct > ticker_cap:
        errors.append(
            f"Adding {contracts}c brings {ticker} ticker total to "
            f"{ticker_pct*100:.1f}% NAV (cap {ticker_cap*100:.0f}%)"
        )

    # ── 3. Sector cap ─────────────────────────────────────────────────────
    sector_coll = sum(
        p.get("strike", 0) * 100 * p.get("contracts", 0)
        for p in current_positions
        if get_sector(p.get("ticker","")) == sector
        and p.get("type","") in ("CSP","CC")
    ) + collateral
    sector_pct = sector_coll / nav
    sector_cap = rules["sector_max_pct"]
    if sector_pct > sector_cap:
        if sector_coll - collateral >= sector_cap * nav:
            # Sector was already over cap before this position
            warnings.append(
                f"{sector} sector already at {(sector_coll-collateral)/nav*100:.1f}% NAV "
                f"(cap {sector_cap*100:.0f}%) — adding more increases concentration"
            )
        else:
            errors.append(
                f"Adding {ticker} would bring {sector} sector to "
                f"{sector_pct*100:.1f}% NAV (cap {sector_cap*100:.0f}%)"
            )

    allowed = len(errors) == 0
    return allowed, errors, warnings


def concentration_summary(positions: list, nav: float) -> dict:
    """
    Return a full concentration summary for the advisor prompt.
    Shows per-ticker and per-sector exposure with cap status.
    """
    if nav <= 0:
        return {}

    rules = get_sizing_rules(nav)
    # Per-ticker
    ticker_coll: dict = {}
    for p in positions:
        if p.get("type","") not in ("CSP","CC"):
            continue
        t = p.get("ticker","")
        ticker_coll[t] = ticker_coll.get(t, 0) + p.get("strike",0)*100*p.get("contracts",0)
    # Per-sector
    sector_coll: dict = {}
    for t, coll in ticker_coll.items():
        s = get_sector(t)
        sector_coll[s] = sector_coll.get(s, 0) + coll

    return {
        "rules":        rules,
        "nav":          nav,
        "per_ticker":   {t: {"collateral":c, "pct":round(c/nav*100,1),
                             "over_cap": c/nav > rules["per_ticker_pct"]}
                         for t, c in ticker_coll.items()},
        "per_sector":   {s: {"collateral":c, "pct":round(c/nav*100,1),
                             "over_cap": c/nav > rules["sector_max_pct"]}
                         for s, c in sector_coll.items()},
        "total_deployed": round(sum(ticker_coll.values()) / nav * 100, 1),
    }
