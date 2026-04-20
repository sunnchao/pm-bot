from datetime import UTC, datetime, timedelta

from pm_bot.config import AppConfig
from pm_bot.filters import evaluate_no_trade_filters
from pm_bot.models import MarketSnapshot, OrderBookSide, SignalDecision
from pm_bot.risk import RiskManager


def make_market(**overrides) -> MarketSnapshot:
    base = dict(
        market_id="btc-5m-1",
        slug="btc-updown-5m",
        interval="5m",
        active=True,
        closed=False,
        seconds_to_expiry=240,
        liquidity=12_000,
        spread=0.02,
        up=OrderBookSide(price=0.52),
        down=OrderBookSide(price=0.48),
        reference_price=100_000.0,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def test_no_trade_filter_rejects_wide_spread():
    market = make_market(spread=0.05)
    blocked, reasons = evaluate_no_trade_filters(market=market, min_volatility_bps=2.0, realized_volatility_bps=6.0)

    assert blocked is True
    assert "spread_above_limit" in reasons


def test_no_trade_filter_rejects_extreme_price():
    market = make_market(up=OrderBookSide(price=0.67), down=OrderBookSide(price=0.33))
    blocked, reasons = evaluate_no_trade_filters(market=market, min_volatility_bps=2.0, realized_volatility_bps=6.0)

    assert blocked is True
    assert "extreme_market_price" in reasons


def test_risk_manager_blocks_after_daily_drawdown():
    risk = RiskManager()
    now = datetime(2026, 4, 18, tzinfo=UTC)
    risk.record_closed_trade(pnl=-30.0, closed_at=now)
    risk.record_closed_trade(pnl=-25.0, closed_at=now + timedelta(minutes=5))

    allowed, reasons = risk.allow_trade(balance=1_000.0, now=now + timedelta(minutes=10))

    assert allowed is False
    assert "daily_drawdown_limit" in reasons


def test_risk_manager_suggests_larger_size_for_oracle_delay():
    risk = RiskManager()
    decision = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="oracle_delay",
        confidence=0.85,
        reasons=["fast move"],
    )

    stake = risk.position_size(balance=1_000.0, decision=decision)

    assert stake == 40.0


def test_risk_manager_resets_five_loss_lockout_after_configured_cooldown():
    risk = RiskManager(config=AppConfig(cooldown_after_five_losses_minutes=30))
    start = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    for minute in range(5):
        risk.record_closed_trade(pnl=-1.0, closed_at=start + timedelta(minutes=minute))

    allowed_during_lockout, reasons_during_lockout = risk.allow_trade(
        balance=1_000.0,
        now=start + timedelta(minutes=33),
    )
    allowed_after_reset, reasons_after_reset = risk.allow_trade(
        balance=1_000.0,
        now=start + timedelta(minutes=35),
    )

    assert allowed_during_lockout is False
    assert reasons_during_lockout == ["five_loss_lockout"]
    assert allowed_after_reset is True
    assert reasons_after_reset == []


def test_risk_manager_caps_live_position_size_to_live_max_order_usd():
    risk = RiskManager(config=AppConfig(live_max_order_usd=10.0))
    decision = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="momentum",
        confidence=0.7,
        reasons=["trend"],
    )

    paper_stake = risk.position_size(balance=1_000.0, decision=decision)
    live_stake = risk.position_size(balance=1_000.0, decision=decision, live_mode=True)

    assert paper_stake == 20.0
    assert live_stake == 10.0


def test_risk_manager_quantizes_position_size_with_decimal_cents():
    risk = RiskManager(config=AppConfig(base_risk_pct=0.026665))
    decision = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="momentum",
        confidence=0.7,
        reasons=["trend"],
    )

    stake = risk.position_size(balance=1_000.0, decision=decision)

    assert stake == 26.66
