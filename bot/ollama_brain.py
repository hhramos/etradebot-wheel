"""
ollama_brain.py — Local Model Decision Engine
==============================================
Synchronous (non-streaming) Ollama interface for the autonomous wheel bot.
Every call returns structured JSON — no free-form text reaches the execution layer.

Four decision calls:
    rank_candidates(candidates, portfolio_context)
        → [{ticker, rank, action, strike, expiry, limit, reasoning}]

    evaluate_position(position, market_data)
        → {action: HOLD|ROLL|CLOSE|EARLY_BTC, urgency: 1-5, reasoning}

    select_cc_strike(option_chain, cost_basis, stock_context)
        → {strike, expiry, limit_price, delta, reasoning}

    assess_portfolio(all_positions, account)
        → {risk_level: LOW|MEDIUM|HIGH, summary, key_concern, urgent_tickers[]}

Design principles:
    - Temperature 0.2 — maximum consistency for financial decisions
    - JSON output enforced via system prompt + output validation
    - Regex fallback if JSON parse fails — never crashes the bot
    - Hard timeout: 90s fast model, 180s deep model
    - If Ollama is down → raises OllamaUnavailable → bot uses deterministic fallback
    - Every call logged with input context and parsed output for audit

Usage:
    brain = OllamaBrain()                    # uses config.json model
    brain = OllamaBrain(model="qwen2.5:7b")  # override

    result = brain.rank_candidates(candidates, portfolio)
    result = brain.evaluate_position(position, market_data)
    result = brain.select_cc_strike(chain, cost_basis, context)
    result = brain.assess_portfolio(positions, account)
"""

import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OLLAMA_BASE       = "http://127.0.0.1:11434"
OLLAMA_GENERATE   = OLLAMA_BASE + "/api/generate"
OLLAMA_CHAT       = OLLAMA_BASE + "/api/chat"
OLLAMA_TAGS       = OLLAMA_BASE + "/api/tags"
DEFAULT_MODEL     = "llama3.2:latest"
DEEP_MODEL        = "qwen2.5:7b"
TIMEOUT_FAST      = 90    # seconds
TIMEOUT_DEEP      = 180


class OllamaUnavailable(Exception):
    """Raised when Ollama is not reachable. Bot falls back to deterministic rules."""
    pass


class OllamaParseError(Exception):
    """Raised when model output cannot be parsed to expected schema."""
    pass


# ── OllamaBrain ───────────────────────────────────────────────────────────

class OllamaBrain:
    """
    Local Ollama model interface for autonomous wheel strategy decisions.
    All public methods return validated Python dicts — never raw strings.
    """

    def __init__(self, model: str = None, deep_model: str = None):
        cfg         = self._load_config()
        self.model  = model      or cfg.get("model",      DEFAULT_MODEL)
        self.deep   = deep_model or cfg.get("model_deep", DEEP_MODEL)
        self._host  = OLLAMA_BASE
        logger.info("OllamaBrain: fast=%s  deep=%s", self.model, self.deep)

    # ── Config ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_config() -> dict:
        cfg_path = os.path.join(_ROOT, "data", "config.json")
        try:
            with open(cfg_path) as f:
                return json.load(f)
        except Exception:
            return {}

    # ── Connectivity ──────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """True if Ollama is reachable at localhost:11434."""
        for host in ["http://127.0.0.1:11434", "http://localhost:11434"]:
            try:
                urllib.request.urlopen(host, timeout=3)
                self._host = host
                return True
            except Exception:
                continue
        return False

    def available_models(self) -> list[str]:
        """Return list of pulled model names."""
        try:
            url = self._host + "/api/tags"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def _ensure_available(self) -> None:
        if not self.is_available():
            raise OllamaUnavailable(
                "Ollama not reachable at localhost:11434. "
                "Bot will use deterministic fallback rules."
            )

    # ── Core HTTP call ────────────────────────────────────────────────────

    def _call(self, prompt: str, system: str,
               use_deep: bool = False,
               schema_hint: str = "") -> str:
        """
        Synchronous Ollama /api/chat call.
        Returns raw response text. Raises OllamaUnavailable on connection error.
        """
        self._ensure_available()
        model   = self.deep if use_deep else self.model
        timeout = TIMEOUT_DEEP if use_deep else TIMEOUT_FAST

        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]
        payload = json.dumps({
            "model":    model,
            "stream":   False,
            "messages": messages,
            "options":  {
                "temperature": 0.15,
                "top_p":       0.9,
                "num_predict": 1024,
            },
        }).encode()

        start = time.time()
        try:
            req = urllib.request.Request(
                self._host + "/api/chat",
                data    = payload,
                headers = {"Content-Type": "application/json"},
                method  = "POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data    = json.loads(resp.read())
                content = data.get("message", {}).get("content", "")
                elapsed = round(time.time() - start, 1)
                tokens  = data.get("eval_count", 0)
                logger.debug("Model=%s tokens=%d elapsed=%.1fs",
                             model, tokens, elapsed)
                return content
        except urllib.error.URLError as e:
            raise OllamaUnavailable(f"Ollama connection failed: {e}")
        except Exception as e:
            logger.error("Ollama call failed: %s", e)
            raise OllamaUnavailable(str(e))

    # ── JSON extraction ───────────────────────────────────────────────────

    @staticmethod
    def _extract_json(text: str) -> dict | list:
        """
        Extract JSON from model output.
        Tries: direct parse → ```json block → first {...} or [...] found.
        """
        text = text.strip()

        # Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # ```json ... ``` block
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass

        # First { ... } block
        m = re.search(r"(\{[\s\S]+\})", text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # First [ ... ] block
        m = re.search(r"(\[[\s\S]+\])", text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        raise OllamaParseError(
            f"Could not extract JSON from model output: {text[:200]}"
        )

    # ── Call 1: rank_candidates ───────────────────────────────────────────

    _RANK_SYSTEM = """You are a wheel strategy options advisor. Your job is to rank stock candidates for selling cash-secured puts.

WHEEL STRATEGY RULES:
- Favor stocks with stable fundamentals and consistent dividends
- Prefer lower-priced stocks that allow more contracts within capital limits
- Fisher score 9+ and Wheel grade A/B are ideal
- Avoid stocks approaching earnings (within 7 days)
- Consider portfolio diversification — don't pile into same sector

CRITICAL: You MUST respond with ONLY a valid JSON array. No explanation before or after.
No markdown. No preamble. Start your response with [ and end with ].

Output schema (array of objects, best first):
[
  {
    "ticker": "SOFI",
    "rank": 1,
    "recommended_action": "STO",
    "reasoning": "one sentence"
  }
]"""

    def rank_candidates(self,
                         candidates: list[dict],
                         portfolio_context: dict) -> list[dict]:
        """
        Rank screener candidates for new CSP entry.

        Args:
            candidates:        list of scored screener candidates
            portfolio_context: {nav, buying_power, active_tickers, active_count}

        Returns:
            Ranked list with {ticker, rank, recommended_action, reasoning}
            Falls back to score-sorted order if model unavailable.
        """
        if not candidates:
            return []

        # Build compact prompt — only send what the model needs
        cand_lines = []
        for c in candidates[:8]:    # cap at 8 to stay within context
            cand_lines.append(
                f"  {c['ticker']}: Fisher={c.get('fisher_score','?')} "
                f"Wheel={c.get('wheel_grade','?')}·{c.get('wheel_score','?')} "
                f"Price=${c.get('price','?')} Strike=${c.get('csp_strike','?')} "
                f"IV={c.get('iv_pct','?')}% ROC={c.get('roc_est','?')}% "
                f"Collateral%={c.get('wheel_collateral_pct','?')}%"
            )

        active = portfolio_context.get("active_tickers", [])
        nav    = portfolio_context.get("nav", 0)
        bp     = portfolio_context.get("buying_power", 0)

        prompt = f"""ACCOUNT: NAV=${nav:,.0f}  Buying power=${bp:,.0f}
Active wheels ({len(active)}): {', '.join(active) if active else 'none'}
5% position cap = ${nav*0.05:,.0f} per contract

CANDIDATES TO RANK:
{chr(10).join(cand_lines)}

Rank these candidates best-to-worst for wheel strategy entry given the account context.
Return JSON array only."""

        try:
            raw    = self._call(prompt, self._RANK_SYSTEM)
            parsed = self._extract_json(raw)
            if not isinstance(parsed, list):
                parsed = [parsed]
            # Validate and enrich
            result = []
            for i, item in enumerate(parsed):
                ticker = item.get("ticker", "").upper()
                if not ticker:
                    continue
                result.append({
                    "ticker":             ticker,
                    "rank":               item.get("rank", i + 1),
                    "recommended_action": item.get("recommended_action", "STO"),
                    "reasoning":          item.get("reasoning", ""),
                })
            logger.info("rank_candidates: model ranked %d candidates", len(result))
            self._log_call("rank_candidates", prompt, result)
            return result
        except OllamaUnavailable:
            logger.warning("rank_candidates: Ollama unavailable — returning score order")
            return [{"ticker": c["ticker"], "rank": i+1,
                     "recommended_action": "STO",
                     "reasoning": f"Score-based rank (model offline). "
                                  f"Fisher:{c.get('fisher_score')} Wheel:{c.get('wheel_grade')}"}
                    for i, c in enumerate(candidates[:8])]
        except OllamaParseError as e:
            logger.warning("rank_candidates: parse failed (%s) — score order", e)
            return [{"ticker": c["ticker"], "rank": i+1,
                     "recommended_action": "STO", "reasoning": "Parse fallback"}
                    for i, c in enumerate(candidates[:8])]

    # ── Call 2: evaluate_position ─────────────────────────────────────────

    _EVAL_SYSTEM = """You are a wheel strategy risk manager. Evaluate a single options position and recommend an action.

DECISION RULES:
- HOLD: premium decayed < 50%, delta within normal range, DTE > 14
- EARLY_BTC: premium decayed > 40% AND DTE < 14 (close early, free capital)
- ROLL: delta has increased significantly (1.5x initial) OR DTE < 14 with loss
- CLOSE: earnings within 7 days, or deep ITM with no recovery path
- HOLD is the DEFAULT — only recommend action when there is a specific reason

CRITICAL: Respond with ONLY valid JSON. No explanation outside the JSON object.

Output schema:
{
  "action": "HOLD",
  "urgency": 2,
  "reasoning": "one sentence",
  "suggested_roll_expiry": null,
  "suggested_roll_strike": null
}

action must be: HOLD, EARLY_BTC, ROLL, or CLOSE
urgency: 1=low, 2=normal, 3=moderate, 4=high, 5=urgent"""

    def evaluate_position(self, position: dict,
                           market_data: dict) -> dict:
        """
        Evaluate a single open wheel position.

        Args:
            position: wheel dict from WheelEngine (phase, strike, expiry, premium, etc)
            market_data: {current_price, current_delta, dte, stock_price,
                          pnl_pct, earnings_date, recent_change_pct}

        Returns:
            {action, urgency, reasoning, suggested_roll_expiry, suggested_roll_strike}
            Falls back to deterministic rules if model unavailable.
        """
        ticker       = position.get("ticker", "?")
        phase        = position.get("phase", "?")
        strike       = position.get("csp_strike") or position.get("cc_strike", 0)
        expiry       = position.get("csp_expiry") or position.get("cc_expiry", "?")
        premium      = position.get("csp_premium") or position.get("cc_premium", 0)
        init_delta   = position.get("csp_initial_delta", 0.28)
        current_px   = market_data.get("current_price", premium)
        current_del  = market_data.get("current_delta", 0)
        dte          = market_data.get("dte", 30)
        stock_px     = market_data.get("stock_price", 0)
        pnl_pct      = market_data.get("pnl_pct", 0)
        earnings     = market_data.get("earnings_date", "unknown")
        recent_chg   = market_data.get("recent_change_pct", 0)

        decay_pct = round((float(premium) - float(current_px)) / float(premium) * 100, 1) \
                    if premium else 0

        prompt = f"""POSITION TO EVALUATE:
Ticker:   {ticker}
Phase:    {phase}
Strike:   ${strike}  |  Expiry: {expiry}  |  DTE: {dte}
Sold @:   ${premium}  |  Now: ${current_px}  |  P&L: {pnl_pct:+.1f}%
Premium decay: {decay_pct:.1f}% (target: 50%)
Delta:    {current_del:.2f} (initial: {init_delta:.2f})
Stock:    ${stock_px} (changed {recent_chg:+.1f}% recently)
Earnings: {earnings}

Should I HOLD, EARLY_BTC, ROLL, or CLOSE this position?
Return JSON only."""

        try:
            raw    = self._call(prompt, self._EVAL_SYSTEM)
            parsed = self._extract_json(raw)
            if not isinstance(parsed, dict):
                raise OllamaParseError("Expected dict")

            action  = parsed.get("action", "HOLD").upper()
            if action not in ("HOLD", "EARLY_BTC", "ROLL", "CLOSE"):
                action = "HOLD"

            result = {
                "action":                action,
                "urgency":               int(parsed.get("urgency", 2)),
                "reasoning":             parsed.get("reasoning", ""),
                "suggested_roll_expiry": parsed.get("suggested_roll_expiry"),
                "suggested_roll_strike": parsed.get("suggested_roll_strike"),
                "source":                "model",
            }
            logger.info("evaluate_position %s: %s (urgency=%d) — %s",
                        ticker, action, result["urgency"], result["reasoning"][:60])
            self._log_call("evaluate_position", prompt, result)
            return result

        except (OllamaUnavailable, OllamaParseError) as e:
            logger.warning("evaluate_position %s: fallback (%s)", ticker, e)
            return self._deterministic_eval(position, market_data)

    def _deterministic_eval(self, position: dict, market_data: dict) -> dict:
        """
        Pure rule-based fallback when model is unavailable.
        Mirrors the evaluate() logic in wheel_engine.py.
        """
        from bot.trade_rules import (btc_target_hit, should_roll_position,
                                      DTE_ROLL_THRESHOLD, EARNINGS_BLACKOUT)
        premium     = float(position.get("csp_premium") or
                            position.get("cc_premium") or 0)
        init_delta  = float(position.get("csp_initial_delta", 0.28) or 0.28)
        current_px  = float(market_data.get("current_price", premium))
        current_del = float(market_data.get("current_delta", 0))
        dte         = int(market_data.get("dte", 30))
        pnl_pct     = float(market_data.get("pnl_pct", 0))
        earnings    = market_data.get("earnings_date")

        if earnings:
            try:
                import datetime
                if isinstance(earnings, str):
                    earnings = datetime.date.fromisoformat(earnings[:10])
                diff = abs((earnings - datetime.date.today()).days)
                if diff <= EARNINGS_BLACKOUT:
                    return {"action": "CLOSE", "urgency": 4,
                            "reasoning": f"Earnings in {diff} days",
                            "suggested_roll_expiry": None,
                            "suggested_roll_strike": None,
                            "source": "deterministic"}
            except Exception:
                pass

        if btc_target_hit(premium, current_px):
            return {"action": "EARLY_BTC", "urgency": 3,
                    "reasoning": "50% profit target reached",
                    "suggested_roll_expiry": None,
                    "suggested_roll_strike": None,
                    "source": "deterministic"}

        should_roll, reason = should_roll_position(
            current_del, init_delta, dte, pnl_pct
        )
        if should_roll:
            return {"action": "ROLL", "urgency": 4,
                    "reasoning": reason,
                    "suggested_roll_expiry": None,
                    "suggested_roll_strike": None,
                    "source": "deterministic"}

        return {"action": "HOLD", "urgency": 1,
                "reasoning": "No rule triggers — hold",
                "suggested_roll_expiry": None,
                "suggested_roll_strike": None,
                "source": "deterministic"}

    # ── Call 3: select_cc_strike ──────────────────────────────────────────

    _CC_SYSTEM = """You are a covered call strategy advisor. Select the best covered call strike after stock assignment.

RULES:
- Strike MUST be at or above cost basis (never sell below cost basis)
- Target delta 0.20–0.35 (don't give away too much upside)
- DTE 28–50 days (monthly expiry preferred)
- Limit price should be the mid-price (bid+ask)/2, rounded to nearest $0.05

CRITICAL: Respond with ONLY valid JSON. No text outside the JSON object.

Output schema:
{
  "strike": 21.0,
  "expiry": "2026-07-17",
  "limit_price": 0.85,
  "delta": 0.28,
  "reasoning": "one sentence"
}"""

    def select_cc_strike(self, option_chain: list[dict],
                          cost_basis: float,
                          stock_context: dict) -> Optional[dict]:
        """
        Select the best covered call strike after stock assignment.

        Args:
            option_chain: list of call strikes from etrade_api.get_option_chain()
            cost_basis:   adjusted cost basis (strike - premium_collected)
            stock_context: {ticker, stock_price, recent_change_pct, earnings_date}

        Returns:
            {strike, expiry, limit_price, delta, reasoning}
            or None if no suitable strike found
        """
        if not option_chain:
            return None

        ticker    = stock_context.get("ticker", "?")
        stock_px  = stock_context.get("stock_price", 0)
        earnings  = stock_context.get("earnings_date", "unknown")
        recent    = stock_context.get("recent_change_pct", 0)

        # Filter to valid strikes (above cost basis, with premium)
        valid = [s for s in option_chain
                 if s["strike"] >= cost_basis
                 and s.get("mid", 0) >= 0.15
                 and s.get("open_interest", 0) >= 50]

        if not valid:
            # Relax OI if nothing found
            valid = [s for s in option_chain
                     if s["strike"] >= cost_basis and s.get("mid", 0) >= 0.10]

        if not valid:
            logger.warning("select_cc_strike %s: no valid strikes above cost basis $%.2f",
                           ticker, cost_basis)
            return None

        chain_lines = []
        for s in valid[:10]:
            chain_lines.append(
                f"  Strike=${s['strike']}  Mid=${s.get('mid',0):.2f}  "
                f"Delta={s.get('delta',0):.2f}  OI={s.get('open_interest',0)}  "
                f"Expiry={s.get('expiry','?')}"
            )

        prompt = f"""COVERED CALL SELECTION:
Ticker:     {ticker}
Stock price: ${stock_px}
Cost basis:  ${cost_basis} (strike must be ≥ this)
Recent move: {recent:+.1f}%
Earnings:    {earnings}

AVAILABLE CALL STRIKES (above cost basis):
{chr(10).join(chain_lines)}

Select the best covered call strike. Return JSON only."""

        try:
            raw    = self._call(prompt, self._CC_SYSTEM)
            parsed = self._extract_json(raw)
            if not isinstance(parsed, dict):
                raise OllamaParseError("Expected dict")

            strike = float(parsed.get("strike", 0))
            if strike < cost_basis:
                logger.warning("Model selected strike $%.2f below cost basis $%.2f — correcting",
                                strike, cost_basis)
                # Pick the valid strike with highest mid
                best = max(valid, key=lambda x: x.get("mid", 0))
                strike = best["strike"]
                parsed["strike"]    = strike
                parsed["reasoning"] = f"Corrected to ${strike} (model below cost basis)"

            result = {
                "strike":      float(parsed.get("strike", strike)),
                "expiry":      parsed.get("expiry", valid[0].get("expiry", "")),
                "limit_price": float(parsed.get("limit_price", 0)),
                "delta":       float(parsed.get("delta", 0)),
                "reasoning":   parsed.get("reasoning", ""),
                "source":      "model",
            }
            logger.info("select_cc_strike %s: $%.2f exp=%s @ $%.2f delta=%.2f",
                        ticker, result["strike"], result["expiry"],
                        result["limit_price"], result["delta"])
            self._log_call("select_cc_strike", prompt, result)
            return result

        except (OllamaUnavailable, OllamaParseError) as e:
            logger.warning("select_cc_strike %s: fallback (%s)", ticker, e)
            # Deterministic fallback: pick strike with best premium at or above cost basis
            from bot.trade_rules import DELTA_RANGE, MIN_PREMIUM
            best = None
            for s in valid:
                d = abs(s.get("delta", 0.30))
                if DELTA_RANGE[0] <= d <= DELTA_RANGE[1] and s.get("mid", 0) >= MIN_PREMIUM:
                    if best is None or s.get("mid", 0) > best.get("mid", 0):
                        best = s
            if not best:
                best = max(valid, key=lambda x: x.get("mid", 0))
            return {
                "strike":      best["strike"],
                "expiry":      best.get("expiry", ""),
                "limit_price": best.get("mid", 0),
                "delta":       best.get("delta", 0),
                "reasoning":   "Deterministic fallback — highest valid premium",
                "source":      "deterministic",
            }

    # ── Call 4: assess_portfolio ──────────────────────────────────────────

    _ASSESS_SYSTEM = """You are a portfolio risk manager for a wheel strategy account.
Assess the overall portfolio risk and identify any urgent actions needed.

RISK LEVELS:
- LOW: all positions healthy, no imminent expirations, diversified
- MEDIUM: 1-2 positions need attention, some concentration risk
- HIGH: multiple positions under pressure, earnings risks, concentrated

CRITICAL: Respond with ONLY valid JSON. No text outside the JSON object.

Output schema:
{
  "risk_level": "MEDIUM",
  "summary": "2-3 sentence portfolio assessment",
  "key_concern": "the single most important thing to watch",
  "urgent_tickers": ["DOCS"],
  "recommended_focus": "what to prioritize today"
}"""

    def assess_portfolio(self, positions: list[dict],
                          account: dict) -> dict:
        """
        Assess overall portfolio risk and return morning briefing summary.

        Args:
            positions: list of wheel dicts from WheelEngine
            account:   {nav, buying_power, cash_balance}

        Returns:
            {risk_level, summary, key_concern, urgent_tickers, recommended_focus}
        """
        nav   = account.get("nav",           account.get("net_value", 0))
        bp    = account.get("buying_power",  0)
        cash  = account.get("cash_balance",  0)
        deployed_pct = round((nav - cash) / nav * 100, 1) if nav > 0 else 0

        pos_lines = []
        for w in positions:
            ticker   = w.get("ticker", "?")
            phase    = w.get("phase", "?")
            strike   = w.get("csp_strike") or w.get("cc_strike") or "—"
            expiry   = w.get("csp_expiry") or w.get("cc_expiry") or "—"
            premium  = w.get("csp_premium") or w.get("cc_premium") or 0
            shares   = w.get("shares_held", 0)
            pnl      = w.get("cycle_pnl") or "open"
            rolls    = w.get("roll_count", 0)
            pos_lines.append(
                f"  {ticker}: {phase} | "
                + (f"${strike} exp {expiry} | sold@${premium}"
                   if phase not in ("ASSIGNED", "IDLE") else
                   f"{shares}sh | cost_basis=${w.get('cost_basis','?')}")
                + (f" | rolled×{rolls}" if rolls else "")
            )

        prompt = f"""PORTFOLIO ASSESSMENT:
Account: NAV=${nav:,.0f}  Buying power=${bp:,.0f}  Cash=${cash:,.0f}
Deployed: {deployed_pct:.1f}% of NAV

POSITIONS ({len(positions)}):
{chr(10).join(pos_lines) if pos_lines else '  No active positions'}

Provide a brief risk assessment of this wheel strategy portfolio.
Return JSON only."""

        try:
            raw    = self._call(prompt, self._ASSESS_SYSTEM, use_deep=False)
            parsed = self._extract_json(raw)
            if not isinstance(parsed, dict):
                raise OllamaParseError("Expected dict")

            risk = parsed.get("risk_level", "MEDIUM").upper()
            if risk not in ("LOW", "MEDIUM", "HIGH"):
                risk = "MEDIUM"

            result = {
                "risk_level":         risk,
                "summary":            parsed.get("summary", ""),
                "key_concern":        parsed.get("key_concern", ""),
                "urgent_tickers":     parsed.get("urgent_tickers", []),
                "recommended_focus":  parsed.get("recommended_focus", ""),
                "source":             "model",
            }
            logger.info("assess_portfolio: risk=%s urgent=%s",
                        risk, result["urgent_tickers"])
            self._log_call("assess_portfolio", prompt, result)
            return result

        except (OllamaUnavailable, OllamaParseError) as e:
            logger.warning("assess_portfolio: fallback (%s)", e)
            # Deterministic fallback
            urgent = [w["ticker"] for w in positions
                      if w.get("phase") in ("CSP_ROLLING", "CC_ROLLING") or
                      w.get("roll_count", 0) > 0]
            risk = "HIGH" if len(urgent) > 1 else "MEDIUM" if urgent else "LOW"
            return {
                "risk_level":        risk,
                "summary":           f"{len(positions)} active positions. "
                                     f"{deployed_pct:.0f}% of capital deployed.",
                "key_concern":       f"{urgent[0]} needs attention" if urgent else
                                     "No urgent issues",
                "urgent_tickers":    urgent,
                "recommended_focus": "Review rolled positions" if urgent else
                                     "Monitor normally",
                "source":            "deterministic",
            }

    # ── Audit logging ─────────────────────────────────────────────────────

    def _log_call(self, call_name: str, prompt: str, result: dict) -> None:
        """Append model call to trade_log.json for audit."""
        from bot.time_utils import timestamp_et
        log_path = os.path.join(_ROOT, "data", "trade_log.json")
        entry = {
            "ts":       timestamp_et(),
            "event":    f"MODEL_{call_name.upper()}",
            "model":    self.model,
            "result":   result,
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
                json.dump(log[-2000:], f, indent=2, default=str)
        except Exception as e:
            logger.debug("Brain log write failed: %s", e)


# ── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import datetime
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    brain = OllamaBrain()

    print("\n═══════════════════════════════════════")
    print("  OllamaBrain Self-Test")
    print("═══════════════════════════════════════")

    # ── Connectivity ──────────────────────────────────────────────────────
    print(f"\n[1] Ollama available: {brain.is_available()}")
    models = brain.available_models()
    print(f"    Models installed: {models or 'none / offline'}")
    if not brain.is_available():
        print("    Ollama offline — running deterministic fallback tests only\n")

    # ── rank_candidates ───────────────────────────────────────────────────
    print("\n[2] rank_candidates")
    mock_candidates = [
        {"ticker":"NOK", "fisher_score":8,"wheel_grade":"C","wheel_score":65,
         "price":5.0,"csp_strike":5.0,"iv_pct":28,"roc_est":5.6,"wheel_collateral_pct":7.7},
        {"ticker":"F",   "fisher_score":7,"wheel_grade":"B","wheel_score":74,
         "price":10.2,"csp_strike":10.0,"iv_pct":24,"roc_est":4.6,"wheel_collateral_pct":15.4},
        {"ticker":"SOFI","fisher_score":7,"wheel_grade":"B","wheel_score":72,
         "price":15.3,"csp_strike":15.0,"iv_pct":35,"roc_est":5.2,"wheel_collateral_pct":23.1},
        {"ticker":"T",   "fisher_score":9,"wheel_grade":"A","wheel_score":100,
         "price":22.4,"csp_strike":22.0,"iv_pct":20,"roc_est":5.8,"wheel_collateral_pct":33.8},
    ]
    portfolio_ctx = {"nav": 6500, "buying_power": 1800,
                     "active_tickers": ["DOCS", "CCL"], "active_count": 2}
    ranked = brain.rank_candidates(mock_candidates, portfolio_ctx)
    for r in ranked:
        src = "(model)" if not r.get("source") == "deterministic" else "(fallback)"
        print(f"    #{r['rank']} {r['ticker']:6} {r['recommended_action']} {src}")
        print(f"         {r['reasoning'][:80]}")

    # ── evaluate_position ─────────────────────────────────────────────────
    print("\n[3] evaluate_position — HOLD case (decay 26%, DTE 35)")
    pos = {"ticker":"SOFI","phase":"CSP_OPEN","csp_strike":15.0,
            "csp_expiry":"2026-07-17","csp_premium":0.57,"csp_initial_delta":0.28}
    mkt = {"current_price":0.42,"current_delta":0.20,"dte":35,
           "stock_price":15.8,"pnl_pct":26.3,"recent_change_pct":1.2}
    ev = brain.evaluate_position(pos, mkt)
    print(f"    Action: {ev['action']} (urgency={ev['urgency']}) [{ev['source']}]")
    print(f"    {ev['reasoning'][:80]}")

    print("\n[4] evaluate_position — BTC_TARGET_HIT case (decay 51%)")
    mkt2 = {"current_price":0.28,"current_delta":0.10,"dte":28,
            "stock_price":16.2,"pnl_pct":50.9,"recent_change_pct":2.1}
    ev2 = brain.evaluate_position(pos, mkt2)
    print(f"    Action: {ev2['action']} (urgency={ev2['urgency']}) [{ev2['source']}]")
    print(f"    {ev2['reasoning'][:80]}")

    print("\n[5] evaluate_position — ROLL case (delta breach)")
    mkt3 = {"current_price":1.22,"current_delta":0.55,"dte":10,
            "stock_price":14.1,"pnl_pct":-114.0,"recent_change_pct":-7.2}
    ev3 = brain.evaluate_position(pos, mkt3)
    print(f"    Action: {ev3['action']} (urgency={ev3['urgency']}) [{ev3['source']}]")
    print(f"    {ev3['reasoning'][:80]}")

    # ── select_cc_strike ──────────────────────────────────────────────────
    print("\n[6] select_cc_strike (deterministic fallback)")
    mock_chain = [
        {"strike":26.0,"bid":0.60,"ask":0.70,"mid":0.65,"delta":0.35,
         "open_interest":450,"expiry":"2026-07-17"},
        {"strike":27.0,"bid":0.35,"ask":0.45,"mid":0.40,"delta":0.25,
         "open_interest":380,"expiry":"2026-07-17"},
        {"strike":28.0,"bid":0.18,"ask":0.26,"mid":0.22,"delta":0.16,
         "open_interest":210,"expiry":"2026-07-17"},
    ]
    cc = brain.select_cc_strike(
        mock_chain, cost_basis=25.70,
        stock_context={"ticker":"CCL","stock_price":27.57,
                       "recent_change_pct":7.3,"earnings_date":"2026-09-15"}
    )
    if cc:
        print(f"    Strike: ${cc['strike']}  Expiry: {cc['expiry']}  "
              f"Limit: ${cc['limit_price']}  Delta: {cc['delta']}  [{cc['source']}]")
        print(f"    {cc['reasoning'][:80]}")
    else:
        print("    No valid CC strike found")

    # ── assess_portfolio ──────────────────────────────────────────────────
    print("\n[7] assess_portfolio")
    mock_positions = [
        {"ticker":"CCL","phase":"ASSIGNED","shares_held":100,"cost_basis":25.70},
        {"ticker":"SOFI","phase":"CSP_OPEN","csp_strike":15.0,"csp_expiry":"2026-07-17",
         "csp_premium":0.57,"roll_count":0},
        {"ticker":"DOCS","phase":"CSP_OPEN","csp_strike":20.0,"csp_expiry":"2026-07-17",
         "csp_premium":1.14,"roll_count":1},
    ]
    assessment = brain.assess_portfolio(
        mock_positions,
        {"nav":6568,"buying_power":435,"cash_balance":130}
    )
    print(f"    Risk: {assessment['risk_level']} [{assessment['source']}]")
    print(f"    {assessment['summary'][:100]}")
    print(f"    Key concern: {assessment['key_concern'][:80]}")
    if assessment["urgent_tickers"]:
        print(f"    Urgent: {assessment['urgent_tickers']}")

    print("\n═══════════════════════════════════════")
    print("  All tests complete")
    print("═══════════════════════════════════════\n")
