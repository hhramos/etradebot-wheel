"""
bot/memory.py — Persistent Memory Layer
========================================
Accumulates trading history, NAV trends, and AI recommendation outcomes
across sessions. Injected into the advisor prompt as a 30-day context block.

Storage: data/etradebot.db (SQLite, via data/db.py)
         data/memory.json migrated automatically on first run

Three public functions:
  update(session, positions, closed_cycle=None)
  record_recommendation(question, text, model, session)
  build_memory_context(days=30) → str
"""

from __future__ import annotations

import datetime
import logging

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.date.today().isoformat()


def _db():
    from data import db
    return db


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
    db = _db()
    today = _today()

    nav = float(session.get("_net_value", 0) or 0)
    bp  = float(session.get("_accountBP", 0) or 0)

    if nav > 0:
        db.record_nav_snapshot(nav=nav, buying_power=bp)

    if closed_cycle:
        db.record_cycle(closed_cycle)

    for pos in positions:
        if not pos.get("ticker") or pos.get("type") == "STOCK":
            continue
        db.upsert_position(pos)

    logger.debug(f"memory updated — nav={nav} positions={len(positions)} "
                 f"closed={'yes' if closed_cycle else 'no'}")


def record_recommendation(
    question: str,
    text:     str,
    model:    str,
    session:  dict | None = None,
) -> None:
    """Called by /advisor/chat after each stream completes."""
    try:
        _db().record_ai_recommendation(question=question, text=text, model=model)
    except Exception as e:
        logger.warning(f"record_recommendation failed: {e}")


def build_memory_context(days: int = 30) -> str:
    """
    Build a compact plain-text memory block for injection into the advisor prompt.
    Delegates entirely to db.build_memory_context().
    """
    try:
        return _db().build_memory_context(days=days)
    except Exception as e:
        logger.warning(f"build_memory_context error: {e}")
        return ""
