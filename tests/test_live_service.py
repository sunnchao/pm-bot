from __future__ import annotations

from pathlib import Path

from pm_bot.clients import (
    FixtureBinanceMarketDataClient,
    FixtureChainlinkReferenceClient,
    FixturePolymarketMarketClient,
)
from pm_bot.config import AppConfig
from pm_bot.execution import ExecutionResult
from pm_bot.live_recorder import LiveOrderRecorder
from pm_bot.models import Candle, LiveOrderRecord, MarketSnapshot, OrderBookSide, PriceTick, SignalDecision
from pm_bot.live_service import LiveTradingService


def make_market(**overrides) -> MarketSnapshot:
    payload = {
        "market_id": "btc-5m-1",
        "slug": "btc-updown-5m",
        "interval": "5m",
        "active": True,
        "closed": False,
        "seconds_to_expiry": 240,
        "liquidity": 20_000.0,
        "spread": 0.02,
        "up": OrderBookSide(price=0.51),
        "down": OrderBookSide(price=0.49),
        "reference_price": 100_000.0,
        "token_id_up": "token-up",
        "token_id_down": "token-down",
        "tick_size": 0.01,
        "neg_risk": False,
    }
    payload.update(overrides)
    return MarketSnapshot(**payload)


def make_live_record(**overrides) -> LiveOrderRecord:
    payload = {
        "timestamp": "2026-04-19T00:00:00+00:00",
        "market_id": "btc-5m-1",
        "token_id": "token-down",
        "side": "DOWN",
        "submitted_price": 0.49,
        "submitted_size": 12.5,
        "status": "accepted",
        "submission_id": "submission-123",
        "order_id": "order-123",
        "message": None,
    }
    payload.update(overrides)
    return LiveOrderRecord(**payload)


class StubSignalEngine:
    def __init__(self) -> None:
        self.decision = SignalDecision(
            should_trade=True,
            side="DOWN",
            signal_name="test_signal",
            confidence=0.42,
            reasons=["test_reason"],
        )

    def decide(self, market, latest_tick, recent_ticks, candles):
        return self.decision


class StubRiskManager:
    def allow_trade(self, balance: float, now):
        return True, []

    def position_size(
        self,
        balance: float,
        decision: SignalDecision,
        *,
        live_mode: bool = False,
    ) -> float:
        return 20.0


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return ExecutionResult(
            action="live_trade",
            status="accepted",
            order_id="new-order-123",
            submission_id=request.submission_id,
            submitted_price=request.price,
            submitted_size=request.size_usd,
            message="accepted",
        )


class StubLiveClient:
    def __init__(
        self,
        payload_by_order_id: dict[str, dict],
        *,
        payload_by_order_hash: dict[str, dict] | None = None,
        replay_response: dict | None = None,
    ) -> None:
        self.payload_by_order_id = payload_by_order_id
        self.payload_by_order_hash = {} if payload_by_order_hash is None else payload_by_order_hash
        self.replay_response = replay_response
        self.get_order_calls: list[str] = []
        self.get_order_by_hash_calls: list[str] = []
        self.replay_calls: list[dict] = []

    def get_order(self, order_id: str):
        self.get_order_calls.append(order_id)
        return self.payload_by_order_id[order_id]

    def get_order_by_hash(self, order_hash: str):
        self.get_order_by_hash_calls.append(order_hash)
        return self.payload_by_order_hash.get(order_hash)

    def replay_signed_order_payload(self, signed_order_payload: dict):
        self.replay_calls.append(signed_order_payload)
        if self.replay_response is None:
            raise RuntimeError('replay not configured')
        return self.replay_response


def make_live_service(
    tmp_path: Path,
    *,
    live_client: StubLiveClient,
    live_recorder: LiveOrderRecorder,
    executor: RecordingExecutor,
) -> LiveTradingService:
    market = make_market()
    config = AppConfig(
        trading_mode="live",
        wallet_private_key="0xabc123",
        signature_type=0,
        funder_address="0x0000000000000000000000000000000000000001",
        live_allow_market_ids=(market.market_id,),
        live_max_order_usd=100.0,
        paper_trades_path=tmp_path / "paper_trades.jsonl",
        live_orders_path=tmp_path / "live_orders.jsonl",
    )
    return LiveTradingService(
        config=config,
        binance=FixtureBinanceMarketDataClient(
            latest_tick=PriceTick(price=100_100.0, volume=10.0),
            candles=[
                Candle(open=99_980.0, high=100_000.0, low=99_970.0, close=100_000.0),
                Candle(open=100_000.0, high=100_040.0, low=99_995.0, close=100_030.0),
                Candle(open=100_030.0, high=100_110.0, low=100_020.0, close=100_060.0),
            ],
        ),
        polymarket=FixturePolymarketMarketClient(market=market),
        chainlink=FixtureChainlinkReferenceClient(reference=market.reference_price),
        signal_engine=StubSignalEngine(),
        risk_manager=StubRiskManager(),
        executor=executor,
        live_client=live_client,
        live_recorder=live_recorder,
    )


def test_oneshot_reconciles_open_order_with_order_id_before_live_decision(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(make_live_record(status="accepted", order_id="order-123"))
    live_client = StubLiveClient(
        {
            "order-123": {
                "orderID": "order-123",
                "status": "matched",
                "errorMsg": "",
            }
        }
    )
    executor = RecordingExecutor()
    service = make_live_service(
        tmp_path,
        live_client=live_client,
        live_recorder=recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)
    journaled = recorder.get_by_submission_id("submission-123")

    assert live_client.get_order_calls == ["order-123"]
    assert journaled is not None
    assert journaled.status == "matched"
    assert journaled.order_id == "order-123"
    assert result.action == "live_trade"
    assert len(executor.requests) == 1


def test_oneshot_blocks_live_trading_for_pending_reconcile_without_order_id(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(make_live_record(status="pending_reconcile", order_id=None))
    live_client = StubLiveClient({})
    executor = RecordingExecutor()
    service = make_live_service(
        tmp_path,
        live_client=live_client,
        live_recorder=recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)

    assert result.action == "skip"
    assert result.reasons == ["live_order_pending_reconcile"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_order_pending_reconcile"
    assert live_client.get_order_calls == []
    assert executor.requests == []


def test_oneshot_does_not_place_new_live_order_while_reconciled_order_remains_open(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(make_live_record(status="accepted", order_id="order-123"))
    live_client = StubLiveClient(
        {
            "order-123": {
                "orderID": "order-123",
                "status": "open",
                "errorMsg": "",
            }
        }
    )
    executor = RecordingExecutor()
    service = make_live_service(
        tmp_path,
        live_client=live_client,
        live_recorder=recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)
    journaled = recorder.get_by_submission_id("submission-123")

    assert live_client.get_order_calls == ["order-123"]
    assert journaled is not None
    assert journaled.status == "open"
    assert result.action == "skip"
    assert result.reasons == ["live_order_open"]
    assert result.execution_status == "blocked_live_order"
    assert result.execution_message == "live_order_open"
    assert executor.requests == []


def test_oneshot_recovers_missing_order_id_via_order_hash_before_live_decision(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(
        make_live_record(
            status="pending_reconcile",
            order_id=None,
            order_hash="0xhash-123",
        )
    )
    live_client = StubLiveClient(
        {},
        payload_by_order_hash={
            "0xhash-123": {
                "id": "0xhash-123",
                "status": "matched",
                "errorMsg": "",
            }
        },
    )
    executor = RecordingExecutor()
    service = make_live_service(
        tmp_path,
        live_client=live_client,
        live_recorder=recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)
    journaled = recorder.get_by_submission_id("submission-123")

    assert live_client.get_order_by_hash_calls == ["0xhash-123"]
    assert journaled is not None
    assert journaled.order_id == "0xhash-123"
    assert journaled.status == "matched"
    assert result.action == "live_trade"
    assert len(executor.requests) == 1


def test_oneshot_replays_exact_signed_order_once_when_hash_lookup_misses(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(
        make_live_record(
            status="pending_reconcile",
            order_id=None,
            order_hash="0xhash-123",
            signed_order_payload={"order": {"salt": 1}},
        )
    )
    live_client = StubLiveClient(
        {"0xhash-123": {"id": "0xhash-123", "status": "open", "errorMsg": ""}},
        payload_by_order_hash={},
        replay_response={"success": False, "errorMsg": "order 0xhash123 is invalid. Duplicated."},
    )
    executor = RecordingExecutor()
    service = make_live_service(
        tmp_path,
        live_client=live_client,
        live_recorder=recorder,
        executor=executor,
    )

    result = service.oneshot(interval="5m", balance=1_000.0, live_confirmed=True)
    journaled = recorder.get_by_submission_id("submission-123")

    assert live_client.replay_calls == [{"order": {"salt": 1}}]
    assert live_client.get_order_calls == ["0xhash-123"]
    assert journaled is not None
    assert journaled.order_id == "0xhash-123"
    assert result.action == "skip"
    assert result.reasons == ["live_order_open"]
    assert result.execution_status == "blocked_live_order"
    assert executor.requests == []
