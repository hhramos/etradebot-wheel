"""
data/db.py — SQLite persistence layer for ETradeBot
=====================================================
Single file, zero extra dependencies (sqlite3 ships with Python).
Replaces three JSON files:
  data/memory.json      → nav_snapshots, ai_recommendations, cycles (via memory.py)
  data/trade_log.json   → trade_events, bot_runs
  data/wheel_state.json → cycles (supplementary)

Thread safety: all writes go through a module-level Lock.
WAL mode enabled at init — readers never block writers.

Public API (called by other modules):
  init_db()                          → create tables, enable WAL, run migration once
  record_nav_snapshot(nav, bp)       → daily NAV row (deduplicates by date)
  record_trade_event(event, **kw)    → one row per bot action
  upsert_position(pos_dict)          → insert-or-replace current position
  record_cycle(cycle_dict)           → completed wheel cycle
  record_bot_run(summary_dict)       → one row per scheduler fire
  record_screener_snapshot(results)  → top candidates at screener time
  record_ai_recommendation(q, text, model) → advisor Q&A
  query_reinvest_log(limit)          → list of event dicts for projection.html
  build_memory_context(days)         → plain-text block for Ollama prompt
  get_log_entries(n)                 → last N trade_events for /data/log
  prune_old_rows()                   → delete bot_runs older than 90 days
"""

from __future__ import annotations

import os
import sqlite3
import threading
import datetime
import json
import re
import logging

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(__file__), "etradebot.db")
_LOCK    = threading.Lock()

# ── Connection helper ──────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    """Return a new connection. Caller must close it."""
    c = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ── Schema ─────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS nav_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL UNIQUE,   -- YYYY-MM-DD, one row per day
    nav         REAL    NOT NULL,
    buying_power REAL   DEFAULT 0,
    margin      REAL    DEFAULT 0,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    event        TEXT    NOT NULL,         -- QUEUED_EXIT, EXIT_PLACED, CSP_FILLED …
    ticker       TEXT    NOT NULL DEFAULT '',
    product_type TEXT    NOT NULL DEFAULT 'equity_option',
    action       TEXT    DEFAULT '',
    strike       REAL    DEFAULT NULL,
    expiry       TEXT    DEFAULT NULL,
    contracts    INTEGER DEFAULT NULL,
    price        REAL    DEFAULT NULL,
    pnl          REAL    DEFAULT NULL,
    order_id     TEXT    DEFAULT NULL,
    mode         TEXT    DEFAULT NULL,
    reason       TEXT    DEFAULT NULL,
    raw_json     TEXT    DEFAULT NULL      -- full original dict as JSON
);

CREATE INDEX IF NOT EXISTS idx_te_ts     ON trade_events(ts);
CREATE INDEX IF NOT EXISTS idx_te_ticker ON trade_events(ticker);

CREATE TABLE IF NOT EXISTS positions (
    ticker      TEXT    NOT NULL,
    type        TEXT    NOT NULL,          -- CSP, CC, STOCK
    strike      REAL    DEFAULT NULL,
    expiry      TEXT    DEFAULT NULL,
    contracts   INTEGER DEFAULT NULL,
    cost        REAL    DEFAULT NULL,
    current     REAL    DEFAULT NULL,
    pnl         REAL    DEFAULT NULL,
    pnl_pct     REAL    DEFAULT NULL,
    action      TEXT    DEFAULT NULL,
    product_type TEXT   NOT NULL DEFAULT 'equity_option',
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (ticker, type, strike, expiry)
);

CREATE TABLE IF NOT EXISTS cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    product_type    TEXT    NOT NULL DEFAULT 'equity_option',
    source          TEXT    NOT NULL DEFAULT 'live',   -- 'live' or 'backtest'
    entry_ts        TEXT    DEFAULT NULL,
    exit_ts         TEXT    DEFAULT NULL,
    opened          TEXT    DEFAULT NULL,              -- YYYY-MM-DD
    closed          TEXT    DEFAULT NULL,              -- YYYY-MM-DD
    csp_strike      REAL    DEFAULT NULL,
    csp_premium     REAL    DEFAULT NULL,
    btc_cost        REAL    DEFAULT NULL,
    net_pnl         REAL    DEFAULT NULL,
    outcome         TEXT    DEFAULT NULL,              -- BTC_PROFIT, ASSIGNED, CALLED_AWAY
    rolls           INTEGER DEFAULT 0,
    days_held       INTEGER DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_cy_ticker ON cycles(ticker);
CREATE INDEX IF NOT EXISTS idx_cy_closed ON cycles(closed);

CREATE TABLE IF NOT EXISTS screener_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    fisher_score INTEGER DEFAULT NULL,
    wheel_grade TEXT    DEFAULT NULL,
    wheel_score INTEGER DEFAULT NULL,
    iv_pct      REAL    DEFAULT NULL,
    csp_strike  REAL    DEFAULT NULL,
    csp_expiry  TEXT    DEFAULT NULL,
    nav_at_time REAL    DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_ss_ts ON screener_snapshots(ts);

CREATE TABLE IF NOT EXISTS ai_recommendations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    question    TEXT    NOT NULL,
    summary     TEXT    DEFAULT NULL,
    full_text   TEXT    DEFAULT NULL,
    model       TEXT    DEFAULT NULL,
    followed_at TEXT    DEFAULT NULL       -- NULL until action taken
);

CREATE TABLE IF NOT EXISTS bot_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    mode        TEXT    DEFAULT NULL,
    fills       INTEGER DEFAULT 0,
    exits       INTEGER DEFAULT 0,
    queued      INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT NULL,
    raw_json    TEXT    DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_br_ts ON bot_runs(ts);
"""


# ── Init + migration ───────────────────────────────────────────────────────

def init_db() -> None:
    """
    Create tables (idempotent), enable WAL, run JSON migration once.
    Call at server startup.
    """
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    with _LOCK:
        c = _conn()
        try:
            c.executescript(_SCHEMA)
            c.commit()
        finally:
            c.close()
    prune_old_rows()
    _migrate_json_once()
    logger.info("SQLite DB ready: %s", _DB_PATH)


def _migrate_json_once() -> None:
    """Import existing JSON files into SQLite on first run, then rename them."""
    base = os.path.dirname(_DB_PATH)

    _migrate_memory_json(os.path.join(base, "memory.json"))
    _migrate_trade_log_json(os.path.join(base, "trade_log.json"))


def _migrate_memory_json(path: str) -> None:
    if not os.path.exists(path):
        return
    migrated = path + ".migrated"
    if os.path.exists(migrated):
        return   # already done
    try:
        with open(path) as f:
            mem = json.load(f)

        now = datetime.datetime.now().isoformat()

        with _LOCK:
            c = _conn()
            try:
                # NAV history
                for e in mem.get("nav_history", []):
                    c.execute(
                        "INSERT OR IGNORE INTO nav_snapshots(date,nav,buying_power,updated_at)"
                        " VALUES(?,?,?,?)",
                        (e.get("date"), e.get("nav", 0), e.get("bp", 0), now)
                    )
                # Closed cycles
                for cy in mem.get("closed_cycles", []):
                    c.execute(
                        "INSERT INTO cycles(ticker,source,opened,closed,csp_strike,"
                        "csp_premium,btc_cost,net_pnl,outcome,rolls,days_held)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (cy.get("ticker"), "live",
                         cy.get("opened"), cy.get("closed"),
                         cy.get("strike"), cy.get("sold_at"), cy.get("btc_at"),
                         cy.get("net_pnl"), cy.get("outcome"),
                         cy.get("rolls", 0), cy.get("days_held"))
                    )
                # AI recommendations
                for r in mem.get("ai_recommendations", []):
                    c.execute(
                        "INSERT INTO ai_recommendations(date,question,summary,model)"
                        " VALUES(?,?,?,?)",
                        (r.get("date"), r.get("question",""),
                         r.get("summary",""), r.get("model",""))
                    )
                c.commit()
            finally:
                c.close()

        os.rename(path, migrated)
        logger.info("Migrated memory.json → SQLite (%s rows)", "ok")
    except Exception as e:
        logger.warning("memory.json migration failed: %s", e)


def _migrate_trade_log_json(path: str) -> None:
    if not os.path.exists(path):
        return
    migrated = path + ".migrated"
    if os.path.exists(migrated):
        return
    try:
        with open(path) as f:
            entries = json.load(f)

        with _LOCK:
            c = _conn()
            try:
                for e in entries:
                    event = e.get("event", "RAW")
                    ts    = e.get("ts") or e.get("ran_at", datetime.datetime.now().isoformat())
                    c.execute(
                        "INSERT INTO trade_events(ts,event,ticker,action,strike,expiry,"
                        "price,pnl,order_id,mode,reason,raw_json)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ts, event,
                         e.get("ticker",""), e.get("action",""),
                         e.get("strike"), e.get("expiry"),
                         e.get("price") or e.get("limit_price") or e.get("premium"),
                         e.get("profit") or e.get("pnl"),
                         e.get("order_id"), e.get("mode"),
                         e.get("reason"), json.dumps(e))
                    )
                c.commit()
            finally:
                c.close()

        os.rename(path, migrated)
        logger.info("Migrated trade_log.json → SQLite (%d entries)", len(entries))
    except Exception as e:
        logger.warning("trade_log.json migration failed: %s", e)


# ── Writers ────────────────────────────────────────────────────────────────

def record_nav_snapshot(nav: float, buying_power: float = 0,
                        margin: float = 0) -> None:
    if not nav or nav <= 0:
        return
    today = datetime.date.today().isoformat()
    now   = datetime.datetime.now().isoformat()
    with _LOCK:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO nav_snapshots(date,nav,buying_power,margin,updated_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(date) DO UPDATE SET"
                "   nav=excluded.nav, buying_power=excluded.buying_power,"
                "   margin=excluded.margin, updated_at=excluded.updated_at",
                (today, nav, buying_power, margin, now)
            )
            c.commit()
        finally:
            c.close()


def record_trade_event(event: str, ticker: str = "", **kwargs) -> None:
    ts = kwargs.pop("ts", datetime.datetime.now().isoformat())
    raw = {**kwargs, "event": event, "ticker": ticker, "ts": ts}
    with _LOCK:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO trade_events(ts,event,ticker,product_type,action,"
                "strike,expiry,contracts,price,pnl,order_id,mode,reason,raw_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, event, ticker,
                 kwargs.get("product_type", "equity_option"),
                 kwargs.get("action",""),
                 kwargs.get("strike"), kwargs.get("expiry"),
                 kwargs.get("contracts"), kwargs.get("price"),
                 kwargs.get("pnl") or kwargs.get("profit"),
                 kwargs.get("order_id"), kwargs.get("mode"),
                 kwargs.get("reason"), json.dumps(raw, default=str))
            )
            c.commit()
        finally:
            c.close()


def upsert_position(pos: dict) -> None:
    now = datetime.datetime.now().isoformat()
    ticker = pos.get("ticker","")
    ptype  = pos.get("type","")
    strike = pos.get("strike")
    expiry = pos.get("expiry","")
    if not ticker or not ptype:
        return
    with _LOCK:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO positions(ticker,type,strike,expiry,contracts,cost,"
                "current,pnl,pnl_pct,action,product_type,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(ticker,type,strike,expiry) DO UPDATE SET"
                "  contracts=excluded.contracts, cost=excluded.cost,"
                "  current=excluded.current, pnl=excluded.pnl,"
                "  pnl_pct=excluded.pnl_pct, action=excluded.action,"
                "  updated_at=excluded.updated_at",
                (ticker, ptype, strike, expiry or "",
                 pos.get("contracts"), pos.get("cost"),
                 pos.get("current"), pos.get("pnl"), pos.get("pnl_pct"),
                 pos.get("action"), "equity_option", now)
            )
            c.commit()
        finally:
            c.close()


def record_cycle(cycle: dict, source: str = "live") -> None:
    with _LOCK:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO cycles(ticker,product_type,source,opened,closed,"
                "csp_strike,csp_premium,btc_cost,net_pnl,outcome,rolls,days_held)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cycle.get("ticker",""),
                 cycle.get("product_type","equity_option"), source,
                 str(cycle.get("start_date",""))[:10] or None,
                 str(cycle.get("end_date",""))[:10] or None,
                 cycle.get("csp_strike"), cycle.get("csp_premium"),
                 cycle.get("btc_cost"), cycle.get("net_pnl"),
                 cycle.get("outcome"), cycle.get("rolls",0),
                 cycle.get("days_held"))
            )
            c.commit()
        finally:
            c.close()


def record_bot_run(summary: dict) -> None:
    ts = summary.get("ran_at", datetime.datetime.now().isoformat())
    with _LOCK:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO bot_runs(ts,mode,fills,exits,queued,errors,raw_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts, summary.get("mode"),
                 len(summary.get("fills",[])),
                 len(summary.get("exits",[])),
                 len(summary.get("queued",[])) + len(summary.get("entries",[])),
                 len(summary.get("errors",[])),
                 json.dumps(summary, default=str))
            )
            c.commit()
        finally:
            c.close()


def record_screener_snapshot(candidates: list, nav: float = 0) -> None:
    if not candidates:
        return
    ts  = datetime.datetime.now().isoformat()
    top = candidates[:20]   # store top 20 only
    with _LOCK:
        c = _conn()
        try:
            for cand in top:
                c.execute(
                    "INSERT INTO screener_snapshots"
                    "(ts,ticker,fisher_score,wheel_grade,wheel_score,"
                    "iv_pct,csp_strike,csp_expiry,nav_at_time)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (ts, cand.get("ticker",""),
                     cand.get("fisher_score"), cand.get("wheel_grade"),
                     cand.get("wheel_score"), cand.get("iv_pct"),
                     cand.get("csp_strike"), cand.get("csp_expiry"), nav)
                )
            c.commit()
        finally:
            c.close()


def record_ai_recommendation(question: str, text: str, model: str) -> None:
    summary = _extract_summary(text)
    if not summary:
        return
    today = datetime.date.today().isoformat()
    with _LOCK:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO ai_recommendations(date,question,summary,full_text,model)"
                " VALUES(?,?,?,?,?)",
                (today, question[:200], summary, text[:4000], model)
            )
            c.commit()
        finally:
            c.close()


# ── Readers ────────────────────────────────────────────────────────────────

def query_reinvest_log(limit: int = 15) -> list[dict]:
    """Return recent trade events for the reinvestment log panel."""
    relevant = (
        "REINVEST_DECISION","CYCLE_COMPLETE","ASSIGNED","CALLED_AWAY",
        "CC_ORDER_PREPARED","CC_ORDER_SUBMITTED","ROLL_EXECUTED","POLL",
        "DAILY_SUMMARY","QUEUED_EXIT","QUEUED_ENTRY","EXIT_PLACED","CSP_FILLED",
    )
    placeholders = ",".join("?" * len(relevant))
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM trade_events WHERE event IN ({placeholders})"
            f" ORDER BY ts DESC LIMIT ?",
            (*relevant, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def get_log_entries(n: int = 50) -> list[dict]:
    """Return last N trade_events rows for /data/log endpoint."""
    n = min(n, 500)
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trade_events ORDER BY ts DESC LIMIT ?", (n,)
        ).fetchall()
    # Return in chronological order (oldest first, like the old JSON list)
    return list(reversed([dict(r) for r in rows]))


def build_memory_context(days: int = 30) -> str:
    """
    Build plain-text memory block for Ollama advisor prompt.
    Backed by SQLite queries instead of the old memory.json text dump.
    Public API identical to the old bot/memory.py version.
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    lines  = ["TRADING MEMORY (last 30 days):"]

    try:
        with _conn() as c:
            # NAV trend
            nav_rows = c.execute(
                "SELECT date,nav,buying_power FROM nav_snapshots"
                " WHERE date >= ? ORDER BY date", (cutoff,)
            ).fetchall()
            if len(nav_rows) >= 2:
                oldest, newest = nav_rows[0], nav_rows[-1]
                delta = newest["nav"] - oldest["nav"]
                pct   = delta / oldest["nav"] * 100 if oldest["nav"] else 0
                sign  = "+" if delta >= 0 else ""
                lines.append(
                    f"  NAV trend ({oldest['date']} → {newest['date']}): "
                    f"${oldest['nav']:,.0f} → ${newest['nav']:,.0f} "
                    f"({sign}${delta:,.0f}, {sign}{pct:.1f}%)"
                )
            elif nav_rows:
                lines.append(f"  Current NAV: ${nav_rows[-1]['nav']:,.0f}")

            # Closed cycles summary
            cy_rows = c.execute(
                "SELECT ticker,closed,csp_strike,net_pnl,rolls,outcome"
                " FROM cycles WHERE source='live' AND closed >= ? ORDER BY closed",
                (cutoff,)
            ).fetchall()
            if cy_rows:
                total_pnl = sum(r["net_pnl"] or 0 for r in cy_rows)
                wins      = sum(1 for r in cy_rows if (r["net_pnl"] or 0) >= 0)
                win_rate  = round(wins / len(cy_rows) * 100)
                lines.append(
                    f"  Closed cycles ({days}d): {len(cy_rows)} trades  "
                    f"net P&L ${total_pnl:+.2f}  win rate {win_rate}%"
                )
                for r in cy_rows[-5:]:
                    lines.append(
                        f"    {r['ticker']}  {(r['closed'] or '')[:10]}  "
                        f"${r['csp_strike'] or '?'} put  "
                        f"net ${(r['net_pnl'] or 0):+.2f}  "
                        f"rolls:{r['rolls'] or 0}  {r['outcome'] or '?'}"
                    )
            else:
                lines.append("  No closed cycles in last 30 days")

            # Open positions
            pos_rows = c.execute(
                "SELECT ticker,type,strike,expiry,pnl,pnl_pct"
                " FROM positions WHERE type != 'STOCK'"
                " AND updated_at >= ?", (cutoff,)
            ).fetchall()
            if pos_rows:
                lines.append("  Open positions:")
                for p in pos_rows:
                    pnl_str = (f"  [${(p['pnl'] or 0):+.2f} ({(p['pnl_pct'] or 0):+.1f}%)]"
                               if p["pnl"] is not None else "")
                    lines.append(
                        f"    {p['ticker']} {p['type']} ${p['strike']} "
                        f"{p['expiry'] or ''}{pnl_str}"
                    )

            # Recent AI recommendations
            rec_rows = c.execute(
                "SELECT date,question,summary,model,followed_at"
                " FROM ai_recommendations WHERE date >= ? ORDER BY id DESC LIMIT 3",
                (cutoff,)
            ).fetchall()
            if rec_rows:
                lines.append("  Recent advisor recommendations:")
                for r in reversed(rec_rows):
                    followed = (
                        " ✓ followed"  if r["followed_at"] else ""
                    )
                    lines.append(
                        f"    {r['date']}: \"{r['summary'] or ''}\"  "
                        f"[{r['model'] or '?'}]{followed}"
                    )

    except Exception as e:
        logger.warning("build_memory_context DB error: %s", e)
        return ""

    lines.append("")
    result = "\n".join(lines)
    if len(result) > 3200:
        result = result[:3150] + "\n  … (memory truncated)\n"
    return result


def prune_old_rows() -> None:
    """Delete bot_runs older than 90 days. Safe to call at startup."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    with _LOCK:
        c = _conn()
        try:
            c.execute("DELETE FROM bot_runs WHERE ts < ?", (cutoff,))
            c.commit()
        finally:
            c.close()


# ── Internal helpers ───────────────────────────────────────────────────────

def _extract_summary(text: str, max_chars: int = 120) -> str:
    if not text:
        return ""
    clean = re.sub(r"^#+\s+",    "", text, flags=re.MULTILINE)
    clean = re.sub(r"^[\*\-•]\s+", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)
    m = re.search(r"[^.!?]+[.!?]", clean)
    sentence = m.group(0).strip() if m else clean[:max_chars]
    return sentence[:max_chars]
