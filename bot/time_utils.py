"""
time_utils.py — Market Time Utilities
======================================
Single source of truth for all time-related logic in the wheel bot.
All market times are in US Eastern (ET), DST-aware.
Never use datetime.datetime.now() directly — always use now_et().

Usage:
    from bot.time_utils import now_et, is_market_open, market_open_today, TimeVerifier
"""

import datetime
import time
import logging

logger = logging.getLogger(__name__)

# ── Timezone setup ────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python < 3.9 fallback
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        raise ImportError(
            "zoneinfo not available. Run: pip install tzdata backports.zoneinfo"
        )

ET  = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# ── NYSE Holiday / Early Close Calendar ──────────────────────────────────
# Hardcoded 2025–2027 with pandas_market_calendars as live fallback

NYSE_HOLIDAYS = {
    # 2025
    datetime.date(2025,  1,  1),  # New Year's Day
    datetime.date(2025,  1, 20),  # MLK Day
    datetime.date(2025,  2, 17),  # Presidents Day
    datetime.date(2025,  4, 18),  # Good Friday
    datetime.date(2025,  5, 26),  # Memorial Day
    datetime.date(2025,  6, 19),  # Juneteenth
    datetime.date(2025,  7,  4),  # Independence Day
    datetime.date(2025,  9,  1),  # Labor Day
    datetime.date(2025, 11, 27),  # Thanksgiving
    datetime.date(2025, 12, 25),  # Christmas

    # 2026
    datetime.date(2026,  1,  1),  # New Year's Day
    datetime.date(2026,  1, 19),  # MLK Day
    datetime.date(2026,  2, 16),  # Presidents Day
    datetime.date(2026,  4,  3),  # Good Friday
    datetime.date(2026,  5, 25),  # Memorial Day
    datetime.date(2026,  6, 19),  # Juneteenth
    datetime.date(2026,  7,  3),  # Independence Day (observed Fri)
    datetime.date(2026,  9,  7),  # Labor Day
    datetime.date(2026, 11, 26),  # Thanksgiving
    datetime.date(2026, 12, 25),  # Christmas

    # 2027
    datetime.date(2027,  1,  1),  # New Year's Day
    datetime.date(2027,  1, 18),  # MLK Day
    datetime.date(2027,  2, 15),  # Presidents Day
    datetime.date(2027,  3, 26),  # Good Friday
    datetime.date(2027,  5, 31),  # Memorial Day
    datetime.date(2027,  6, 18),  # Juneteenth (observed Fri)
    datetime.date(2027,  7,  5),  # Independence Day (observed Mon)
    datetime.date(2027,  9,  6),  # Labor Day
    datetime.date(2027, 11, 25),  # Thanksgiving
    datetime.date(2027, 12, 24),  # Christmas (observed Fri)
}

NYSE_EARLY_CLOSE = {
    # 1:00 PM ET closings
    datetime.date(2025, 11, 28): datetime.time(13, 0),  # Day after Thanksgiving
    datetime.date(2025, 12, 24): datetime.time(13, 0),  # Christmas Eve
    datetime.date(2026, 11, 27): datetime.time(13, 0),  # Day after Thanksgiving
    datetime.date(2026, 12, 24): datetime.time(13, 0),  # Christmas Eve
    datetime.date(2027, 11, 26): datetime.time(13, 0),  # Day after Thanksgiving
    datetime.date(2027, 12, 23): datetime.time(13, 0),  # Christmas Eve (observed Thu)
}

MARKET_OPEN  = datetime.time(9, 30)
MARKET_CLOSE = datetime.time(16, 0)


# ── Core time functions ───────────────────────────────────────────────────

def now_et() -> datetime.datetime:
    """Current time in US Eastern, fully DST-aware. Use this everywhere."""
    return datetime.datetime.now(tz=ET)


def today_et() -> datetime.date:
    """Today's date in ET (may differ from local date near midnight)."""
    return now_et().date()


def is_market_holiday(date: datetime.date = None) -> bool:
    """True if NYSE is closed all day on this date."""
    if date is None:
        date = today_et()

    # Try pandas_market_calendars first (most accurate)
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(
            start_date=date.isoformat(),
            end_date=date.isoformat()
        )
        return schedule.empty
    except Exception:
        pass  # Fall through to hardcoded

    return date in NYSE_HOLIDAYS


def early_close_time(date: datetime.date = None) -> datetime.time | None:
    """Return early close time if applicable, else None."""
    if date is None:
        date = today_et()
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(
            start_date=date.isoformat(),
            end_date=date.isoformat()
        )
        if not schedule.empty:
            close = schedule.iloc[0]["market_close"].to_pydatetime()
            close_et = close.astimezone(ET).time()
            if close_et < MARKET_CLOSE:
                return close_et
        return None
    except Exception:
        pass
    return NYSE_EARLY_CLOSE.get(date)


def market_open_today(date: datetime.date = None) -> bool:
    """True if NYSE opens at all today."""
    if date is None:
        date = today_et()
    if date.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return not is_market_holiday(date)


def get_market_close_today() -> datetime.time:
    """Return market close time today (handles early close days)."""
    early = early_close_time(today_et())
    return early if early else MARKET_CLOSE


def is_market_open(at: datetime.datetime = None) -> bool:
    """True if NYSE is currently in a live trading session."""
    if at is None:
        at = now_et()
    else:
        at = at.astimezone(ET)

    date = at.date()
    if date.weekday() >= 5:
        return False
    if is_market_holiday(date):
        return False

    open_dt  = datetime.datetime.combine(date, MARKET_OPEN,  tzinfo=ET)
    close_t  = early_close_time(date) or MARKET_CLOSE
    close_dt = datetime.datetime.combine(date, close_t, tzinfo=ET)

    return open_dt <= at < close_dt


def is_pre_market(at: datetime.datetime = None) -> bool:
    """True between 4:00 AM – 9:30 AM ET on a trading day."""
    if at is None:
        at = now_et()
    date = at.date()
    if not market_open_today(date):
        return False
    pre_open = datetime.datetime.combine(date, datetime.time(4, 0), tzinfo=ET)
    mkt_open = datetime.datetime.combine(date, MARKET_OPEN, tzinfo=ET)
    return pre_open <= at < mkt_open


def minutes_to_market_open() -> int:
    """Minutes until next market open. Returns 0 if currently open."""
    if is_market_open():
        return 0
    now = now_et()
    # Find next trading day
    candidate_date = now.date()
    if now.time() >= MARKET_OPEN:
        candidate_date += datetime.timedelta(days=1)
    while not market_open_today(candidate_date):
        candidate_date += datetime.timedelta(days=1)

    open_dt = datetime.datetime.combine(candidate_date, MARKET_OPEN, tzinfo=ET)
    delta   = open_dt - now
    return max(0, int(delta.total_seconds() / 60))


def minutes_to_market_close() -> int:
    """Minutes until market closes today. Returns 0 if closed."""
    if not is_market_open():
        return 0
    now = now_et()
    close_t  = get_market_close_today()
    close_dt = datetime.datetime.combine(now.date(), close_t, tzinfo=ET)
    delta    = close_dt - now
    return max(0, int(delta.total_seconds() / 60))


def is_expiry_friday(date: datetime.date = None) -> bool:
    """True if this is the 3rd Friday of the month (standard monthly expiry)."""
    if date is None:
        date = today_et()
    if date.weekday() != 4:   # not Friday
        return False
    # 3rd Friday: day is between 15 and 21
    return 15 <= date.day <= 21


def next_monthly_expiry(offset: int = 0) -> datetime.date:
    """
    Return the 3rd Friday of the month `offset` months ahead, guaranteed
    to be in the future. If offset=0 and this month's 3rd Friday has
    already passed, advances to next month's 3rd Friday instead.
    """
    today = today_et()

    def _third_friday(year: int, month: int) -> datetime.date:
        d = datetime.date(year, month, 1)
        fridays = 0
        while True:
            if d.weekday() == 4:
                fridays += 1
                if fridays == 3:
                    return d
            d += datetime.timedelta(days=1)

    year  = today.year
    month = today.month + offset
    while month > 12:
        month -= 12
        year  += 1

    candidate = _third_friday(year, month)

    # If the computed expiry has already passed (or is today), advance
    # to the next month's 3rd Friday — never return a past date.
    while candidate <= today:
        month += 1
        if month > 12:
            month = 1
            year += 1
        candidate = _third_friday(year, month)

    return candidate


def days_to_expiry(expiry: datetime.date | str) -> int:
    """Calendar days from today to expiry date."""
    if isinstance(expiry, str):
        expiry = datetime.date.fromisoformat(expiry)
    return (expiry - today_et()).days


def is_in_earnings_blackout(earnings_date: datetime.date | str | None,
                             blackout_days: int = 7) -> bool:
    """True if today is within blackout_days of an earnings date."""
    if earnings_date is None:
        return False
    if isinstance(earnings_date, str):
        try:
            earnings_date = datetime.date.fromisoformat(earnings_date)
        except ValueError:
            return False
    diff = abs((earnings_date - today_et()).days)
    return diff <= blackout_days


def is_30min_mark(at: datetime.datetime = None) -> bool:
    """True at :00 and :30 of any hour during market hours."""
    if at is None:
        at = now_et()
    if not is_market_open(at):
        return False
    return at.minute in (0, 30) and at.second < 90


def timestamp_et(dt: datetime.datetime = None) -> str:
    """Return ISO-style timestamp string in ET for logging."""
    if dt is None:
        dt = now_et()
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def dual_timestamp() -> str:
    """Return 'HH:MM LOCAL / HH:MM ET' for log lines."""
    local = datetime.datetime.now().strftime("%H:%M %Z") if datetime.datetime.now().tzinfo else \
            datetime.datetime.now().strftime("%H:%M local")
    et    = now_et().strftime("%H:%M ET")
    return f"{local} / {et}"


# ── TimeVerifier ─────────────────────────────────────────────────────────

class TimeVerifier:
    """
    Print a startup sanity-check block showing local time, ET time,
    DST status, market status, and next scheduled run.
    Call TimeVerifier.print_block() at bot startup.
    """

    @staticmethod
    def dst_active() -> bool:
        now_et_dt = now_et()
        return bool(now_et_dt.dst())

    @staticmethod
    def utc_offset_str() -> str:
        now_et_dt = now_et()
        offset    = now_et_dt.utcoffset()
        total_hrs = int(offset.total_seconds() / 3600)
        return f"UTC{total_hrs:+d}"

    @staticmethod
    def market_status_str() -> str:
        if is_market_open():
            mins_left = minutes_to_market_close()
            return f"OPEN  ({mins_left} min until close)"
        elif market_open_today():
            if is_pre_market():
                return "PRE-MARKET"
            mins = minutes_to_market_open()
            return f"CLOSED (opens in {mins} min)"
        else:
            mins = minutes_to_market_open()
            return f"CLOSED — holiday/weekend (next open in {mins} min)"

    @staticmethod
    def print_block(next_run_label: str = ""):
        now_local = datetime.datetime.now()
        now_e     = now_et()
        early     = early_close_time()

        lines = [
            "═" * 55,
            "  ETradeBot Time Verification",
            "═" * 55,
            f"  Computer local time:  {now_local.strftime('%I:%M %p')} "
            f"({now_local.astimezone().strftime('%Z') if hasattr(now_local, 'astimezone') else 'local'})",
            f"  Eastern market time:  {now_e.strftime('%I:%M %p %Z')} "
            f"({TimeVerifier.utc_offset_str()})",
            f"  DST currently active: {'Yes' if TimeVerifier.dst_active() else 'No'}",
            f"  Market status:        {TimeVerifier.market_status_str()}",
            f"  Today is holiday:     {'Yes' if is_market_holiday() else 'No'}",
            f"  Early close today:    {'Yes — ' + str(early) if early else 'No'}",
        ]
        if next_run_label:
            lines.append(f"  Next scheduled run:   {next_run_label}")
        lines.append("═" * 55)

        for line in lines:
            print(line)
        logger.info("Time verification: ET=%s market=%s",
                    now_e.strftime("%H:%M %Z"),
                    TimeVerifier.market_status_str())


# ── Scheduler support ─────────────────────────────────────────────────────

class RunTracker:
    """
    Track which run modes have already fired today to prevent
    double-execution after a bot restart.
    """

    def __init__(self, state_path: str = "data/wheel_state.json"):
        self._state_path = state_path
        self._ran: dict[str, datetime.date] = {}
        self._load()

    def _load(self):
        import json, os
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path) as f:
                    state = json.load(f)
                for mode, date_str in state.get("ran_today", {}).items():
                    self._ran[mode] = datetime.date.fromisoformat(date_str)
            except Exception:
                pass

    def _save(self):
        import json, os
        path = self._state_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            data = {}
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
            data["ran_today"] = {m: d.isoformat() for m, d in self._ran.items()}
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("RunTracker save failed: %s", e)

    def already_ran(self, mode: str) -> bool:
        return self._ran.get(mode) == today_et()

    def mark_ran(self, mode: str):
        self._ran[mode] = today_et()
        self._save()

    def reset_day(self):
        """Call at start of new trading day to clear yesterday's runs."""
        today = today_et()
        self._ran = {m: d for m, d in self._ran.items() if d == today}
        self._save()


# ── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    TimeVerifier.print_block("9:35 AM ET (morning scan)")
    print()
    print(f"  next_monthly_expiry(0) = {next_monthly_expiry(0)}")
    print(f"  next_monthly_expiry(1) = {next_monthly_expiry(1)}")
    print(f"  minutes_to_open        = {minutes_to_market_open()}")
    print(f"  is_expiry_friday       = {is_expiry_friday()}")
    print(f"  days_to_expiry('2026-07-17') = {days_to_expiry('2026-07-17')}")
