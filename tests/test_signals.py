from pm_bot.models import Candle, MarketSnapshot, OrderBookSide, PriceTick
from pm_bot.signals import SignalEngine


def make_market(interval: str = "5m") -> MarketSnapshot:
    return MarketSnapshot(
        market_id=f"btc-{interval}-1",
        slug=f"btc-updown-{interval}",
        interval=interval,
        active=True,
        closed=False,
        seconds_to_expiry=420 if interval == "15m" else 240,
        liquidity=20_000,
        spread=0.02,
        up=OrderBookSide(price=0.51),
        down=OrderBookSide(price=0.49),
        reference_price=100_000.0,
    )


def test_oracle_delay_has_priority_when_market_near_even():
    engine = SignalEngine()
    market = make_market("5m")
    decision = engine.decide(
        market=market,
        latest_tick=PriceTick(price=100_060.0, volume=120.0),
        recent_ticks=[
            PriceTick(price=100_000.0, volume=40.0),
            PriceTick(price=100_010.0, volume=45.0),
            PriceTick(price=100_060.0, volume=120.0),
        ],
        candles=[
            Candle(open=99_980.0, high=100_010.0, low=99_970.0, close=100_000.0),
            Candle(open=100_000.0, high=100_040.0, low=99_995.0, close=100_030.0),
            Candle(open=100_030.0, high=100_070.0, low=100_020.0, close=100_060.0),
        ],
    )

    assert decision.should_trade is True
    assert decision.side == "UP"
    assert decision.signal_name == "oracle_delay"


def test_oracle_delay_uses_reference_price_when_available():
    engine = SignalEngine()
    market = make_market("5m")
    market.reference_price = 100_060.0

    decision = engine.oracle_delay_signal(
        market=market,
        latest_tick=PriceTick(price=100_060.0, volume=120.0),
        recent_ticks=[
            PriceTick(price=100_000.0, volume=40.0),
            PriceTick(price=100_010.0, volume=45.0),
            PriceTick(price=100_060.0, volume=120.0),
        ],
        candles=[
            Candle(open=99_980.0, high=100_010.0, low=99_970.0, close=100_000.0),
            Candle(open=100_000.0, high=100_040.0, low=99_995.0, close=100_030.0),
            Candle(open=100_030.0, high=100_070.0, low=100_020.0, close=100_060.0),
        ],
    )

    assert decision.should_trade is False
    assert decision.reasons == ["oracle_delta_too_small"]


def test_momentum_used_when_oracle_delay_not_triggered():
    engine = SignalEngine()
    market = make_market("5m")
    decision = engine.decide(
        market=market,
        latest_tick=PriceTick(price=100_024.0, volume=50.0),
        recent_ticks=[
            PriceTick(price=100_010.0, volume=30.0),
            PriceTick(price=100_018.0, volume=32.0),
            PriceTick(price=100_024.0, volume=34.0),
        ],
        candles=[
            Candle(open=100_000.0, high=100_015.0, low=99_995.0, close=100_010.0),
            Candle(open=100_010.0, high=100_025.0, low=100_005.0, close=100_018.0),
            Candle(open=100_018.0, high=100_030.0, low=100_015.0, close=100_024.0),
        ],
    )

    assert decision.should_trade is True
    assert decision.side == "UP"
    assert decision.signal_name == "momentum"


def test_conflicting_signals_skip_trade():
    engine = SignalEngine()
    market = make_market("15m")
    decision = engine.decide(
        market=market,
        latest_tick=PriceTick(price=99_880.0, volume=60.0),
        recent_ticks=[
            PriceTick(price=100_040.0, volume=25.0),
            PriceTick(price=99_960.0, volume=35.0),
            PriceTick(price=99_880.0, volume=60.0),
        ],
        candles=[
            Candle(open=100_060.0, high=100_080.0, low=100_020.0, close=100_030.0),
            Candle(open=100_030.0, high=100_040.0, low=99_930.0, close=99_940.0),
            Candle(open=99_940.0, high=99_950.0, low=99_860.0, close=99_880.0),
        ],
    )

    assert decision.should_trade is False
    assert decision.reasons == ["conflicting_signals"]


def test_mean_reversion_for_fifteen_minute_extreme_move():
    engine = SignalEngine()
    market = make_market("15m")
    market.reference_price = 99_840.0
    decision = engine.decide(
        market=market,
        latest_tick=PriceTick(price=99_840.0, volume=60.0),
        recent_ticks=[
            PriceTick(price=100_040.0, volume=25.0),
            PriceTick(price=99_960.0, volume=35.0),
            PriceTick(price=99_840.0, volume=60.0),
        ],
        candles=[
            Candle(open=100_060.0, high=100_140.0, low=100_000.0, close=100_120.0),
            Candle(open=100_120.0, high=100_130.0, low=99_760.0, close=99_780.0),
            Candle(open=99_780.0, high=99_900.0, low=99_820.0, close=99_840.0),
        ],
    )

    assert decision.should_trade is True
    assert decision.side == "UP"
    assert decision.signal_name == "mean_reversion"
