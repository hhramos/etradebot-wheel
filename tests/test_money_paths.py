"""
ETradeBot — money-path regression tests.

Every test here corresponds to a bug that shipped (or nearly shipped) to a
live trading account. Run before every commit:

    pytest tests/ -v
"""
import datetime
import inspect
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# 1. Expiry dates — must NEVER be in the past
#    (bug shipped Jun 30: T order card offered 2026-06-19 on 2026-06-30)
# ═══════════════════════════════════════════════════════════════════════════

class TestExpiryNeverPast:
    def test_time_utils_expiry_after_third_friday(self, monkeypatch):
        import bot.time_utils as tu
        monkeypatch.setattr(tu, "today_et", lambda: datetime.date(2026, 6, 30))
        result = tu.next_monthly_expiry(0)
        assert result == datetime.date(2026, 7, 17)
        assert result > datetime.date(2026, 6, 30)

    def test_time_utils_expiry_before_third_friday(self, monkeypatch):
        import bot.time_utils as tu
        monkeypatch.setattr(tu, "today_et", lambda: datetime.date(2026, 6, 1))
        assert tu.next_monthly_expiry(0) == datetime.date(2026, 6, 19)

    def test_time_utils_expiry_on_third_friday_rolls_forward(self, monkeypatch):
        # Same-day expiry is useless — must roll to next month
        import bot.time_utils as tu
        monkeypatch.setattr(tu, "today_et", lambda: datetime.date(2026, 6, 19))
        assert tu.next_monthly_expiry(0) == datetime.date(2026, 7, 17)

    def test_time_utils_december_rolls_to_january(self, monkeypatch):
        import bot.time_utils as tu
        monkeypatch.setattr(tu, "today_et", lambda: datetime.date(2026, 12, 28))
        result = tu.next_monthly_expiry(0)
        assert result.year == 2027 and result.month == 1

    def test_screener_expiry_delegates_to_canonical(self, monkeypatch):
        # wheel_screener previously had a duplicate copy (the Jun 30 bug had
        # to be fixed twice). It now delegates to bot.time_utils — verify the
        # single source of truth actually controls the screener's output.
        import bot.time_utils as tu
        import wheel_screener as ws
        monkeypatch.setattr(tu, "today_et", lambda: datetime.date(2026, 6, 30))
        assert ws.next_monthly_expiry(0) == "2026-07-17"
        assert ws.next_monthly_expiry(0) == tu.next_monthly_expiry(0).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# 2. Exit triggers — 50% target, 21 DTE, earnings
# ═══════════════════════════════════════════════════════════════════════════

class TestExitRules:
    def test_btc_target_hit_at_exactly_50(self):
        from bot.trade_rules import btc_target_hit
        assert btc_target_hit(cost_basis=1.00, current_price=0.50) is True

    def test_btc_target_not_hit_at_49(self):
        from bot.trade_rules import btc_target_hit
        assert btc_target_hit(cost_basis=1.00, current_price=0.51) is False

    def test_btc_target_zero_cost_basis_safe(self):
        from bot.trade_rules import btc_target_hit
        assert btc_target_hit(cost_basis=0, current_price=0.50) is False

    def test_exit_fires_on_50pct_decay(self):
        from bot.trade_rules import should_exit_position
        exit_, reason = should_exit_position(
            cost_basis=1.14, current_price=0.57, dte=45)
        assert exit_ is True
        assert "50%" in reason

    def test_exit_fires_at_21_dte_in_profit(self):
        from bot.trade_rules import should_exit_position
        exit_, reason = should_exit_position(
            cost_basis=1.00, current_price=0.80, dte=21)
        assert exit_ is True
        assert "21 DTE" in reason

    def test_no_exit_at_21_dte_at_loss(self):
        # Underwater position at 21 DTE should NOT auto-exit (roll instead)
        from bot.trade_rules import should_exit_position
        exit_, _ = should_exit_position(
            cost_basis=1.00, current_price=1.40, dte=21)
        assert exit_ is False

    def test_illiquid_uses_14_dte(self):
        from bot.trade_rules import should_exit_position
        # 18 DTE, liquid would exit at 21 — illiquid must hold until 14
        exit_, _ = should_exit_position(
            cost_basis=1.00, current_price=0.80, dte=18, is_liquid=False)
        assert exit_ is False
        exit_, reason = should_exit_position(
            cost_basis=1.00, current_price=0.80, dte=14, is_liquid=False)
        assert exit_ is True

    def test_earnings_exit_overrides_everything(self, monkeypatch):
        from bot import trade_rules
        earnings = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        exit_, reason = trade_rules.should_exit_position(
            cost_basis=1.00, current_price=0.95, dte=40,
            earnings_date=earnings)
        assert exit_ is True
        assert "Earnings" in reason

    def test_earnings_blackout_blocks_new_entry(self, monkeypatch):
        from bot import trade_rules
        from bot.trade_rules import check_earnings_blackout, RuleViolation
        import bot.time_utils as tu
        monkeypatch.setattr(tu, "today_et", lambda: datetime.date(2026, 7, 3))
        with pytest.raises(RuleViolation):
            check_earnings_blackout(datetime.date(2026, 7, 8), "TEST")

    def test_earnings_blackout_allows_distant_earnings(self, monkeypatch):
        from bot.trade_rules import check_earnings_blackout
        import bot.time_utils as tu
        monkeypatch.setattr(tu, "today_et", lambda: datetime.date(2026, 7, 3))
        check_earnings_blackout(datetime.date(2026, 8, 15), "TEST")  # no raise


# ═══════════════════════════════════════════════════════════════════════════
# 3. WheelEngine ↔ wheel_bot signature compatibility
#    (bug shipped Jul 2: 17 real fills/cycle all failed on kwargs mismatch)
# ═══════════════════════════════════════════════════════════════════════════

class TestEngineSignatureCompat:
    """Bind the EXACT calls wheel_bot makes against the engine signatures.
    inspect.signature().bind() raises TypeError on any mismatch — no engine
    state or file I/O needed."""

    def test_on_btc_filled_accepts_wheel_bot_call(self):
        from bot.wheel_engine import WheelEngine
        sig = inspect.signature(WheelEngine.on_btc_filled)
        sig.bind(None, "DOCS", 44.48, order_id=None)   # as called in wheel_bot

    def test_on_csp_filled_accepts_wheel_bot_call(self):
        from bot.wheel_engine import WheelEngine
        sig = inspect.signature(WheelEngine.on_csp_filled)
        sig.bind(None, "DOCS", fill_price=1.14, order_id=None)

    def test_on_assigned_accepts_wheel_bot_call(self):
        from bot.wheel_engine import WheelEngine
        sig = inspect.signature(WheelEngine.on_assigned)
        sig.bind(None, "PFE", shares=100, strike=26.0)

    def test_old_buggy_kwargs_rejected(self):
        # The Jul 2 bug: these kwargs must NOT silently become accepted again
        from bot.wheel_engine import WheelEngine
        with pytest.raises(TypeError):
            inspect.signature(WheelEngine.on_btc_filled).bind(
                None, "X", profit=1.0, fill_price=0.5)
        with pytest.raises(TypeError):
            inspect.signature(WheelEngine.on_csp_filled).bind(
                None, "X", strike=20, expiry="2026-08-21",
                premium=1.0, contracts=1)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Position sizing — collateral caps
# ═══════════════════════════════════════════════════════════════════════════

class TestPositionSizing:
    def test_collateral_over_8pct_nav_rejected(self):
        from bot.trade_rules import check_collateral, RuleViolation
        # $50 strike × 100 = $5,000 on $10,000 NAV = 50% — way over 8%
        with pytest.raises(RuleViolation) as e:
            check_collateral(strike=50, contracts=1,
                             nav=10_000, buying_power=50_000)
        assert e.value.rule == "MAX_POSITION_PCT"

    def test_collateral_within_cap_passes(self):
        from bot.trade_rules import check_collateral
        # $7 strike × 100 = $700 on $10,000 NAV = 7% — under 8%
        check_collateral(strike=7, contracts=1,
                         nav=10_000, buying_power=50_000)

    def test_insufficient_buying_power_rejected(self):
        from bot.trade_rules import check_collateral, RuleViolation
        with pytest.raises(RuleViolation) as e:
            check_collateral(strike=7, contracts=1,
                             nav=10_000, buying_power=800)  # 700 > 800-300
        assert e.value.rule == "INSUFFICIENT_CAPITAL"

    def test_max_contracts_respects_all_three_limits(self):
        from bot.trade_rules import max_contracts_for_capital
        # capital limit: 2500/500=5c; cap limit: 8%*100k/500=16c; hard cap 10c
        assert max_contracts_for_capital(strike=5, capital=2_500,
                                         nav=100_000) == 5
        # hard cap binds: huge capital and NAV → still 10
        assert max_contracts_for_capital(strike=5, capital=1e9,
                                         nav=1e9) == 10
        # zero/negative inputs safe
        assert max_contracts_for_capital(strike=0, capital=1000, nav=1e5) == 0
        assert max_contracts_for_capital(strike=5, capital=0, nav=1e5) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5. Entry delta band
# ═══════════════════════════════════════════════════════════════════════════

class TestDeltaBand:
    def test_entry_delta_constants_sane(self):
        from bot.trade_rules import MAX_ENTRY_DELTA, MIN_ENTRY_DELTA
        # Put deltas are negative; MAX is the deeper (more negative) bound
        assert MAX_ENTRY_DELTA == -0.30
        assert MIN_ENTRY_DELTA == -0.15
        assert MAX_ENTRY_DELTA < MIN_ENTRY_DELTA
