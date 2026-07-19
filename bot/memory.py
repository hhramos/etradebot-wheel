"""
bot/memory.py — Persistent Memory Layer
========================================
Accumulates trading history, NAV trends, and AI recommendation outcomes
across sessions. Injected into the advisor prompt as a 30-day context block.

Storage: data/memory.json (created on first run, grows indefinitely,
         old records pruned automatically)

Three public functions:
  update(session, positions, closed_cycle=None)
      → Call from wheel_bot.run_cycle() after each cycle
      → Appends NAV snapshot, updates ticker notes, logs closed cycles

  record_recommendation(question, text, model, session)
      → Call from /advisor/chat after stream completes
      → Stores last 30 AI recommendations with one-sentence summary

  build_memory_context(days=30) → str
      → Call from build_advisor_prompt()
      → Returns compact plain-text block (~400-600 tokens max)
"""

from __future__ import annotations

import os
import json
import re
import datetime
import logging

logger = logging.getLogger(__name__)

_MEMORY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "memory.json"
)

# Retention limits — keeps file from growing unbounded
_MAX_NAV_DAYS        = 90
_MAX_CLOSED_CYCLES   = 200
_MAX_RECOMMENDATIONS = 30


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    try:
        with open(_MEMORY_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return _blank()
    except Exception as e:
        logger.warning(f"memory load error: {e} — starting blank")
        return _blank()


def _save(mem: dict) -> None:
    mem["last_updated"] = datetime.datetime.now().isoformat()
    try:
        with open(_MEMORY_PATH, "w") as f:
            json.dump(mem, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"memory save error: {e}")


def _blank() -> dict:
    return {
        "_description": ("ETradeBot persistent memory — "
                         "accumulates trading history across sessions. "
                         "Do not edit manually."),
        "_version":          2,
        "nav_history":       [],   # list of {date, nav, bp}
        "closed_cycles":     [],   # list of closed trade records
        "ai_recommendations":[],   # list of {date, question, summary, followed}
        "ticker_notes":      {},   # ticker → running stats
        "session_count":     0,
        "last_updated":      None,
    }


def _today() -> str:
    return datetime.date.today().isoformat()


def _extract_summary(text: str, max_chars: int = 120) -> str:
    """Extract the first meaningful sentence from an AI response."""
    if not text:
        return ""
    # Strip markdown headers and bullet prefixes
    clean = re.sub(r"^#+\s+", "", text, flags=re.MULTILINE)
    clean = re.sub(r"^[\*\-•]\s+", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)
    # First sentence
    m = re.search(r"[^.!?]+[.!?]", clean)
    sentence = m.group(0).strip() if m else clean[:max_chars]
    return sentence[:max_chars]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def update(
    session:        dict,
    positions:      list,
    closed_cycle:   dict | None = None,
) -> None:
    """
    Called by wheel_bot.run_cycle() at end of each scheduler fire.

    session:      _session dict from server.py (for NAV, account data)
    positions:    list of current position dicts from _last_positions
    closed_cycle: optional dict with keys matching CycleRecord fields,
                  passed when wheel_bot detects a BTC fill completing a cycle
    """
    mem = _load()
    today = _today()

    # ── 1. NAV snapshot ────────────────────────────────────────────────────
    nav = float(session.get("_net_value", 0) or 0)
    bp  = float(session.get("_accountBP", 0) or 0)
    if nav > 0:
        # Deduplicate — one entry per day
        existing_dates = {e["date"] for e in mem["nav_history"]}
        if today not in existing_dates:
            mem["nav_history"].append({"date": today, "nav": nav, "bp": bp})
        else:
            # Update today's entry with latest values
            for e in mem["nav_history"]:
                if e["date"] == today:
                    e["nav"] = nav
                    e["bp"]  = bp
        # Prune old entries
        cutoff = (datetime.date.today() -
                  datetime.timedelta(days=_MAX_NAV_DAYS)).isoformat()
        mem["nav_history"] = [e for e in mem["nav_history"]
                               if e["date"] >= cutoff]

    # ── 2. Closed cycle ────────────────────────────────────────────────────
    if closed_cycle:
        ticker  = closed_cycle.get("ticker", "?")
        net_pnl = float(closed_cycle.get("net_pnl", 0) or 0)
        rolls   = int(closed_cycle.get("rolls", 0) or 0)
        outcome = closed_cycle.get("outcome", "?")
        opened  = str(closed_cycle.get("start_date", ""))[:10]
        closed  = str(closed_cycle.get("end_date", today))[:10]
        strike  = closed_cycle.get("csp_strike", 0)
        sold_at = closed_cycle.get("csp_premium", 0)
        btc_at  = closed_cycle.get("btc_cost", 0)

        mem["closed_cycles"].append({
            "ticker":   ticker,
            "opened":   opened,
            "closed":   closed,
            "strike":   strike,
            "sold_at":  sold_at,
            "btc_at":   btc_at,
            "net_pnl":  net_pnl,
            "outcome":  outcome,
            "rolls":    rolls,
            "days_held": (datetime.date.fromisoformat(closed) -
                          datetime.date.fromisoformat(opened)).days
                          if opened else None,
        })
        # Prune
        mem["closed_cycles"] = mem["closed_cycles"][-_MAX_CLOSED_CYCLES:]

        # Update ticker notes
        notes = mem["ticker_notes"].setdefault(ticker, {
            "total_pnl": 0.0, "cycles": 0, "rolls": 0,
            "wins": 0, "losses": 0, "current_status": "CLOSED",
        })
        notes["total_pnl"] = round(notes.get("total_pnl", 0) + net_pnl, 2)
        notes["cycles"]    = notes.get("cycles", 0) + 1
        notes["rolls"]     = notes.get("rolls",  0) + rolls
        if net_pnl >= 0:
            notes["wins"]   = notes.get("wins",   0) + 1
        else:
            notes["losses"] = notes.get("losses", 0) + 1
        notes["last_closed"]    = closed
        notes["last_outcome"]   = outcome
        notes["current_status"] = "CLOSED"

    # ── 3. Update open positions in ticker_notes ───────────────────────────
    open_tickers = set()
    for pos in positions:
        ticker = pos.get("ticker", "")
        ptype  = pos.get("type", "")
        if not ticker or ptype == "STOCK":
            continue
        open_tickers.add(ticker)
        notes = mem["ticker_notes"].setdefault(ticker, {
            "total_pnl": 0.0, "cycles": 0, "rolls": 0,
            "wins": 0, "losses": 0,
        })
        notes["current_status"]  = ptype
        notes["current_pnl"]     = pos.get("pnl", 0)
        notes["current_pnl_pct"] = pos.get("pnl_pct", 0)
        notes["current_strike"]  = pos.get("strike")
        notes["current_expiry"]  = pos.get("expiry")

    # ── 4. Session counter ─────────────────────────────────────────────────
    mem["session_count"] = mem.get("session_count", 0) + 1

    _save(mem)
    logger.debug(f"memory updated — nav={nav} positions={len(positions)} "
                 f"closed={'yes' if closed_cycle else 'no'}")


def record_recommendation(
    question: str,
    text:     str,
    model:    str,
    session:  dict | None = None,
) -> None:
    """
    Called by server.py /advisor/chat after each stream completes.

    question: the user's question text
    text:     the full model response
    model:    active model name
    session:  optional — used to mark prior recommendations as followed
              when relevant orders appear in pending/completed actions
    """
    mem = _load()
    summary = _extract_summary(text)
    if not summary:
        return

    rec = {
        "date":     _today(),
        "question": question[:80],
        "summary":  summary,
        "model":    model,
        "followed": None,   # updated later when order is placed
    }

    # Check if any recent recommendation can be marked as followed
    # (a pending action for the same ticker as a recent recommendation)
    if session:
        pending_tickers = {a.get("ticker", "") for a in
                           session.get("_pending_actions", [])}
        for r in mem["ai_recommendations"][-5:]:
            if r.get("followed") is None:
                # Rough match: if ticker mentioned in summary is now pending
                for t in pending_tickers:
                    if t and t in r.get("summary", ""):
                        r["followed"] = True
                        break

    mem["ai_recommendations"].append(rec)
    mem["ai_recommendations"] = mem["ai_recommendations"][-_MAX_RECOMMENDATIONS:]
    _save(mem)


def build_memory_context(days: int = 30) -> str:
    """
    Build a compact plain-text memory block for injection into the advisor prompt.
    Capped at ~600 tokens regardless of history depth.
    Returns empty string if memory.json doesn't exist yet.
    """
    try:
        mem = _load()
    except Exception:
        return ""

    if not mem.get("nav_history") and not mem.get("closed_cycles"):
        return ""

    lines = ["TRADING MEMORY (last 30 days):"]

    # ── NAV trend ──────────────────────────────────────────────────────────
    nav_hist = sorted(mem.get("nav_history", []), key=lambda x: x["date"])
    cutoff   = (datetime.date.today() -
                datetime.timedelta(days=days)).isoformat()
    recent   = [e for e in nav_hist if e["date"] >= cutoff]
    if len(recent) >= 2:
        oldest = recent[0]
        newest = recent[-1]
        delta  = newest["nav"] - oldest["nav"]
        pct    = delta / oldest["nav"] * 100 if oldest["nav"] else 0
        sign   = "+" if delta >= 0 else ""
        lines.append(
            f"  NAV trend ({oldest['date']} → {newest['date']}): "
            f"${oldest['nav']:,.0f} → ${newest['nav']:,.0f} "
            f"({sign}${delta:,.0f}, {sign}{pct:.1f}%)"
        )
    elif nav_hist:
        lines.append(f"  Current NAV: ${nav_hist[-1]['nav']:,.0f}")

    # ── Closed cycles summary ──────────────────────────────────────────────
    closed = [c for c in mem.get("closed_cycles", [])
              if str(c.get("closed","")) >= cutoff]
    if closed:
        total_pnl = sum(c.get("net_pnl", 0) for c in closed)
        wins      = sum(1 for c in closed if c.get("net_pnl", 0) >= 0)
        win_rate  = round(wins / len(closed) * 100) if closed else 0
        lines.append(
            f"  Closed cycles ({days}d): {len(closed)} trades  "
            f"net P&L ${total_pnl:+.2f}  win rate {win_rate}%"
        )
        # Last 5 closed trades
        for c in sorted(closed, key=lambda x: x.get("closed",""))[-5:]:
            lines.append(
                f"    {c.get('ticker','?')}  {c.get('closed','?')[:10]}  "
                f"${c.get('strike','?')} put  "
                f"net ${c.get('net_pnl',0):+.2f}  "
                f"rolls:{c.get('rolls',0)}  {c.get('outcome','?')}"
            )
    else:
        lines.append("  No closed cycles in last 30 days")

    # ── Per-ticker running notes (active/recent only) ─────────────────────
    ticker_notes = mem.get("ticker_notes", {})
    active = {t: n for t, n in ticker_notes.items()
              if n.get("current_status") not in (None, "CLOSED")
              or n.get("last_closed","") >= cutoff}
    if active:
        lines.append("  Per-ticker history:")
        for ticker, n in sorted(active.items()):
            status    = n.get("current_status", "?")
            total_pnl = n.get("total_pnl", 0)
            cycles    = n.get("cycles", 0)
            rolls     = n.get("rolls", 0)
            wins      = n.get("wins", 0)
            cur_pnl   = n.get("current_pnl", None)
            cur_pct   = n.get("current_pnl_pct", None)
            parts = [f"    {ticker}: {cycles}c total ${total_pnl:+.2f} "
                     f"{wins}W/{n.get('losses',0)}L {rolls} rolls"]
            if status not in ("CLOSED",) and cur_pnl is not None:
                parts.append(f"  [{status} ${cur_pnl:+.2f} ({cur_pct:+.1f}%)]")
            lines.append("".join(parts))

    # ── Recent AI recommendations ──────────────────────────────────────────
    recs = mem.get("ai_recommendations", [])
    recent_recs = [r for r in recs if r.get("date","") >= cutoff][-3:]
    if recent_recs:
        lines.append("  Recent advisor recommendations:")
        for r in recent_recs:
            followed = (" ✓ followed" if r.get("followed") is True
                        else " ✗ not followed" if r.get("followed") is False
                        else "")
            lines.append(f"    {r.get('date','?')}: \"{r.get('summary','')}\"  "
                         f"[{r.get('model','?')}]{followed}")

    lines.append("")   # trailing newline for prompt spacing
    result = "\n".join(lines)

    # Hard cap at 800 tokens (~3,200 chars) — safety net
    if len(result) > 3200:
        result = result[:3150] + "\n  … (memory truncated)\n"

    return result
