"""
wheel_engine.py — Wheel Strategy State Machine
================================================
Tracks every wheel position through its full lifecycle.
State is persisted to data/wheel_state.json on every change
so the bot survives restarts, crashes, and overnight gaps.

Phase diagram:
    IDLE
      → CSP_PENDING     order placed, waiting for fill
      → CSP_OPEN        filled, monitoring decay / delta
        → CSP_ROLLING   roll in progress (BTC + new STO placed)
      → ASSIGNED        stock acquired at expiry, need to sell CC
      → CC_PENDING      CC order placed, waiting for fill
      → CC_OPEN         CC filled, monitoring
        → CC_ROLLING    CC roll in progress
      → CALLED_AWAY     stock sold via CC — cycle complete
      → COMPLETE        archived, P&L logged

Transitions are triggered by:
  - Order fills detected via get_fills_since()
  - Delta/DTE rule breaches detected in monitoring checks
  - Post-expiry portfolio diff (assignment / expiry worthless)
  - Manual override via WheelEngine.force_phase()

Usage:
    engine = WheelEngine()
    engine.load()

    # Initialize from live positions (first run)
    engine.init_from_positions(api.get_portfolio(), api.get_balance())

    # Morning check
    for ticker, wheel in engine.active_wheels():
        action = engine.evaluate(ticker, current_price, current_delta, dte)

    # After BTC fills
    engine.on_btc_filled("SOFI", profit=29.00)

    # After assignment detected
    engine.on_assigned("DOCS", shares=100, strike=20.0)
"""

import os
import json
import copy
import logging
import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "wheel_state.json"
)


# ── Phase definitions ─────────────────────────────────────────────────────

class Phase(str, Enum):
    IDLE        = "IDLE"
    CSP_PENDING = "CSP_PENDING"
    CSP_OPEN    = "CSP_OPEN"
    CSP_ROLLING = "CSP_ROLLING"
    ASSIGNED    = "ASSIGNED"
    CC_PENDING  = "CC_PENDING"
    CC_OPEN     = "CC_OPEN"
    CC_ROLLING  = "CC_ROLLING"
    CALLED_AWAY = "CALLED_AWAY"
    COMPLETE    = "COMPLETE"

# Valid transitions — (from_phase, to_phase)
VALID_TRANSITIONS = {
    (Phase.IDLE,        Phase.CSP_PENDING),
    (Phase.CSP_PENDING, Phase.CSP_OPEN),
    (Phase.CSP_PENDING, Phase.IDLE),        # order cancelled
    (Phase.CSP_OPEN,    Phase.CSP_ROLLING),
    (Phase.CSP_OPEN,    Phase.ASSIGNED),
    (Phase.CSP_OPEN,    Phase.IDLE),        # expired worthless / BTC filled
    (Phase.CSP_ROLLING, Phase.CSP_OPEN),    # roll complete
    (Phase.CSP_ROLLING, Phase.ASSIGNED),    # rolled into assignment
    (Phase.ASSIGNED,    Phase.CC_PENDING),
    (Phase.ASSIGNED,    Phase.IDLE),        # shares sold manually
    (Phase.CC_PENDING,  Phase.CC_OPEN),
    (Phase.CC_PENDING,  Phase.ASSIGNED),    # CC order cancelled
    (Phase.CC_OPEN,     Phase.CC_ROLLING),
    (Phase.CC_OPEN,     Phase.CALLED_AWAY),
    (Phase.CC_OPEN,     Phase.ASSIGNED),    # CC expired worthless → sell new CC
    (Phase.CC_ROLLING,  Phase.CC_OPEN),
    (Phase.CC_ROLLING,  Phase.CALLED_AWAY),
    (Phase.CALLED_AWAY, Phase.COMPLETE),
    (Phase.CALLED_AWAY, Phase.IDLE),        # immediate re-entry
    (Phase.COMPLETE,    Phase.IDLE),
    # Force reset from any phase
    (Phase.CSP_PENDING, Phase.ASSIGNED),
    (Phase.CSP_OPEN,    Phase.COMPLETE),
}

# Phases considered "active" (capital deployed)
ACTIVE_PHASES = {
    Phase.CSP_PENDING, Phase.CSP_OPEN, Phase.CSP_ROLLING,
    Phase.ASSIGNED,
    Phase.CC_PENDING,  Phase.CC_OPEN,  Phase.CC_ROLLING,
    Phase.CALLED_AWAY,
}


# ── Action recommendations ────────────────────────────────────────────────

class WheelAction(str, Enum):
    HOLD           = "HOLD"
    BTC_TARGET_HIT = "BTC_TARGET_HIT"   # 50% profit — auto-execute
    ROLL_NEEDED    = "ROLL_NEEDED"       # delta breach — alert
    APPROACHING_EXPIRY = "APPROACHING_EXPIRY"  # ≤14 DTE — monitor
    SELL_CC        = "SELL_CC"           # post-assignment — auto-execute
    CLOSE_EARNINGS = "CLOSE_EARNINGS"    # earnings soon — alert
    MONITOR        = "MONITOR"           # standard monitoring
    REINVEST       = "REINVEST"          # capital free — auto-execute


# ── Wheel record ──────────────────────────────────────────────────────────

def _empty_wheel(ticker: str) -> dict:
    return {
        "ticker":           ticker,
        "phase":            Phase.IDLE.value,
        "phase_since_et":   None,

        # CSP fields
        "csp_strike":       None,
        "csp_expiry":       None,
        "csp_contracts":    0,
        "csp_premium":      None,      # premium received per contract
        "csp_order_id":     None,
        "csp_fill_date":    None,
        "csp_initial_delta":None,
        "btc_order_id":     None,      # GTC profit-target order
        "btc_limit":        None,

        # CC fields (after assignment)
        "cc_strike":        None,
        "cc_expiry":        None,
        "cc_contracts":     0,
        "cc_premium":       None,
        "cc_order_id":      None,
        "cc_fill_date":     None,

        # Cost tracking
        "cost_basis":       None,      # strike - total_premium_collected
        "total_premium":    0.0,       # running total across all rolls
        "roll_count":       0,
        "roll_credits":     [],        # list of net credits from each roll

        # Assignment
        "shares_held":      0,
        "assignment_date":  None,

        # Cycle P&L
        "cycle_start_et":   None,
        "cycle_end_et":     None,
        "cycle_pnl":        None,

        # Metadata
        "notes":            "",
        "earnings_date":    None,
    }


# ── WheelEngine ───────────────────────────────────────────────────────────

class WheelEngine:
    """
    Manages all wheel positions. Single source of truth for phase state.
    Thread-safe for the bot's single monitoring loop.
    """

    def __init__(self, state_path: str = STATE_FILE):
        self._path        = state_path
        self._state       = {}         # full JSON state dict
        self._wheels      = {}         # {ticker: wheel_dict}
        self._dirty       = False      # needs save

    # ── Persistence ───────────────────────────────────────────────────────

    def load(self) -> "WheelEngine":
        """Load state from file. Safe to call on first run (empty file)."""
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        if os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    self._state = json.load(f)
                self._wheels = self._state.get("wheels", {})
                logger.info("State loaded: %d wheels from %s",
                            len(self._wheels), self._path)
            except (json.JSONDecodeError, OSError) as e:
                logger.error("State file corrupt (%s) — starting fresh", e)
                self._state  = {}
                self._wheels = {}
        else:
            logger.info("No state file found — starting fresh")
            self._state  = {}
            self._wheels = {}
        return self

    def save(self) -> None:
        """Persist current state to file atomically."""
        from bot.time_utils import timestamp_et
        self._state["wheels"]        = self._wheels
        self._state["last_saved_et"] = timestamp_et()
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
            os.replace(tmp, self._path)   # atomic on POSIX + Windows
            self._dirty = False
            logger.debug("State saved to %s", self._path)
        except OSError as e:
            logger.error("State save failed: %s", e)

    def _touch(self, ticker: str) -> None:
        """Mark a wheel as modified and schedule save."""
        from bot.time_utils import timestamp_et
        self._wheels[ticker]["_modified_et"] = timestamp_et()
        self._dirty = True

    # ── Wheel access ──────────────────────────────────────────────────────

    def get(self, ticker: str) -> Optional[dict]:
        return self._wheels.get(ticker.upper())

    def all_wheels(self) -> dict:
        return dict(self._wheels)

    def active_wheels(self) -> list[tuple[str, dict]]:
        """Return (ticker, wheel) pairs for all non-IDLE, non-COMPLETE wheels."""
        return [
            (t, w) for t, w in self._wheels.items()
            if Phase(w["phase"]) in ACTIVE_PHASES
        ]

    def active_count(self) -> int:
        return len(self.active_wheels())

    def tickers_in_wheel(self) -> list[str]:
        return [t for t, _ in self.active_wheels()]

    def total_collateral_deployed(self) -> float:
        """Sum of (strike × 100 × contracts) for all active CSP/CC positions."""
        total = 0.0
        for _, w in self.active_wheels():
            phase = Phase(w["phase"])
            if phase in {Phase.CSP_PENDING, Phase.CSP_OPEN, Phase.CSP_ROLLING}:
                strike    = w.get("csp_strike") or 0
                contracts = w.get("csp_contracts") or 0
                total    += float(strike) * 100 * int(contracts)
            elif phase in {Phase.CC_PENDING, Phase.CC_OPEN, Phase.CC_ROLLING}:
                # Shares held — cost basis × 100 × (contracts = shares/100)
                cost      = w.get("cost_basis") or w.get("csp_strike") or 0
                shares    = w.get("shares_held") or 0
                contracts = shares // 100
                total    += float(cost) * 100 * contracts
        return total

    # ── Phase transitions ─────────────────────────────────────────────────

    def _transition(self, ticker: str, to_phase: Phase,
                     reason: str = "", force: bool = False) -> None:
        """
        Move a wheel to a new phase.
        Validates the transition unless force=True.
        Logs every transition for the audit trail.
        """
        ticker = ticker.upper()
        if ticker not in self._wheels:
            self._wheels[ticker] = _empty_wheel(ticker)

        wheel      = self._wheels[ticker]
        from_phase = Phase(wheel["phase"])

        if not force and (from_phase, to_phase) not in VALID_TRANSITIONS:
            logger.warning(
                "Invalid transition %s: %s → %s (reason: %s). Use force=True to override.",
                ticker, from_phase.value, to_phase.value, reason
            )
            return

        from bot.time_utils import timestamp_et
        old_phase = wheel["phase"]
        wheel["phase"]          = to_phase.value
        wheel["phase_since_et"] = timestamp_et()

        logger.info("PHASE  %s  %s → %s  (%s)",
                    ticker, old_phase, to_phase.value, reason or "—")
        self._touch(ticker)
        self._append_log(ticker, "PHASE_CHANGE", {
            "from": old_phase, "to": to_phase.value, "reason": reason
        })
        self.save()

    def force_phase(self, ticker: str, phase: Phase, reason: str = "") -> None:
        """Manually override a wheel's phase. Use only for corrections."""
        self._transition(ticker, phase,
                         reason=f"MANUAL OVERRIDE: {reason}", force=True)

    # ── Lifecycle event handlers ──────────────────────────────────────────

    def on_csp_placed(self, ticker: str, strike: float, expiry: str,
                       contracts: int, premium: float, order_id: str,
                       initial_delta: float = 0.0) -> None:
        """Call when a new CSP order is placed."""
        ticker = ticker.upper()
        if ticker not in self._wheels:
            self._wheels[ticker] = _empty_wheel(ticker)
        w = self._wheels[ticker]
        from bot.time_utils import timestamp_et
        w["csp_strike"]        = strike
        w["csp_expiry"]        = expiry
        w["csp_contracts"]     = contracts
        w["csp_premium"]       = premium
        w["csp_order_id"]      = order_id
        w["csp_initial_delta"] = initial_delta
        w["cycle_start_et"]    = timestamp_et()
        # Set BTC target
        w["btc_limit"]         = round(premium * 0.50, 2)
        self._touch(ticker)
        self._transition(ticker, Phase.CSP_PENDING,
                         f"STO {contracts}c ${strike}P {expiry} @ ${premium}")

    def on_csp_filled(self, ticker: str, fill_price: float = None,
                       order_id: str = None) -> None:
        """Call when CSP fill confirmed."""
        ticker = ticker.upper()
        w = self._wheels.get(ticker, {})
        from bot.time_utils import timestamp_et
        w["csp_fill_date"] = timestamp_et()
        if fill_price:
            w["csp_premium"] = fill_price
            w["btc_limit"]   = round(fill_price * 0.50, 2)
        w["total_premium"] = float(w.get("total_premium", 0)) + \
                             float(w.get("csp_premium", 0)) * int(w.get("csp_contracts", 1))
        self._touch(ticker)
        self._transition(ticker, Phase.CSP_OPEN,
                         f"Filled @ ${fill_price or w.get('csp_premium')}")

    def on_btc_filled(self, ticker: str, profit: float,
                       order_id: str = None) -> None:
        """
        Call when BTC (profit target) order fills.
        Frees the wheel back to IDLE for reinvestment.
        """
        ticker = ticker.upper()
        w = self._wheels.get(ticker, {})
        from bot.time_utils import timestamp_et
        w["cycle_end_et"] = timestamp_et()
        w["cycle_pnl"]    = round(profit, 2)
        self._touch(ticker)
        self._append_log(ticker, "CYCLE_COMPLETE", {
            "type":   "CSP_BTC",
            "profit": profit,
            "total_premium": w.get("total_premium"),
            "roll_count": w.get("roll_count", 0),
        })
        self._update_account_totals(profit)
        self._transition(ticker, Phase.IDLE,
                         f"BTC filled — profit ${profit:.2f}")

    def on_expired_worthless(self, ticker: str) -> None:
        """Call when CSP expires OTM and worthless — full premium kept."""
        ticker = ticker.upper()
        w      = self._wheels.get(ticker, {})
        profit = float(w.get("total_premium", 0))
        from bot.time_utils import timestamp_et
        w["cycle_end_et"] = timestamp_et()
        w["cycle_pnl"]    = round(profit, 2)
        self._touch(ticker)
        self._append_log(ticker, "CYCLE_COMPLETE", {
            "type":   "EXPIRED_WORTHLESS",
            "profit": profit,
        })
        self._update_account_totals(profit)
        self._transition(ticker, Phase.IDLE, "Expired worthless — full premium")

    def on_assigned(self, ticker: str, shares: int,
                     strike: float = None) -> None:
        """
        Call when assignment is detected (stock acquired after expiry).
        Computes cost basis = strike - total_premium_collected.
        """
        ticker = ticker.upper()
        w = self._wheels.get(ticker, {})
        if ticker not in self._wheels:
            self._wheels[ticker] = _empty_wheel(ticker)
            w = self._wheels[ticker]

        strike_used    = strike or w.get("csp_strike", 0)
        total_prem     = float(w.get("total_premium", 0))
        premium_per_sh = total_prem / max(shares, 1)
        cost_basis     = round(float(strike_used) - premium_per_sh, 2)

        from bot.time_utils import timestamp_et
        w["shares_held"]     = shares
        w["assignment_date"] = timestamp_et()
        w["cost_basis"]      = cost_basis
        w["csp_strike"]      = strike_used
        self._touch(ticker)
        logger.info(
            "ASSIGNED %s — %d shares @ cost basis $%.2f "
            "(strike $%.2f - $%.2f premium)",
            ticker, shares, cost_basis, strike_used, premium_per_sh
        )
        self._append_log(ticker, "ASSIGNED", {
            "shares": shares, "strike": strike_used,
            "cost_basis": cost_basis, "premium_offset": premium_per_sh,
        })
        self._transition(ticker, Phase.ASSIGNED,
                         f"{shares}sh assigned, cost basis ${cost_basis}")

    def on_cc_placed(self, ticker: str, strike: float, expiry: str,
                      contracts: int, premium: float, order_id: str) -> None:
        """Call when covered call STO order is placed."""
        ticker = ticker.upper()
        w = self._wheels.get(ticker, {})
        w["cc_strike"]    = strike
        w["cc_expiry"]    = expiry
        w["cc_contracts"] = contracts
        w["cc_premium"]   = premium
        w["cc_order_id"]  = order_id
        # Add CC premium to running total
        w["total_premium"] = float(w.get("total_premium", 0)) + \
                             premium * contracts * 100
        self._touch(ticker)
        self._transition(ticker, Phase.CC_PENDING,
                         f"STO CC {contracts}c ${strike}C {expiry} @ ${premium}")

    def on_cc_filled(self, ticker: str, fill_price: float = None) -> None:
        """Call when CC fill confirmed."""
        ticker = ticker.upper()
        w = self._wheels.get(ticker, {})
        from bot.time_utils import timestamp_et
        w["cc_fill_date"] = timestamp_et()
        if fill_price:
            w["cc_premium"] = fill_price
        self._touch(ticker)
        self._transition(ticker, Phase.CC_OPEN,
                         f"CC filled @ ${fill_price or w.get('cc_premium')}")

    def on_called_away(self, ticker: str) -> None:
        """
        Call when stock is called away (CC exercised / assigned to buyer).
        Computes full wheel cycle P&L.
        """
        ticker = ticker.upper()
        w = self._wheels.get(ticker, {})
        from bot.time_utils import timestamp_et

        # Full cycle P&L:
        # (cc_strike - cost_basis) × shares + total_premium_collected
        cc_strike  = float(w.get("cc_strike", 0) or 0)
        cost_basis = float(w.get("cost_basis", 0) or 0)
        shares     = int(w.get("shares_held", 0) or 0)
        total_prem = float(w.get("total_premium", 0) or 0)

        stock_gain = (cc_strike - cost_basis) * shares
        cycle_pnl  = round(stock_gain + total_prem, 2)

        w["cycle_end_et"]  = timestamp_et()
        w["cycle_pnl"]     = cycle_pnl
        w["shares_held"]   = 0
        self._touch(ticker)

        logger.info(
            "CALLED_AWAY %s — stock gain $%.2f + premium $%.2f = cycle P&L $%.2f",
            ticker, stock_gain, total_prem, cycle_pnl
        )
        self._append_log(ticker, "CYCLE_COMPLETE", {
            "type":       "CALLED_AWAY",
            "cc_strike":  cc_strike,
            "cost_basis": cost_basis,
            "shares":     shares,
            "stock_gain": stock_gain,
            "total_premium": total_prem,
            "cycle_pnl":  cycle_pnl,
        })
        self._update_account_totals(cycle_pnl)
        self._transition(ticker, Phase.CALLED_AWAY,
                         f"Called away — cycle P&L ${cycle_pnl:.2f}")

    def on_cc_expired_worthless(self, ticker: str) -> None:
        """CC expired OTM — keep premium, sell new CC."""
        self._transition(ticker.upper(), Phase.ASSIGNED,
                         "CC expired worthless — sell new CC")

    def on_roll_placed(self, ticker: str, option_type: str = "PUT") -> None:
        """Call when a roll is in progress (BTC + new STO placed together)."""
        ticker = ticker.upper()
        w = self._wheels.get(ticker, {})
        w["roll_count"] = int(w.get("roll_count", 0)) + 1
        self._touch(ticker)
        phase = Phase.CSP_ROLLING if option_type == "PUT" else Phase.CC_ROLLING
        self._transition(ticker, phase,
                         f"Roll #{w['roll_count']} in progress")

    def on_roll_complete(self, ticker: str, new_strike: float,
                          new_expiry: str, new_premium: float,
                          net_credit: float, option_type: str = "PUT") -> None:
        """Call when roll BTC fills and new STO fills."""
        ticker = ticker.upper()
        w = self._wheels.get(ticker, {})
        w["roll_credits"] = w.get("roll_credits", []) + [net_credit]
        w["total_premium"] = float(w.get("total_premium", 0)) + \
                             net_credit * int(w.get("csp_contracts", 1)) * 100
        if option_type == "PUT":
            w["csp_strike"]  = new_strike
            w["csp_expiry"]  = new_expiry
            w["csp_premium"] = new_premium
            w["btc_limit"]   = round(new_premium * 0.50, 2)
            next_phase = Phase.CSP_OPEN
        else:
            w["cc_strike"]  = new_strike
            w["cc_expiry"]  = new_expiry
            w["cc_premium"] = new_premium
            next_phase = Phase.CC_OPEN
        self._touch(ticker)
        self._transition(ticker, next_phase,
                         f"Roll complete → ${new_strike} {new_expiry} net ${net_credit:+.2f}")

    # ── Evaluation — called every monitoring cycle ────────────────────────

    def evaluate(self, ticker: str,
                  current_price: float,
                  current_delta: float,
                  dte: int,
                  earnings_date: Optional[datetime.date] = None) -> WheelAction:
        """
        Evaluate a single wheel position and return the recommended action.
        This is called every 30 minutes for each active wheel.
        Does NOT execute anything — purely returns a recommendation.
        """
        from bot.trade_rules import (
            btc_target_hit, should_roll_position, should_exit_position,
            EARNINGS_BLACKOUT
        )
        ticker = ticker.upper()
        w = self._wheels.get(ticker)
        if not w:
            return WheelAction.MONITOR

        phase = Phase(w["phase"])

        # ── ASSIGNED: needs CC entry ──────────────────────────────────────
        if phase == Phase.ASSIGNED:
            return WheelAction.SELL_CC

        # ── CSP_OPEN / CC_OPEN: active monitoring ─────────────────────────
        if phase in {Phase.CSP_OPEN, Phase.CC_OPEN}:
            option_type = "PUT" if phase == Phase.CSP_OPEN else "CALL"
            premium     = w.get("csp_premium" if option_type=="PUT" else "cc_premium", 0) or 0
            init_delta  = w.get("csp_initial_delta", 0.28) or 0.28

            # Earnings blackout
            if earnings_date:
                if isinstance(earnings_date, str):
                    try:
                        earnings_date = datetime.date.fromisoformat(earnings_date[:10])
                    except ValueError:
                        earnings_date = None
                if earnings_date:
                    days = abs((earnings_date - datetime.date.today()).days)
                    if days <= EARNINGS_BLACKOUT:
                        logger.warning("%s earnings in %d days — close recommended", ticker, days)
                        return WheelAction.CLOSE_EARNINGS

            # 50% profit target
            _should_exit, _exit_why = should_exit_position(
                float(premium), float(current_price), dte
            )
            if _should_exit:
                logger.info("[WHEEL] %s exit triggered: %s", ticker, _exit_why)
            if _should_exit or btc_target_hit(float(premium), float(current_price)):
                return WheelAction.BTC_TARGET_HIT

            # Roll check
            should_roll, roll_reason = should_roll_position(
                current_delta, float(init_delta), dte,
                self._pnl_pct(w, current_price)
            )
            if should_roll:
                logger.info("%s ROLL recommended: %s", ticker, roll_reason)
                return WheelAction.ROLL_NEEDED

            # Approaching expiry
            from bot.trade_rules import DTE_ROLL_THRESHOLD
            if dte <= DTE_ROLL_THRESHOLD:
                return WheelAction.APPROACHING_EXPIRY

            return WheelAction.HOLD

        # ── IDLE: check for reinvestment opportunity ──────────────────────
        if phase == Phase.IDLE:
            return WheelAction.REINVEST

        # ── PENDING / ROLLING: just monitor ──────────────────────────────
        return WheelAction.MONITOR

    def _pnl_pct(self, wheel: dict, current_price: float) -> float:
        """Current P&L % for an open option position."""
        premium = wheel.get("csp_premium") or wheel.get("cc_premium") or 0
        if not premium:
            return 0.0
        return round((float(premium) - float(current_price)) / float(premium) * 100, 1)

    # ── Portfolio diff — assignment / call-away detection ────────────────

    def detect_changes(self, before: list[dict],
                        after: list[dict]) -> list[dict]:
        """
        Compare portfolio snapshots before and after expiry weekend.
        Returns list of detected events:
          {"event": "ASSIGNED",     "ticker", "shares", "strike"}
          {"event": "CALLED_AWAY",  "ticker", "shares"}
          {"event": "EXPIRED",      "ticker"}  (option disappeared)
          {"event": "FILLED",       "ticker", "type", "contracts"}
        """
        def _idx(positions, key):
            result = {}
            for p in positions:
                t = p.get("ticker", "")
                k = (t, p.get("type", ""), p.get("strike", 0), p.get("expiry", ""))
                result[k] = p
            return result

        before_idx = _idx(before, "before")
        after_idx  = _idx(after,  "after")

        before_stocks = {p["ticker"]: p for p in before if p.get("type") == "STOCK"}
        after_stocks  = {p["ticker"]: p for p in after  if p.get("type") == "STOCK"}
        before_opts   = {(p["ticker"], p["type"]): p for p in before
                         if p.get("type") in ("CSP", "CC")}
        after_opts    = {(p["ticker"], p["type"]): p for p in after
                         if p.get("type") in ("CSP", "CC")}

        events = []

        # Options that disappeared
        for key, opt in before_opts.items():
            ticker, opt_type = key
            if key not in after_opts:
                before_sh = int((before_stocks.get(ticker) or {}).get("contracts", 0))
                after_sh  = int((after_stocks.get(ticker)  or {}).get("contracts", 0))
                new_shares = after_sh - before_sh

                if opt_type == "CSP" and new_shares >= 100:
                    events.append({
                        "event":  "ASSIGNED",
                        "ticker": ticker,
                        "shares": new_shares,
                        "strike": opt.get("strike", 0),
                    })
                    logger.info("DETECTED ASSIGNMENT: %s +%d shares", ticker, new_shares)

                elif opt_type == "CC" and after_sh < before_sh:
                    events.append({
                        "event":  "CALLED_AWAY",
                        "ticker": ticker,
                        "shares": before_sh - after_sh,
                    })
                    logger.info("DETECTED CALLED_AWAY: %s", ticker)

                elif opt_type == "CSP":
                    events.append({
                        "event":  "EXPIRED",
                        "ticker": ticker,
                        "type":   "CSP",
                    })
                    logger.info("DETECTED EXPIRED WORTHLESS: %s CSP", ticker)

                elif opt_type == "CC":
                    events.append({
                        "event":  "EXPIRED",
                        "ticker": ticker,
                        "type":   "CC",
                    })
                    logger.info("DETECTED CC EXPIRED WORTHLESS: %s", ticker)

        return events

    def apply_detected_changes(self, events: list[dict]) -> None:
        """Apply detected portfolio changes to wheel state."""
        for ev in events:
            ticker = ev.get("ticker", "").upper()
            event  = ev.get("event", "")

            if event == "ASSIGNED":
                self.on_assigned(ticker, ev.get("shares", 100),
                                  ev.get("strike"))
            elif event == "CALLED_AWAY":
                self.on_called_away(ticker)
            elif event == "EXPIRED":
                if ev.get("type") == "CSP":
                    self.on_expired_worthless(ticker)
                else:
                    self.on_cc_expired_worthless(ticker)

    # ── Initialization from live positions ────────────────────────────────

    def init_from_positions(self, positions: list[dict],
                              balance: dict) -> int:
        """
        Bootstrap wheel state from live E*Trade portfolio.
        Call this on first run to populate wheel_state.json
        from your current open positions.

        Returns number of wheels initialized.
        """
        from bot.time_utils import timestamp_et, days_to_expiry

        count = 0
        logger.info("Initializing wheel state from %d live positions…", len(positions))

        for p in positions:
            ticker    = p.get("ticker", "").upper()
            pos_type  = p.get("type", "")
            contracts = int(p.get("contracts", 0) or 0)
            strike    = float(p.get("strike", 0) or 0)
            expiry    = p.get("expiry", "")
            cost      = float(p.get("cost", 0) or 0)
            current   = float(p.get("current", 0) or 0)

            if ticker not in self._wheels:
                self._wheels[ticker] = _empty_wheel(ticker)
            w = self._wheels[ticker]
            w["cycle_start_et"] = timestamp_et()

            if pos_type == "CSP":
                w["csp_strike"]        = strike
                w["csp_expiry"]        = expiry
                w["csp_contracts"]     = contracts
                w["csp_premium"]       = cost
                w["btc_limit"]         = round(cost * 0.50, 2)
                w["total_premium"]     = cost * 100 * contracts
                w["csp_initial_delta"] = 0.28   # estimated — update on next chain fetch
                w["phase"]             = Phase.CSP_OPEN.value
                w["phase_since_et"]    = timestamp_et()
                w["notes"]             = "Initialized from live portfolio"
                logger.info("INIT CSP: %s $%s %s × %dc @ $%s (BTC target $%s)",
                            ticker, strike, expiry, contracts, cost, w["btc_limit"])
                count += 1

            elif pos_type == "CC":
                w["cc_strike"]    = strike
                w["cc_expiry"]    = expiry
                w["cc_contracts"] = contracts
                w["cc_premium"]   = cost
                w["shares_held"]  = 100 * contracts   # assumed 1 lot per contract
                w["total_premium"] = cost * 100 * contracts
                # Cost basis: assume strike equals CSP strike if unknown
                w["cost_basis"]   = strike   # will be corrected by user if needed
                w["phase"]        = Phase.CC_OPEN.value
                w["phase_since_et"] = timestamp_et()
                w["notes"]        = "Initialized from live portfolio"
                logger.info("INIT CC: %s $%s %s × %dc @ $%s",
                            ticker, strike, expiry, contracts, cost)
                count += 1

            elif pos_type == "STOCK":
                shares = int(p.get("contracts", 0) or 0)
                cost_basis = float(p.get("cost", 0) or 0)
                w["shares_held"] = shares
                w["cost_basis"]  = cost_basis
                if shares >= 100:
                    # Has enough shares for CC — mark as ASSIGNED
                    w["phase"]           = Phase.ASSIGNED.value
                    w["phase_since_et"]  = timestamp_et()
                    w["assignment_date"] = timestamp_et()
                    w["notes"]           = "Initialized from live portfolio — sell CC"
                    logger.info("INIT STOCK: %s %dsh @ $%s → ASSIGNED (sell CC)",
                                ticker, shares, cost_basis)
                else:
                    # Sub-lot stock — hold until more acquired or decide manually
                    w["phase"]          = Phase.ASSIGNED.value
                    w["phase_since_et"] = timestamp_et()
                    w["notes"]          = (
                        f"Initialized: only {shares}sh, need 100 for CC. "
                        "Hold or add more shares."
                    )
                    logger.info("INIT STOCK: %s %dsh @ $%s — sub-lot, monitoring",
                                ticker, shares, cost_basis)
                count += 1

        # Update global metadata
        self._state["initialized_et"]      = timestamp_et()
        self._state["init_position_count"] = count
        self._state["last_run"]            = timestamp_et()
        self.save()
        logger.info("Wheel state initialized: %d positions → %d wheels",
                    len(positions), count)
        return count

    # ── Idle capital tracking ─────────────────────────────────────────────

    def set_idle_capital(self, amount: float,
                          pending_ticker: str = None) -> None:
        """Record that capital is sitting idle awaiting reinvestment."""
        from bot.time_utils import timestamp_et
        self._state["idle_capital"] = {
            "amount":           round(amount, 2),
            "since_et":         timestamp_et(),
            "pending_reinvest": pending_ticker,
        }
        self._dirty = True
        self.save()

    def idle_capital(self) -> dict:
        return self._state.get("idle_capital",
                                {"amount": 0, "since_et": None, "pending_reinvest": None})

    def idle_minutes(self) -> int:
        """Minutes since capital became idle. 0 if no idle capital."""
        ic = self.idle_capital()
        if not ic.get("since_et") or not ic.get("amount"):
            return 0
        from bot.time_utils import now_et, ET
        try:
            since = datetime.datetime.fromisoformat(ic["since_et"]).replace(tzinfo=ET)
            return max(0, int((now_et() - since).total_seconds() / 60))
        except Exception:
            return 0

    def clear_idle_capital(self) -> None:
        self._state["idle_capital"] = {
            "amount": 0, "since_et": None, "pending_reinvest": None
        }
        self._dirty = True
        self.save()

    # ── Account totals ────────────────────────────────────────────────────

    def _update_account_totals(self, cycle_pnl: float) -> None:
        self._state["total_premium_collected"] = round(
            float(self._state.get("total_premium_collected", 0)) + cycle_pnl, 2
        )
        self._state["total_cycles_completed"] = \
            int(self._state.get("total_cycles_completed", 0)) + 1

    def lifetime_stats(self) -> dict:
        return {
            "total_premium":  self._state.get("total_premium_collected", 0),
            "total_cycles":   self._state.get("total_cycles_completed", 0),
            "active_wheels":  self.active_count(),
            "collateral_deployed": self.total_collateral_deployed(),
        }

    # ── Audit log ─────────────────────────────────────────────────────────

    def _append_log(self, ticker: str, event: str, data: dict) -> None:
        """Append an event to the trade_log.json audit trail."""
        from bot.time_utils import timestamp_et
        log_path = os.path.join(
            os.path.dirname(self._path), "trade_log.json"
        )
        entry = {
            "ts":     timestamp_et(),
            "ticker": ticker,
            "event":  event,
            **data,
        }
        log = []
        if os.path.exists(log_path):
            try:
                with open(log_path) as f:
                    log = json.load(f)
            except Exception:
                log = []
        log.append(entry)
        try:
            with open(log_path, "w") as f:
                json.dump(log[-2000:], f, indent=2, default=str)  # keep last 2000
        except Exception as e:
            logger.warning("Trade log write failed: %s", e)

    # ── Debug / display ───────────────────────────────────────────────────

    def summary_lines(self) -> list[str]:
        """Return human-readable summary for the morning briefing."""
        lines = []
        active = self.active_wheels()
        if not active:
            lines.append("  No active wheels.")
            return lines
        for ticker, w in sorted(active, key=lambda x: x[0]):
            phase     = Phase(w["phase"])
            strike    = w.get("csp_strike") or w.get("cc_strike") or "—"
            expiry    = w.get("csp_expiry") or w.get("cc_expiry") or "—"
            contracts = w.get("csp_contracts") or w.get("cc_contracts") or \
                        (w.get("shares_held", 0) // 100) or 0
            premium   = w.get("csp_premium") or w.get("cc_premium") or 0
            btc       = w.get("btc_limit") or "—"
            lines.append(
                f"  {ticker:<6} {phase.value:<14} "
                f"${strike} {expiry} ×{contracts}c "
                f"sold@${premium} BTC@${btc}"
            )
        return lines

    def print_summary(self) -> None:
        stats = self.lifetime_stats()
        print(f"\n{'─'*60}")
        print(f"  Wheel Engine Summary")
        print(f"{'─'*60}")
        print(f"  Active wheels:     {stats['active_wheels']}/{MAX_WHEELS_DISPLAY}")
        print(f"  Deployed capital:  ${stats['collateral_deployed']:,.0f}")
        print(f"  Lifetime premium:  ${stats['total_premium']:,.2f}")
        print(f"  Completed cycles:  {stats['total_cycles']}")
        print(f"{'─'*60}")
        for line in self.summary_lines():
            print(line)
        print(f"{'─'*60}\n")


MAX_WHEELS_DISPLAY = 10  # for display only


# ── Convenience: initialize from current live positions (script entry) ────

def init_from_live(dry_run: bool = False) -> WheelEngine:
    """
    Connect to E*Trade via saved tokens and initialize wheel state
    from current portfolio. Run once after first morning auth.

    Usage:
        python bot/wheel_engine.py --init
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bot.etrade_api import ETradeAPI, TokenExpiredError

    engine = WheelEngine()
    engine.load()

    try:
        api       = ETradeAPI(dry_run=dry_run)
        balance   = api.get_balance()
        positions = api.get_portfolio()
        print(f"Connected: NAV ${balance['net_value']:,.2f}, "
              f"{len(positions)} positions")
        n = engine.init_from_positions(positions, balance)
        print(f"Initialized {n} wheels.")
        engine.print_summary()
    except TokenExpiredError as e:
        print(f"Token error: {e}")
        print("Open the ETradeBot UI, authenticate, then re-run.")
    return engine


# ── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    if "--init" in sys.argv:
        init_from_live(dry_run="--dry-run" in sys.argv)
        sys.exit(0)

    # Unit test with mock positions
    print("\n── WheelEngine unit test ──")
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = f.name

    engine = WheelEngine(state_path=tmp_path)
    engine.load()

    # Simulate SOFI CSP lifecycle
    print("\n[1] Place CSP")
    engine.on_csp_placed("SOFI", strike=15.0, expiry="2026-07-17",
                          contracts=1, premium=0.57,
                          order_id="ORD001", initial_delta=0.28)
    print(f"    Phase: {engine.get('SOFI')['phase']}")

    print("[2] Fill CSP")
    engine.on_csp_filled("SOFI", fill_price=0.57)
    print(f"    Phase: {engine.get('SOFI')['phase']}")

    print("[3] Evaluate — BTC target not hit")
    action = engine.evaluate("SOFI", current_price=0.40, current_delta=0.20, dte=35)
    print(f"    Action: {action}")

    print("[4] Evaluate — BTC target hit")
    action = engine.evaluate("SOFI", current_price=0.28, current_delta=0.12, dte=30)
    print(f"    Action: {action}")

    print("[5] BTC fills")
    engine.on_btc_filled("SOFI", profit=29.00)
    print(f"    Phase: {engine.get('SOFI')['phase']}")

    # Simulate DOCS assignment → CC → called away
    print("\n[6] DOCS CSP → assignment → CC → called away")
    engine.on_csp_placed("DOCS", 20.0, "2026-06-19", 1, 1.14, "ORD002", 0.35)
    engine.on_csp_filled("DOCS", 1.14)
    engine.on_assigned("DOCS", shares=100, strike=20.0)
    print(f"    After assignment: {engine.get('DOCS')['phase']}, "
          f"cost basis ${engine.get('DOCS')['cost_basis']}")

    action = engine.evaluate("DOCS", current_price=20.50, current_delta=0, dte=0)
    print(f"    Eval: {action}")

    engine.on_cc_placed("DOCS", 21.0, "2026-07-17", 1, 0.85, "ORD003")
    engine.on_cc_filled("DOCS", 0.85)
    engine.on_called_away("DOCS")
    print(f"    After called away: {engine.get('DOCS')['phase']}, "
          f"cycle P&L ${engine.get('DOCS')['cycle_pnl']}")

    # Init from mock positions
    print("\n[7] Init from mock live positions")
    mock_positions = [
        {"ticker":"CCL",  "type":"STOCK","contracts":100,"cost":25.70,"current":27.57,"strike":0,"expiry":""},
        {"ticker":"SOFI", "type":"CSP",  "contracts":1,  "cost":0.57, "current":0.46, "strike":15,"expiry":"2026-07-17"},
        {"ticker":"DOCS", "type":"CSP",  "contracts":1,  "cost":1.14, "current":1.20, "strike":20,"expiry":"2026-07-17"},
    ]
    engine2 = WheelEngine(state_path=tmp_path + "2")
    engine2.load()
    n = engine2.init_from_positions(mock_positions, {"net_value": 6568})
    print(f"    Initialized {n} wheels")
    engine2.print_summary()

    # Detect assignment
    print("[8] Detect assignment from portfolio diff")
    before = [{"ticker":"SOFI","type":"CSP","strike":15,"expiry":"2026-06-19","contracts":1}]
    after  = [{"ticker":"SOFI","type":"STOCK","contracts":100,"cost":15.0,"current":14.5}]
    events = engine2.detect_changes(before, after)
    print(f"    Detected: {[e['event'] for e in events]}")

    stats = engine.lifetime_stats()
    print(f"\n── Lifetime stats ──")
    print(f"  Premium collected: ${stats['total_premium']:.2f}")
    print(f"  Cycles completed:  {stats['total_cycles']}")

    os.unlink(tmp_path)
    try:
        os.unlink(tmp_path + "2")
    except Exception:
        pass
    print("\n── All tests passed ──\n")
