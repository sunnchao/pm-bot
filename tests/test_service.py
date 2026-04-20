import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import pm_bot.service as service_module
from pm_bot.clients import (
    FixtureBinanceMarketDataClient,
    FixtureChainlinkReferenceClient,
    FixturePolymarketMarketClient,
)
from pm_bot.config import AppConfig
from pm_bot.execution import ExecutionRequest, ExecutionResult, PaperExecutor
from pm_bot.live_guards import evaluate_live_order_guards
from pm_bot.models import Candle, MarketSnapshot, OrderBookSide, PriceTick, SignalDecision
from pm_bot.recorder import PaperTradeRecorder
from pm_bot.risk import RiskManager
from pm_bot.service import TradingService


def make_market(**overrides) -> MarketSnapshot:
    base = dict(
        market_id="btc-5m-1",
        slug="btc-updown-5m",
        interval="5m",
        active=True,
        closed=False,
        seconds_to_expiry=240,
        liquidity=20_000.0,
        spread=0.02,
        up=OrderBookSide(price=0.51),
        down=OrderBookSide(price=0.49),
        reference_price=100_000.0,
        neg_risk=False,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def make_service(
    paper_trades_path: Path,
    market: MarketSnapshot | None = None,
    signal_engine=None,
) -> TradingService:
    selected_market = market or make_market()
    config = AppConfig(paper_trades_path=paper_trades_path)
    return TradingService(
        config=config,
        binance=FixtureBinanceMarketDataClient(
            latest_tick=PriceTick(price=100_100.0, volume=10.0),
            candles=[
                Candle(open=99_980.0, high=100_000.0, low=99_970.0, close=100_000.0),
                Candle(open=100_000.0, high=100_040.0, low=99_995.0, close=100_030.0),
                Candle(open=100_030.0, high=100_110.0, low=100_020.0, close=100_060.0),
            ],
        ),
        polymarket=FixturePolymarketMarketClient(market=selected_market),
        chainlink=FixtureChainlinkReferenceClient(reference=selected_market.reference_price),
        signal_engine=signal_engine,
        risk_manager=RiskManager(config=config),
        recorder=PaperTradeRecorder(paper_trades_path),
    )


def freeze_service_now(monkeypatch, now: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now.replace(tzinfo=None)
            return now.astimezone(tz)

    monkeypatch.setattr(service_module, "datetime", FrozenDateTime)


class NoActiveMarketClient:
    def active_markets(self, interval: str) -> list[MarketSnapshot]:
        return []


class MultiMarketClient:
    def __init__(self, markets: list[MarketSnapshot]) -> None:
        self.markets = markets

    def active_markets(self, interval: str) -> list[MarketSnapshot]:
        return [market for market in self.markets if market.interval == interval]


class SettlementAwareBinanceClient(FixtureBinanceMarketDataClient):
    def __init__(self, latest_tick: PriceTick, candles: list[Candle], price_at_map: dict[str, float]) -> None:
        super().__init__(latest_tick=latest_tick, candles=candles)
        self.price_at_map = price_at_map

    def price_at(self, at: datetime, symbol: str = "BTCUSDT") -> PriceTick:
        return PriceTick(price=self.price_at_map[at.isoformat()], volume=0.0)


class RecordingExecutor:
    def __init__(
        self,
        status: str = "accepted",
        message: str = "stubbed",
        *,
        action: str = "live_trade",
        order_id: str | None = "order-123",
        submission_id: str | None = "submission-123",
    ) -> None:
        self.requests = []
        self.action = action
        self.status = status
        self.message = message
        self.order_id = order_id
        self.submission_id = submission_id

    def execute(self, request):
        self.requests.append(request)
        return ExecutionResult(
            action=self.action,
            status=self.status,
            order_id=self.order_id,
            submission_id=self.submission_id,
            submitted_price=request.price,
            submitted_size=request.size_usd,
            message=self.message,
        )


class StubSignalEngine:
    def __init__(self, decision: SignalDecision) -> None:
        self.decision = decision

    def decide(self, market, latest_tick, recent_ticks, candles):
        return self.decision


class StubRiskManager:
    def __init__(self, *, stake: float) -> None:
        self.stake = stake

    def allow_trade(self, balance: float, now: datetime):
        return True, []

    def position_size(
        self,
        balance: float,
        decision: SignalDecision,
        *,
        live_mode: bool = False,
    ) -> float:
        return self.stake


def _cycle_metric_records(caplog):
    return [record for record in caplog.records if record.name == "pm_bot.cycle" and record.msg == "trading_cycle"]


def test_oneshot_emits_cycle_metrics_for_success(tmp_path: Path, caplog):
    service = make_service(tmp_path / "paper_trades.jsonl")

    with caplog.at_level(logging.INFO, logger="pm_bot.cycle"):
        result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "paper_trade"
    records = _cycle_metric_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.event == "trading_cycle"
    assert record.outcome == "success"
    assert record.interval == "5m"
    assert record.action == "paper_trade"
    assert record.market_id == "btc-5m-1"
    assert record.signal_name == "oracle_delay"
    assert record.confidence == 0.9
    assert record.side == "UP"
    assert record.stake == 40.0
    assert record.reasons == ["delta=100.00"]
    assert record.execution_status == "recorded"
    assert record.execution_message == "paper trade recorded"
    assert record.duration_ms >= 0.0


def test_oneshot_emits_cycle_metrics_for_skip(tmp_path: Path, caplog):
    decision = SignalDecision(
        should_trade=False,
        side=None,
        signal_name="test_signal",
        confidence=0.15,
        reasons=["no_edge"],
    )
    service = make_service(
        tmp_path / "paper_trades.jsonl",
        signal_engine=StubSignalEngine(decision),
    )

    with caplog.at_level(logging.INFO, logger="pm_bot.cycle"):
        result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "skip"
    records = _cycle_metric_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.event == "trading_cycle"
    assert record.outcome == "skip"
    assert record.interval == "5m"
    assert record.action == "skip"
    assert record.market_id == "btc-5m-1"
    assert record.signal_name == "test_signal"
    assert record.confidence == 0.15
    assert record.side is None
    assert record.stake == 0.0
    assert record.reasons == ["no_edge"]
    assert record.execution_status is None
    assert record.execution_message is None
    assert record.duration_ms >= 0.0


def test_oneshot_emits_cycle_metrics_for_error(tmp_path: Path, caplog):
    base_service = make_service(tmp_path / "paper_trades.jsonl")
    service = TradingService(
        config=base_service.config,
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=FixtureChainlinkReferenceClient(reference=float("nan")),
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
    )

    with caplog.at_level(logging.ERROR, logger="pm_bot.cycle"):
        with pytest.raises(ValueError, match="unexpected reference price"):
            service.oneshot(interval="5m", balance=1_000.0)

    records = _cycle_metric_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.event == "trading_cycle"
    assert record.outcome == "error"
    assert record.interval == "5m"
    assert record.action == "error"
    assert record.market_id == "btc-5m-1"
    assert record.signal_name is None
    assert record.side is None
    assert record.stake == 0.0
    assert record.reasons == []
    assert record.error_type == "ValueError"
    assert record.error_message == "unexpected reference price"
    assert record.duration_ms >= 0.0


def test_oneshot_emits_cycle_metrics_for_live_execution_error(tmp_path: Path, caplog):
    decision = SignalDecision(
        should_trade=True,
        side="DOWN",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    market = make_market(
        token_id_up="token-up",
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=False,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor(status="error", message="live executor failed")
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    with caplog.at_level(logging.INFO, logger="pm_bot.cycle"):
        result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "live_trade"
    assert result.execution_status == "error"
    records = _cycle_metric_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.event == "trading_cycle"
    assert record.outcome == "error"
    assert record.action == "live_trade"
    assert record.execution_status == "error"
    assert record.execution_message == "live executor failed"
    assert record.duration_ms >= 0.0


def test_oneshot_emits_cycle_metrics_for_untrackable_live_execution_error(tmp_path: Path, caplog):
    decision = SignalDecision(
        should_trade=True,
        side="DOWN",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    market = make_market(
        token_id_up="token-up",
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=False,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor(
        action="skip",
        status="error",
        message="live executor failed before order tracking",
        order_id=None,
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    with caplog.at_level(logging.INFO, logger="pm_bot.cycle"):
        result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.execution_status == "error"
    records = _cycle_metric_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.event == "trading_cycle"
    assert record.outcome == "error"
    assert record.action == "skip"
    assert record.execution_status == "error"
    assert record.execution_message == "live executor failed before order tracking"
    assert record.order_id is None
    assert record.submission_id == "submission-123"
    assert record.duration_ms >= 0.0


def test_oneshot_builds_transport_friendly_execution_request_before_executor_call(tmp_path: Path):
    decision = SignalDecision(
        should_trade=True,
        side="DOWN",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01, neg_risk=False),
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor()
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=("btc-5m-1",),
            live_max_order_usd=10.0,
            paper_trades_path=service.config.paper_trades_path,
        ),
        binance=service.binance,
        polymarket=service.polymarket,
        chainlink=service.chainlink,
        signal_engine=service.signal_engine,
        risk_manager=service.risk_manager,
        recorder=service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "live_trade"
    assert result.submission_id == "submission-123"
    assert len(executor.requests) == 1
    request = executor.requests[0]
    assert request.market_id == "btc-5m-1"
    assert request.token_id == "token-down"
    assert request.side == "DOWN"
    assert request.price == 0.49
    assert request.size_usd == 10.0
    assert request.order_type == "market"
    assert request.metadata["interval"] == "5m"
    assert request.metadata["tick_size"] == 0.01
    assert request.metadata["neg_risk"] is False
    assert request.metadata["signal"] == {
        "should_trade": True,
        "side": "DOWN",
        "signal_name": "test_signal",
        "confidence": 0.42,
        "reasons": ["test_reason"],
    }
    assert isinstance(request.metadata["signal"], dict)


def test_oneshot_live_executor_uses_side_specific_token_id_and_live_metadata(tmp_path: Path):
    decision = SignalDecision(
        should_trade=True,
        side="DOWN",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    market = make_market(
        token_id_up="token-up",
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=True,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor()
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "live_trade"
    assert len(executor.requests) == 1
    request = executor.requests[0]
    assert request.token_id == "token-down"
    assert request.metadata["tick_size"] == 0.01
    assert request.metadata["neg_risk"] is True
    assert request.metadata["notes"] == ["live_trade"]


def test_oneshot_blocks_non_live_mode_before_executor_call(tmp_path: Path):
    executor = RecordingExecutor()
    service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01),
    )
    service = TradingService(
        config=service.config,
        binance=service.binance,
        polymarket=service.polymarket,
        chainlink=service.chainlink,
        signal_engine=service.signal_engine,
        risk_manager=service.risk_manager,
        recorder=service.recorder,
        executor=executor,
    )
    service.config.wallet_private_key = "0xabc123"
    service.config.signature_type = 0
    service.config.funder_address = "0x0000000000000000000000000000000000000001"
    service.config.live_allow_market_ids = ("btc-5m-1",)
    service.config.live_max_order_usd = 100.0

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_mode_required"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_mode_required"
    assert executor.requests == []


def test_oneshot_blocks_live_execution_for_confirmation_and_wallet_gaps(tmp_path: Path):
    executor = RecordingExecutor()
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01),
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            live_allow_market_ids=("btc-5m-1",),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "skip"
    assert result.reasons == ["live_confirmation_required", "live_wallet_config_incomplete"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_confirmation_required,live_wallet_config_incomplete"
    assert executor.requests == []


def test_oneshot_blocks_live_execution_when_allowlist_is_empty(tmp_path: Path):
    executor = RecordingExecutor()
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01),
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_market_allowlist_required"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_market_allowlist_required"
    assert executor.requests == []


def test_oneshot_blocks_live_execution_for_blank_wallet_config(tmp_path: Path):
    executor = RecordingExecutor()
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01),
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="   ",
            signature_type=0,
            funder_address="  ",
            live_allow_market_ids=("btc-5m-1",),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_wallet_config_incomplete"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_wallet_config_incomplete"
    assert executor.requests == []


def test_oneshot_blocks_live_execution_for_allowlist_size_and_metadata_gaps(tmp_path: Path):
    decision = SignalDecision(
        should_trade=True,
        side="DOWN",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    market = make_market(
        down=OrderBookSide(price=0.49),
        token_id_up="token-up",
        token_id_down=None,
        tick_size=None,
        neg_risk=None,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor()
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=("another-market",),
            live_max_order_usd=10.0,
            max_side_price=0.62,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == [
        "live_market_not_allowlisted",
        "live_market_token_ids_incomplete",
        "live_token_id_missing",
        "live_neg_risk_missing",
    ]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == (
        "live_market_not_allowlisted,"
        "live_market_token_ids_incomplete,"
        "live_token_id_missing,"
        "live_neg_risk_missing"
    )
    assert executor.requests == []


def test_oneshot_blocks_live_execution_when_any_market_token_id_is_missing(tmp_path: Path):
    decision = SignalDecision(
        should_trade=True,
        side="DOWN",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    market = make_market(
        token_id_up=None,
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=False,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor()
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_market_token_ids_incomplete"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_market_token_ids_incomplete"
    assert executor.requests == []


def test_oneshot_blocks_invalid_live_numeric_order_values_before_executor_call(tmp_path: Path):
    decision = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    market = make_market(
        up=OrderBookSide(price=0.0),
        down=OrderBookSide(price=1.0),
        token_id_up="token-up",
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=False,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor()
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=StubRiskManager(stake=float("nan")),
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_order_size_invalid", "live_price_invalid"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_order_size_invalid,live_price_invalid"
    assert executor.requests == []


def test_live_order_guards_reject_non_finite_config_thresholds():
    market = make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01, neg_risk=False)

    reasons = evaluate_live_order_guards(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=float("nan"),
            max_side_price=float("inf"),
        ),
        market=market,
        request=ExecutionRequest(
            market_id=market.market_id,
            token_id=market.token_id_up,
            side="UP",
            price=0.51,
            size_usd=20.0,
            order_type="market",
        ),
        live_confirmed=True,
    )

    assert reasons == ["live_order_size_limit_invalid", "live_price_limit_invalid"]


def test_live_order_guards_reject_non_positive_expiry_threshold():
    market = make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01, neg_risk=False)

    reasons = evaluate_live_order_guards(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=20.0,
            max_side_price=0.62,
            min_seconds_5m=-1,
        ),
        market=market,
        request=ExecutionRequest(
            market_id=market.market_id,
            token_id=market.token_id_up,
            side="UP",
            price=0.51,
            size_usd=10.0,
            order_type="market",
        ),
        live_confirmed=True,
    )

    assert reasons == ["live_min_seconds_invalid"]


def test_live_order_guards_include_price_and_expiry_checks():
    market = make_market(
        seconds_to_expiry=60,
        down=OrderBookSide(price=0.71),
        token_id_up="token-up",
        token_id_down=None,
        tick_size=None,
        neg_risk=None,
    )

    reasons = evaluate_live_order_guards(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=("another-market",),
            live_max_order_usd=10.0,
            max_side_price=0.62,
        ),
        market=market,
        request=ExecutionRequest(
            market_id=market.market_id,
            token_id=None,
            side="DOWN",
            price=0.71,
            size_usd=20.0,
            order_type="market",
        ),
        live_confirmed=True,
    )

    assert reasons == [
        "live_market_not_allowlisted",
        "live_order_size_exceeds_limit",
        "live_price_exceeds_max_side_price",
        "live_market_too_close_to_expiry",
        "live_market_token_ids_incomplete",
        "live_token_id_missing",
        "live_neg_risk_missing",
    ]


def test_live_order_guards_require_non_empty_allowlist_and_non_blank_wallet_fields():
    market = make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01)

    reasons = evaluate_live_order_guards(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key=" ",
            signature_type=0,
            funder_address="\t",
            live_allow_market_ids=(),
            live_max_order_usd=100.0,
        ),
        market=market,
        request=ExecutionRequest(
            market_id=market.market_id,
            token_id="token-up",
            side="UP",
            price=0.51,
            size_usd=20.0,
            order_type="market",
        ),
        live_confirmed=True,
    )

    assert reasons == [
        "live_market_allowlist_required",
        "live_wallet_config_incomplete",
    ]


def test_live_order_guards_block_token_id_mismatch_for_side():
    market = make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01, neg_risk=False)

    reasons = evaluate_live_order_guards(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
        ),
        market=market,
        request=ExecutionRequest(
            market_id=market.market_id,
            token_id="token-down",
            side="UP",
            price=0.51,
            size_usd=20.0,
            order_type="market",
        ),
        live_confirmed=True,
    )

    assert reasons == ["live_token_id_mismatch"]


def test_oneshot_live_selection_prefers_allowlisted_market_over_more_liquid_market(tmp_path: Path):
    allowed_market = make_market(
        market_id="allowed-market",
        liquidity=10_000.0,
        token_id_up="token-up",
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=False,
    )
    blocked_market = make_market(
        market_id="blocked-market",
        liquidity=50_000.0,
        token_id_up="blocked-up",
        token_id_down="blocked-down",
        tick_size=0.01,
        neg_risk=False,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=allowed_market,
    )
    executor = RecordingExecutor()
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(allowed_market.market_id,),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=MultiMarketClient([blocked_market, allowed_market]),
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "live_trade"
    assert result.market_id == allowed_market.market_id
    assert len(executor.requests) == 1
    assert executor.requests[0].market_id == allowed_market.market_id


def test_oneshot_blocks_invalid_live_signal_side_before_executor_call(tmp_path: Path):
    decision = SignalDecision(
        should_trade=True,
        side="SIDEWAYS",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    market = make_market(
        token_id_up="token-up",
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=False,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor()
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["decision_side_invalid"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "decision_side_invalid"
    assert executor.requests == []


def test_oneshot_blocks_invalid_paper_signal_side_before_recording_trade(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    decision = SignalDecision(
        should_trade=True,
        side="SIDEWAYS",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    service = make_service(
        paper_trades_path,
        signal_engine=StubSignalEngine(decision),
    )

    result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "skip"
    assert result.reasons == ["decision_side_invalid"]
    assert result.execution_status is None
    assert result.execution_message is None
    assert not paper_trades_path.exists()


def test_oneshot_blocks_missing_reference_price_before_paper_trade_recording(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    decision = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    service = make_service(
        paper_trades_path,
        market=make_market(reference_price=None),
        signal_engine=StubSignalEngine(decision),
    )

    result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "skip"
    assert result.reasons == ["reference_price_unavailable"]
    assert result.execution_status is None
    assert result.execution_message is None
    assert not paper_trades_path.exists()


def test_oneshot_blocks_missing_reference_price_before_live_executor_call(tmp_path: Path):
    decision = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=make_market(reference_price=None, token_id_up="token-up", token_id_down="token-down", tick_size=0.01),
        signal_engine=StubSignalEngine(decision),
    )
    executor = RecordingExecutor()
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=("btc-5m-1",),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["reference_price_unavailable"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "reference_price_unavailable"
    assert executor.requests == []


def test_oneshot_live_path_surfaces_live_price_guard_reason(tmp_path: Path):
    decision = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="test_signal",
        confidence=0.42,
        reasons=["test_reason"],
    )
    market = make_market(
        up=OrderBookSide(price=0.71),
        down=OrderBookSide(price=0.29),
        token_id_up="token-up",
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=False,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
        signal_engine=StubSignalEngine(decision),
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
            max_side_price=0.62,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=RecordingExecutor(),
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_price_exceeds_max_side_price"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_price_exceeds_max_side_price"


def test_oneshot_live_path_surfaces_live_expiry_guard_reason(tmp_path: Path):
    market = make_market(
        seconds_to_expiry=60,
        token_id_up="token-up",
        token_id_down="token-down",
        tick_size=0.01,
        neg_risk=False,
    )
    base_service = make_service(
        tmp_path / "paper_trades.jsonl",
        market=market,
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(market.market_id,),
            live_max_order_usd=100.0,
            paper_trades_path=base_service.config.paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=RecordingExecutor(),
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_market_too_close_to_expiry"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_market_too_close_to_expiry"


def test_oneshot_default_paper_executor_preserves_paper_ledger_behavior(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"

    result = make_service(paper_trades_path).oneshot(interval="5m", balance=1_000.0)

    assert result.action == "paper_trade"
    assert result.execution_status == "recorded"
    assert result.execution_message == "paper trade recorded"
    record = json.loads(paper_trades_path.read_text(encoding="utf-8").strip())
    assert record["market_id"] == "btc-5m-1"
    assert record["side"] == "UP"
    assert record["price"] == 0.51
    assert record["stake"] == 40.0
    assert record["notes"] == ["paper_trade"]


def test_oneshot_live_mode_without_executor_blocks_instead_of_falling_back_to_paper(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    base_service = make_service(
        paper_trades_path,
        market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01, neg_risk=False),
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=("btc-5m-1",),
            live_max_order_usd=100.0,
            paper_trades_path=paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
    )

    result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "skip"
    assert result.reasons == ["live_executor_not_configured"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_executor_not_configured"
    assert not paper_trades_path.exists()


def test_oneshot_live_mode_blocks_when_live_executor_is_missing(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    base_service = make_service(
        paper_trades_path,
        market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01, neg_risk=False),
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=("btc-5m-1",),
            live_max_order_usd=100.0,
            paper_trades_path=paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_executor_not_configured"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_executor_not_configured"
    assert not paper_trades_path.exists()


def test_oneshot_live_mode_blocks_explicit_paper_executor_fallback(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    base_service = make_service(
        paper_trades_path,
        market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01, neg_risk=False),
    )
    service = TradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=("btc-5m-1",),
            live_max_order_usd=100.0,
            paper_trades_path=paper_trades_path,
        ),
        binance=base_service.binance,
        polymarket=base_service.polymarket,
        chainlink=base_service.chainlink,
        signal_engine=base_service.signal_engine,
        risk_manager=base_service.risk_manager,
        recorder=base_service.recorder,
        executor=PaperExecutor(base_service.recorder),
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_executor_not_configured"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_executor_not_configured"
    assert not paper_trades_path.exists()


def test_oneshot_injected_executor_surfaces_status_without_paper_ledger_setup(tmp_path: Path):
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    config = AppConfig(
        trading_mode="live",
        wallet_private_key="0xabc123",
        signature_type=0,
        funder_address="0x0000000000000000000000000000000000000001",
        live_allow_market_ids=("btc-5m-1",),
        live_max_order_usd=100.0,
        paper_trades_path=blocked_parent / "paper_trades.jsonl",
    )
    executor = RecordingExecutor(status="not_implemented", message="live executor stub")
    service = TradingService(
        config=config,
        binance=FixtureBinanceMarketDataClient(
            latest_tick=PriceTick(price=100_100.0, volume=10.0),
            candles=[
                Candle(open=99_980.0, high=100_000.0, low=99_970.0, close=100_000.0),
                Candle(open=100_000.0, high=100_040.0, low=99_995.0, close=100_030.0),
                Candle(open=100_030.0, high=100_110.0, low=100_020.0, close=100_060.0),
            ],
        ),
        polymarket=FixturePolymarketMarketClient(
            market=make_market(token_id_up="token-up", token_id_down="token-down", tick_size=0.01)
        ),
        chainlink=FixtureChainlinkReferenceClient(reference=100_000.0),
        risk_manager=RiskManager(config=config),
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "live_trade"
    assert result.execution_status == "not_implemented"
    assert result.execution_message == "live executor stub"
    assert len(executor.requests) == 1


def test_oneshot_settles_due_losses_before_risk_checks_and_blocks_new_trade(tmp_path: Path, monkeypatch):
    now = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    freeze_service_now(monkeypatch, now)
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    paper_trades_path.write_text(
        json.dumps(
            {
                "timestamp": (now - timedelta(minutes=10)).isoformat(),
                "market_id": "expired-loss",
                "interval": "5m",
                "side": "UP",
                "price": 0.55,
                "stake": 60.0,
                "expires_at": (now - timedelta(minutes=5)).isoformat(),
                "reference_price": 100200.0,
                "signal": {
                    "should_trade": True,
                    "side": "UP",
                    "signal_name": "momentum",
                    "confidence": 0.7,
                    "reasons": ["trend"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    service = make_service(paper_trades_path)

    result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "skip"
    assert "daily_drawdown_limit" in result.reasons
    records = [json.loads(line) for line in paper_trades_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["outcome"] == "loss"
    assert records[0]["pnl"] == -60.0


def test_oneshot_skips_malformed_or_legacy_paper_trade_lines_without_crashing(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    paper_trades_path.write_text(
        "\n".join(
            [
                "not-json",
                json.dumps(
                    {
                        "timestamp": "2026-04-18T00:00:00+00:00",
                        "market_id": "legacy-trade",
                        "interval": "5m",
                        "side": "UP",
                        "price": 0.51,
                        "stake": 20.0,
                        "signal": {
                            "should_trade": True,
                            "side": "UP",
                            "signal_name": "momentum",
                            "confidence": 0.7,
                            "reasons": ["trend"],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    service = make_service(paper_trades_path)

    result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "paper_trade"
    records = [json.loads(line) for line in paper_trades_path.read_text(encoding="utf-8").splitlines() if line.startswith("{")]
    assert records[-1]["market_id"] == "btc-5m-1"
    assert records[-1]["reference_price"] == 100000.0
    timestamp = datetime.fromisoformat(records[-1]["timestamp"])
    expires_at = datetime.fromisoformat(records[-1]["expires_at"])
    assert (expires_at - timestamp).total_seconds() == 240


def test_oneshot_loads_already_settled_rows_for_risk_on_fresh_service(tmp_path: Path, monkeypatch):
    now = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    freeze_service_now(monkeypatch, now)
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    paper_trades_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": (now - timedelta(minutes=16)).isoformat(),
                        "market_id": "settled-loss-1",
                        "interval": "5m",
                        "side": "UP",
                        "price": 0.55,
                        "stake": 30.0,
                        "expires_at": (now - timedelta(minutes=11)).isoformat(),
                        "reference_price": 100200.0,
                        "settled_at": (now - timedelta(minutes=10)).isoformat(),
                        "settlement_price": 100100.0,
                        "outcome": "loss",
                        "pnl": -30.0,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": (now - timedelta(minutes=8)).isoformat(),
                        "market_id": "settled-loss-2",
                        "interval": "5m",
                        "side": "DOWN",
                        "price": 0.45,
                        "stake": 25.0,
                        "expires_at": (now - timedelta(minutes=3)).isoformat(),
                        "reference_price": 100000.0,
                        "settled_at": (now - timedelta(minutes=2)).isoformat(),
                        "settlement_price": 100100.0,
                        "outcome": "loss",
                        "pnl": -25.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = make_service(paper_trades_path).oneshot(interval="5m", balance=1_000.0)

    assert result.action == "skip"
    assert "daily_drawdown_limit" in result.reasons


def test_oneshot_does_not_double_count_already_settled_rows_in_same_service(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    paper_trades_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-18T00:00:00+00:00",
                "market_id": "settled-loss",
                "interval": "5m",
                "side": "UP",
                "price": 0.55,
                "stake": 30.0,
                "expires_at": "2026-04-18T00:05:00+00:00",
                "reference_price": 100200.0,
                "settled_at": "2026-04-18T00:06:00+00:00",
                "settlement_price": 100100.0,
                "outcome": "loss",
                "pnl": -30.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    service = make_service(paper_trades_path)

    first = service.oneshot(interval="5m", balance=1_000.0)
    second = service.oneshot(interval="5m", balance=1_000.0)

    assert first.action == "paper_trade"
    assert second.action == "paper_trade"


def test_oneshot_hydrates_and_settles_even_without_an_active_market(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    paper_trades_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-18T00:00:00+00:00",
                        "market_id": "settled-loss",
                        "interval": "5m",
                        "side": "UP",
                        "price": 0.55,
                        "stake": 30.0,
                        "expires_at": "2026-04-18T00:05:00+00:00",
                        "reference_price": 100200.0,
                        "settled_at": "2026-04-18T00:06:00+00:00",
                        "settlement_price": 100100.0,
                        "outcome": "loss",
                        "pnl": -30.0,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-18T00:10:00+00:00",
                        "market_id": "due-loss",
                        "interval": "5m",
                        "side": "UP",
                        "price": 0.55,
                        "stake": 25.0,
                        "expires_at": "2026-04-18T00:15:00+00:00",
                        "reference_price": 100200.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = AppConfig(paper_trades_path=paper_trades_path)
    service = TradingService(
        config=config,
        binance=SettlementAwareBinanceClient(
            latest_tick=PriceTick(price=100_500.0, volume=10.0),
            candles=[
                Candle(open=99_980.0, high=100_000.0, low=99_970.0, close=100_000.0),
                Candle(open=100_000.0, high=100_040.0, low=99_995.0, close=100_030.0),
                Candle(open=100_030.0, high=100_110.0, low=100_020.0, close=100_060.0),
            ],
            price_at_map={"2026-04-18T00:15:00+00:00": 100_100.0},
        ),
        polymarket=NoActiveMarketClient(),
        chainlink=FixtureChainlinkReferenceClient(reference=100_000.0),
        risk_manager=RiskManager(config=config),
        recorder=PaperTradeRecorder(paper_trades_path),
    )

    result = service.oneshot(interval="5m", balance=1_000.0)

    assert result.action == "skip"
    assert result.reasons == ["no_active_market"]
    assert [trade.pnl for trade in service.risk_manager.closed_trades] == [-30.0, -25.0]
    records = [json.loads(line) for line in paper_trades_path.read_text(encoding="utf-8").splitlines()]
    assert records[1]["outcome"] == "loss"
    assert records[1]["settlement_price"] == 100100.0


def test_oneshot_settles_with_price_at_expiry_not_later_spot_price(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    paper_trades_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-18T00:10:00+00:00",
                "market_id": "due-up",
                "interval": "5m",
                "side": "UP",
                "price": 0.40,
                "stake": 20.0,
                "expires_at": "2026-04-18T00:15:00+00:00",
                "reference_price": 100000.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    service = TradingService(
        config=AppConfig(paper_trades_path=paper_trades_path),
        binance=SettlementAwareBinanceClient(
            latest_tick=PriceTick(price=100_300.0, volume=10.0),
            candles=[
                Candle(open=99_980.0, high=100_000.0, low=99_970.0, close=100_000.0),
                Candle(open=100_000.0, high=100_040.0, low=99_995.0, close=100_030.0),
                Candle(open=100_030.0, high=100_110.0, low=100_020.0, close=100_060.0),
            ],
            price_at_map={"2026-04-18T00:15:00+00:00": 99900.0},
        ),
        polymarket=FixturePolymarketMarketClient(market=make_market()),
        chainlink=FixtureChainlinkReferenceClient(reference=100_000.0),
        risk_manager=RiskManager(config=AppConfig(paper_trades_path=paper_trades_path)),
        recorder=PaperTradeRecorder(paper_trades_path),
    )

    service.oneshot(interval="5m", balance=1_000.0)

    record = json.loads(paper_trades_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["settlement_price"] == 99900.0
    assert record["outcome"] == "loss"
    assert record["pnl"] == -20.0


def test_oneshot_persists_market_absolute_end_time_when_available(tmp_path: Path):
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    class AbsoluteExpiryMarket:
        market_id = "btc-5m-absolute"
        slug = "btc-updown-5m-absolute"
        interval = "5m"
        active = True
        closed = False
        seconds_to_expiry = 240
        liquidity = 20_000.0
        spread = 0.02
        up = OrderBookSide(price=0.51)
        down = OrderBookSide(price=0.49)
        reference_price = 100_000.0
        end_date = "2026-04-18T00:05:00+00:00"

        def midpoint_distance(self) -> float:
            return min(abs(self.up.price - 0.5), abs(self.down.price - 0.5))

    absolute_market = AbsoluteExpiryMarket()
    service = TradingService(
        config=AppConfig(paper_trades_path=paper_trades_path),
        binance=FixtureBinanceMarketDataClient(
            latest_tick=PriceTick(price=100_100.0, volume=10.0),
            candles=[
                Candle(open=99_980.0, high=100_000.0, low=99_970.0, close=100_000.0),
                Candle(open=100_000.0, high=100_040.0, low=99_995.0, close=100_030.0),
                Candle(open=100_030.0, high=100_110.0, low=100_020.0, close=100_060.0),
            ],
        ),
        polymarket=FixturePolymarketMarketClient(market=absolute_market),
        chainlink=FixtureChainlinkReferenceClient(reference=100_000.0),
        risk_manager=RiskManager(config=AppConfig(paper_trades_path=paper_trades_path)),
        recorder=PaperTradeRecorder(paper_trades_path),
    )

    service.oneshot(interval="5m", balance=1_000.0)

    record = json.loads(paper_trades_path.read_text(encoding="utf-8").strip())
    assert record["expires_at"] == "2026-04-18T00:05:00+00:00"
