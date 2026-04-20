import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pm_bot.cli import build_parser, main
from pm_bot.config import AppConfig
from pm_bot.models import PaperTradeRecord, SignalDecision
from pm_bot.recorder import PaperTradeRecorder
from pm_bot.service import OneShotResult


def test_recorder_appends_jsonl(tmp_path: Path):
    recorder = PaperTradeRecorder(tmp_path / "paper_trades.jsonl")
    record = PaperTradeRecord(
        timestamp="2026-04-18T00:00:00Z",
        market_id="btc-5m-1",
        interval="5m",
        side="UP",
        price=0.51,
        stake=20.0,
        expires_at="2026-04-18T00:05:00+00:00",
        reference_price=100000.0,
        signal=SignalDecision(
            should_trade=True,
            side="UP",
            signal_name="momentum",
            confidence=0.7,
            reasons=["trend"],
        ),
        notes=["paper"],
    )

    recorder.record(record)

    payload = json.loads((tmp_path / "paper_trades.jsonl").read_text().strip())
    assert payload["side"] == "UP"
    assert payload["expires_at"] == "2026-04-18T00:05:00+00:00"
    assert payload["reference_price"] == 100000.0
    assert payload["signal"]["signal_name"] == "momentum"


def test_recorder_settles_due_trades_with_win_loss_and_equal_up_win_outcomes(tmp_path: Path):
    recorder = PaperTradeRecorder(tmp_path / "paper_trades.jsonl")
    signal = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="momentum",
        confidence=0.7,
        reasons=["trend"],
    )
    recorder.record(
        PaperTradeRecord(
            timestamp="2026-04-18T00:00:00+00:00",
            market_id="win-up",
            interval="5m",
            side="UP",
            price=0.4,
            stake=20.0,
            expires_at="2026-04-18T00:05:00+00:00",
            reference_price=100000.0,
            signal=signal,
        )
    )
    recorder.record(
        PaperTradeRecord(
            timestamp="2026-04-18T00:00:00+00:00",
            market_id="loss-down",
            interval="5m",
            side="DOWN",
            price=0.25,
            stake=20.0,
            expires_at="2026-04-18T00:05:00+00:00",
            reference_price=100000.0,
            signal=signal,
        )
    )
    recorder.record(
        PaperTradeRecord(
            timestamp="2026-04-18T00:00:00+00:00",
            market_id="equal-up",
            interval="5m",
            side="UP",
            price=0.6,
            stake=20.0,
            expires_at="2026-04-18T00:05:00+00:00",
            reference_price=100100.0,
            signal=signal,
        )
    )

    settlements = recorder.settle_due(current_btc_price=100100.0, now=datetime(2026, 4, 18, 0, 6, tzinfo=UTC))

    assert [item["market_id"] for item in settlements] == ["win-up", "loss-down", "equal-up"]
    assert settlements[0]["outcome"] == "win"
    assert settlements[0]["pnl"] == 30.0
    assert settlements[1]["outcome"] == "loss"
    assert settlements[1]["pnl"] == -20.0
    assert settlements[2]["outcome"] == "win"
    assert settlements[2]["pnl"] == 13.33

    persisted = [
        json.loads(line)
        for line in (tmp_path / "paper_trades.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert persisted[0]["settlement_price"] == 100100.0
    assert persisted[0]["settled_at"] == "2026-04-18T00:06:00+00:00"
    assert persisted[1]["outcome"] == "loss"
    assert persisted[2]["outcome"] == "win"
    assert persisted[2]["pnl"] == 13.33


def test_recorder_settle_due_uses_expiry_as_closed_at_for_risk_windows(tmp_path: Path):
    recorder = PaperTradeRecorder(tmp_path / "paper_trades.jsonl")
    recorder.record(
        PaperTradeRecord(
            timestamp="2026-04-18T00:00:00+00:00",
            market_id="due-up",
            interval="5m",
            side="UP",
            price=0.4,
            stake=20.0,
            expires_at="2026-04-18T00:05:00+00:00",
            reference_price=100000.0,
            signal=SignalDecision(
                should_trade=True,
                side="UP",
                signal_name="momentum",
                confidence=0.7,
                reasons=["trend"],
            ),
        )
    )

    settlements = recorder.settle_due(current_btc_price=100100.0, now=datetime(2026, 4, 18, 0, 6, tzinfo=UTC))

    assert settlements[0]["closed_at"] == datetime(2026, 4, 18, 0, 5, tzinfo=UTC)


def test_recorder_settle_due_quantizes_half_cent_wins_with_decimal(tmp_path: Path):
    recorder = PaperTradeRecorder(tmp_path / "paper_trades.jsonl")
    recorder.record(
        PaperTradeRecord(
            timestamp="2026-04-18T00:00:00+00:00",
            market_id="decimal-win",
            interval="5m",
            side="UP",
            price=0.4,
            stake=20.01,
            expires_at="2026-04-18T00:05:00+00:00",
            reference_price=100000.0,
            signal=SignalDecision(
                should_trade=True,
                side="UP",
                signal_name="momentum",
                confidence=0.7,
                reasons=["trend"],
            ),
        )
    )

    settlements = recorder.settle_due(current_btc_price=100100.0, now=datetime(2026, 4, 18, 0, 6, tzinfo=UTC))

    assert settlements == [
        {
            "market_id": "decimal-win",
            "outcome": "win",
            "pnl": 30.02,
            "closed_at": datetime(2026, 4, 18, 0, 5, tzinfo=UTC),
        }
    ]
    persisted = json.loads((tmp_path / "paper_trades.jsonl").read_text(encoding="utf-8").strip())
    assert persisted["pnl"] == 30.02


def test_recorder_hydrates_closed_trades_from_expiry_instead_of_settled_at(tmp_path: Path):
    path = tmp_path / "paper_trades.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-17T23:55:00+00:00",
                "market_id": "settled-loss",
                "interval": "5m",
                "side": "UP",
                "price": 0.55,
                "stake": 30.0,
                "expires_at": "2026-04-17T23:59:00+00:00",
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

    trades = PaperTradeRecorder(path).settled_trades()

    assert trades == [
        {
            "market_id": "settled-loss",
            "pnl": -30.0,
            "closed_at": datetime(2026, 4, 17, 23, 59, tzinfo=UTC),
        }
    ]


def test_recorder_skips_legacy_records_without_settlement_metadata(tmp_path: Path):
    path = tmp_path / "paper_trades.jsonl"
    path.write_text(
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
        )
        + "\n",
        encoding="utf-8",
    )
    recorder = PaperTradeRecorder(path)

    settlements = recorder.settle_due(current_btc_price=100100.0, now=datetime(2026, 4, 18, 0, 6, tzinfo=UTC))

    assert settlements == []
    persisted = json.loads(path.read_text(encoding="utf-8").strip())
    assert persisted["market_id"] == "legacy-trade"
    assert "settled_at" not in persisted


def test_recorder_skips_naive_expires_at_without_crashing_or_rewriting_line(tmp_path: Path):
    path = tmp_path / "paper_trades.jsonl"
    raw_line = json.dumps(
        {
            "timestamp": "2026-04-18T00:00:00+00:00",
            "market_id": "naive-expiry",
            "interval": "5m",
            "side": "UP",
            "price": 0.51,
            "stake": 20.0,
            "expires_at": "2026-04-18T00:05:00",
            "reference_price": 100000.0,
        }
    )
    path.write_text(raw_line + "\n", encoding="utf-8")
    recorder = PaperTradeRecorder(path)

    settlements = recorder.settle_due(current_btc_price=100100.0, now=datetime(2026, 4, 18, 0, 6, tzinfo=UTC))

    assert settlements == []
    assert path.read_text(encoding="utf-8") == raw_line + "\n"


def test_recorder_preserves_malformed_and_non_finite_lines_while_settling_valid_rows(tmp_path: Path):
    path = tmp_path / "paper_trades.jsonl"
    valid_due = json.dumps(
        {
            "timestamp": "2026-04-18T00:00:00+00:00",
            "market_id": "valid-due",
            "interval": "5m",
            "side": "UP",
            "price": 0.4,
            "stake": 20.0,
            "expires_at": "2026-04-18T00:05:00+00:00",
            "reference_price": 100000.0,
        }
    )
    malformed = "not-json"
    non_finite = '{"timestamp":"2026-04-18T00:00:00+00:00","market_id":"bad-nan","interval":"5m","side":"UP","price":0.4,"stake":NaN,"expires_at":"2026-04-18T00:05:00+00:00","reference_price":100000.0}'
    path.write_text("\n".join([valid_due, malformed, non_finite]) + "\n", encoding="utf-8")
    recorder = PaperTradeRecorder(path)

    settlements = recorder.settle_due(current_btc_price=100100.0, now=datetime(2026, 4, 18, 0, 6, tzinfo=UTC))

    assert [item["market_id"] for item in settlements] == ["valid-due"]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == malformed
    assert lines[2] == non_finite
    persisted = json.loads(lines[0])
    assert persisted["market_id"] == "valid-due"
    assert persisted["outcome"] == "win"


def test_recorder_settle_due_does_not_truncate_the_ledger_in_place(tmp_path: Path, monkeypatch):
    path = tmp_path / "paper_trades.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-18T00:00:00+00:00",
                "market_id": "valid-due",
                "interval": "5m",
                "side": "UP",
                "price": 0.4,
                "stake": 20.0,
                "expires_at": "2026-04-18T00:05:00+00:00",
                "reference_price": 100000.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recorder = PaperTradeRecorder(path)
    original_open = Path.open

    def guarded_open(self: Path, mode: str = "r", *args, **kwargs):
        if self == path and "w" in mode:
            raise AssertionError("ledger must not be rewritten in place")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    settlements = recorder.settle_due(current_btc_price=100100.0, now=datetime(2026, 4, 18, 0, 6, tzinfo=UTC))

    assert [item["market_id"] for item in settlements] == ["valid-due"]
    persisted = json.loads(path.read_text(encoding="utf-8").strip())
    assert persisted["settlement_price"] == 100100.0


def test_recorder_serializes_record_and_settle_due_to_avoid_lost_updates(tmp_path: Path, monkeypatch):
    path = tmp_path / "paper_trades.jsonl"
    recorder = PaperTradeRecorder(path)
    recorder.record(
        PaperTradeRecord(
            timestamp="2026-04-18T00:00:00+00:00",
            market_id="due-trade",
            interval="5m",
            side="UP",
            price=0.4,
            stake=20.0,
            expires_at="2026-04-18T00:05:00+00:00",
            reference_price=100000.0,
            signal=SignalDecision(
                should_trade=True,
                side="UP",
                signal_name="momentum",
                confidence=0.7,
                reasons=["trend"],
            ),
        )
    )

    read_started = threading.Event()
    release_read = threading.Event()
    original_read_text = Path.read_text

    def blocking_read_text(self: Path, *args, **kwargs):
        content = original_read_text(self, *args, **kwargs)
        if self == path and not read_started.is_set():
            read_started.set()
            release_read.wait(timeout=2)
        return content

    monkeypatch.setattr(Path, "read_text", blocking_read_text)

    settle_thread = threading.Thread(
        target=lambda: recorder.settle_due(
            current_btc_price=100100.0,
            now=datetime(2026, 4, 18, 0, 6, tzinfo=UTC),
        )
    )
    record_thread = threading.Thread(
        target=lambda: recorder.record(
            PaperTradeRecord(
                timestamp="2026-04-18T00:06:30+00:00",
                market_id="new-trade",
                interval="5m",
                side="DOWN",
                price=0.48,
                stake=20.0,
                expires_at="2026-04-18T00:10:00+00:00",
                reference_price=100100.0,
                signal=SignalDecision(
                    should_trade=True,
                    side="DOWN",
                    signal_name="fade",
                    confidence=0.6,
                    reasons=["mean_reversion"],
                ),
            )
        )
    )

    settle_thread.start()
    assert read_started.wait(timeout=2)
    record_thread.start()
    release_read.set()
    settle_thread.join(timeout=2)
    record_thread.join(timeout=2)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["market_id"] for record in records] == ["due-trade", "new-trade"]
    assert records[0]["outcome"] == "win"


def test_app_config_defaults_keep_live_trading_paper_safe():
    config = AppConfig.from_env()

    assert config.trading_mode == "paper"
    assert config.polymarket_host == "https://clob.polymarket.com"
    assert config.polygon_chain_id == 137
    assert config.wallet_private_key is None
    assert config.signature_type is None
    assert config.funder_address is None
    assert config.live_max_order_usd == 10.0
    assert config.live_allow_market_ids == ()
    assert config.live_require_explicit_confirm is True
    assert config.paper_trades_path == Path("data/paper_trades.jsonl")
    assert config.live_orders_path == Path("data/live_orders.jsonl")


def test_app_config_from_env_reads_live_fields(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("POLYMARKET_HOST", "https://example-clob.invalid")
    monkeypatch.setenv("POLYGON_CHAIN_ID", "80002")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("SIGNATURE_TYPE", "2")
    monkeypatch.setenv("FUNDER_ADDRESS", "0x0000000000000000000000000000000000000001")
    monkeypatch.setenv("LIVE_MAX_ORDER_USD", "25.5")
    monkeypatch.setenv("LIVE_ALLOW_MARKET_IDS", "btc-5m-1, btc-15m-1 ,, ")
    monkeypatch.setenv("LIVE_REQUIRE_EXPLICIT_CONFIRM", "false")
    monkeypatch.setenv("PAPER_TRADES_PATH", "runtime/paper.jsonl")
    monkeypatch.setenv("LIVE_ORDERS_PATH", "runtime/live.jsonl")

    config = AppConfig.from_env()

    assert config.trading_mode == "live"
    assert config.polymarket_host == "https://example-clob.invalid"
    assert config.polygon_chain_id == 80002
    assert config.wallet_private_key == "0xabc123"
    assert config.signature_type == 2
    assert config.funder_address == "0x0000000000000000000000000000000000000001"
    assert config.live_max_order_usd == 25.5
    assert config.live_allow_market_ids == ("btc-5m-1", "btc-15m-1")
    assert config.live_require_explicit_confirm is False
    assert config.paper_trades_path == Path("runtime/paper.jsonl")
    assert config.live_orders_path == Path("runtime/live.jsonl")


def test_app_config_from_env_rejects_non_finite_float(monkeypatch):
    monkeypatch.setenv("LIVE_MAX_ORDER_USD", "NaN")

    with pytest.raises(ValueError, match="invalid float for LIVE_MAX_ORDER_USD"):
        AppConfig.from_env()


def test_app_config_from_env_rejects_non_positive_min_seconds(monkeypatch):
    monkeypatch.setenv("MIN_SECONDS_5M", "-1")

    with pytest.raises(ValueError, match="invalid positive int for MIN_SECONDS_5M"):
        AppConfig.from_env()


def test_cli_supports_oneshot_loop_and_live_commands():
    parser = build_parser()

    oneshot = parser.parse_args(["oneshot", "--interval", "5m"])
    loop = parser.parse_args(["loop", "--interval", "15m", "--sleep-seconds", "60"])
    live = parser.parse_args(["live", "--interval", "5m", "--confirm-live"])
    live_loop = parser.parse_args(["live-loop", "--interval", "15m", "--sleep-seconds", "60", "--confirm-live"])

    assert oneshot.command == "oneshot"
    assert oneshot.interval == "5m"
    assert loop.command == "loop"
    assert loop.interval == "15m"
    assert loop.sleep_seconds == 60
    assert live.command == "live"
    assert live.interval == "5m"
    assert live.confirm_live is True
    assert live_loop.command == "live-loop"
    assert live_loop.interval == "15m"
    assert live_loop.sleep_seconds == 60
    assert live_loop.confirm_live is True


def test_cli_supports_discover_and_fixture():
    parser = build_parser()

    discover = parser.parse_args(["discover", "--keyword", "btc", "--keyword", "bitcoin", "--limit", "7"])
    oneshot = parser.parse_args(["oneshot", "--interval", "5m", "--fixture", "tests/fixtures/oneshot.json"])

    assert discover.command == "discover"
    assert discover.keyword == ["btc", "bitcoin"]
    assert discover.limit == 7
    assert oneshot.fixture == "tests/fixtures/oneshot.json"


def test_discover_prints_json(monkeypatch, capsys):
    class StubService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def discover(self, keywords: list[str], limit: int) -> list[dict]:
            assert keywords == ["btc", "bitcoin"]
            assert limit == 2
            return [
                {
                    "slug": "bitcoin-up-or-down-apr-18-12pm-et",
                    "question": "Bitcoin up or down?",
                    "active": True,
                    "closed": False,
                    "liquidity": 12345.0,
                    "seconds_to_expiry": 180,
                    "yes_price": 0.53,
                    "no_price": 0.47,
                }
            ]

    monkeypatch.setattr("pm_bot.cli.TradingService", StubService)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pm-bot", "discover", "--keyword", "btc", "--keyword", "bitcoin", "--limit", "2"],
    )

    exit_code = main()

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output[0]["slug"] == "bitcoin-up-or-down-apr-18-12pm-et"
    assert output[0]["yes_price"] == 0.53


def test_discover_does_not_create_paper_trade_directory(monkeypatch, tmp_path: Path, capsys):
    paper_trades_path = tmp_path / "runtime" / "paper_trades.jsonl"
    monkeypatch.setattr(
        "pm_bot.config.AppConfig.from_env",
        classmethod(lambda cls: AppConfig(paper_trades_path=paper_trades_path)),
    )
    monkeypatch.setattr("pm_bot.clients.PolymarketMarketClient.discover_markets", lambda self, keywords, limit: [])
    monkeypatch.setattr(sys, "argv", ["pm-bot", "discover", "--keyword", "btc", "--limit", "1"])

    exit_code = main()

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not paper_trades_path.parent.exists()


def test_discover_network_error_returns_json(monkeypatch, capsys):
    def raise_network_error(self, keywords: list[str], limit: int) -> list[dict]:
        raise OSError("network down")

    monkeypatch.setattr("pm_bot.clients.PolymarketMarketClient.discover_markets", raise_network_error)
    monkeypatch.setattr(sys, "argv", ["pm-bot", "discover", "--keyword", "btc", "--limit", "1"])

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "error"
    assert output["error"] == "network down"


def test_discover_type_error_returns_json(monkeypatch, capsys):
    def raise_shape_error(self, keywords: list[str], limit: int) -> list[dict]:
        raise TypeError("bad market payload")

    monkeypatch.setattr("pm_bot.clients.PolymarketMarketClient.discover_markets", raise_shape_error)
    monkeypatch.setattr(sys, "argv", ["pm-bot", "discover", "--keyword", "btc", "--limit", "1"])

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "error"
    assert output["error"] == "bad market payload"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["pm-bot", "discover", "--limit", "-1"], "limit must be >= 0"),
        (["pm-bot", "oneshot", "--interval", "5m", "--balance", "-1"], "balance must be >= 0"),
        (["pm-bot", "loop", "--interval", "5m", "--sleep-seconds", "-5"], "sleep_seconds must be >= 0"),
        (["pm-bot", "loop", "--interval", "5m", "--iterations", "-1"], "iterations must be >= 0"),
    ],
)
def test_cli_rejects_negative_numeric_arguments(monkeypatch, capsys, argv: list[str], message: str):
    monkeypatch.setattr(sys, "argv", argv)

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "error"
    assert output["error"] == message


@pytest.mark.parametrize(
    ("argv", "message_fragment"),
    [
        (["pm-bot", "oneshot", "--interval", "5m", "--balance", "abc"], "argument --balance"),
        (["pm-bot", "discover", "--limit", "abc"], "argument --limit"),
    ],
)
def test_cli_rejects_non_numeric_arguments(monkeypatch, capsys, argv: list[str], message_fragment: str):
    monkeypatch.setattr(sys, "argv", argv)

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "error"
    assert message_fragment in output["error"]



def test_live_command_rejects_missing_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pm-bot", "live", "--interval", "5m"])

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "error"
    assert output["error"] == "--confirm-live is required for live trading"



def test_live_command_validates_required_wallet_config_before_building_live_path(monkeypatch, capsys):
    monkeypatch.setattr(
        "pm_bot.cli.AppConfig.from_env",
        classmethod(
            lambda cls: AppConfig(
                trading_mode="live",
                live_allow_market_ids=("btc-5m-1",),
                live_max_order_usd=100.0,
            )
        ),
    )
    monkeypatch.setattr(
        "pm_bot.cli.LiveOrderRecorder",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live recorder should not be constructed")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pm-bot", "live", "--interval", "5m", "--confirm-live"],
    )

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "error"
    assert output["error"] == "live wallet config incomplete"



def test_live_command_builds_live_service_executor_path(monkeypatch, tmp_path: Path, capsys):
    config = AppConfig(
        trading_mode="live",
        wallet_private_key="0xabc123",
        signature_type=0,
        funder_address="0x0000000000000000000000000000000000000001",
        live_allow_market_ids=("btc-5m-1",),
        live_max_order_usd=100.0,
        live_orders_path=tmp_path / "runtime" / "live_orders.jsonl",
    )
    created: dict[str, object] = {}

    class StubLiveOrderRecorder:
        def __init__(self, path: Path) -> None:
            self.path = path
            created["recorder"] = self

    class StubLivePolymarketExecutor:
        def __init__(self, config: AppConfig | None = None, client=None, recorder=None) -> None:
            assert config is not None
            assert recorder is created["recorder"]
            self.config = config
            self.client = client
            self.recorder = recorder
            created["executor"] = self

        def get_order(self, order_id: str):
            raise AssertionError("live reconciliation should not run in this CLI wiring test")

    class StubLiveTradingService:
        def __init__(self, *args, config: AppConfig | None = None, executor=None, live_recorder=None, **kwargs) -> None:
            assert config is not None
            assert executor is created["executor"]
            assert live_recorder is created["recorder"]
            assert executor.recorder is live_recorder
            created["service"] = self

        def oneshot(self, interval: str, balance: float = 1_000.0, *, live_confirmed: bool = False):
            created["oneshot_call"] = {
                "interval": interval,
                "balance": balance,
                "live_confirmed": live_confirmed,
            }
            return OneShotResult(
                interval=interval,
                market_id="btc-5m-1",
                action="live_trade",
                reasons=[],
                execution_status="accepted",
                execution_message="accepted",
                order_id="order-123",
                submission_id="submission-123",
            )

    monkeypatch.setattr("pm_bot.cli.AppConfig.from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        "pm_bot.cli.TradingService",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paper service should not be constructed")),
    )
    monkeypatch.setattr("pm_bot.cli.LiveOrderRecorder", StubLiveOrderRecorder)
    monkeypatch.setattr("pm_bot.cli.LivePolymarketExecutor", StubLivePolymarketExecutor)
    monkeypatch.setattr("pm_bot.cli.LiveTradingService", StubLiveTradingService)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pm-bot", "live", "--interval", "5m", "--balance", "250", "--confirm-live"],
    )

    exit_code = main()

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "live_trade"
    assert created["oneshot_call"] == {"interval": "5m", "balance": 250.0, "live_confirmed": True}
    recorder = created["recorder"]
    assert isinstance(recorder, StubLiveOrderRecorder)
    assert recorder.path == config.live_orders_path



def test_live_loop_reuses_same_live_path_and_stops_after_requested_iterations(monkeypatch, tmp_path: Path, capsys):
    config = AppConfig(
        trading_mode="live",
        wallet_private_key="0xabc123",
        signature_type=0,
        funder_address="0x0000000000000000000000000000000000000001",
        live_allow_market_ids=("btc-15m-1",),
        live_max_order_usd=100.0,
        live_orders_path=tmp_path / "runtime" / "live_orders.jsonl",
    )
    created_recorders: list[object] = []
    sleep_calls: list[int] = []

    class StubLiveOrderRecorder:
        def __init__(self, path: Path) -> None:
            self.path = path
            created_recorders.append(self)

    class StubLivePolymarketExecutor:
        def __init__(self, config: AppConfig | None = None, client=None, recorder=None) -> None:
            self.config = config
            self.client = client
            self.recorder = recorder

        def get_order(self, order_id: str):
            raise AssertionError("live reconciliation should not run in this CLI loop test")

    class StubLiveTradingService:
        instances: list["StubLiveTradingService"] = []

        def __init__(self, *args, config: AppConfig | None = None, executor=None, live_recorder=None, **kwargs) -> None:
            assert config is not None
            assert executor is not None
            assert live_recorder is not None
            assert executor.recorder is live_recorder
            self.calls: list[dict[str, object]] = []
            self.live_recorder = live_recorder
            StubLiveTradingService.instances.append(self)

        def oneshot(self, interval: str, balance: float = 1_000.0, *, live_confirmed: bool = False):
            self.calls.append(
                {
                    "interval": interval,
                    "balance": balance,
                    "live_confirmed": live_confirmed,
                    "path": self.live_recorder.path,
                }
            )
            iteration = len(self.calls)
            return OneShotResult(
                interval=interval,
                market_id=f"btc-15m-{iteration}",
                action="live_trade",
                reasons=[],
                execution_status="accepted",
                execution_message=f"accepted-{iteration}",
                order_id=f"order-{iteration}",
                submission_id=f"submission-{iteration}",
            )

    monkeypatch.setattr("pm_bot.cli.AppConfig.from_env", classmethod(lambda cls: config))
    monkeypatch.setattr("pm_bot.cli.LiveOrderRecorder", StubLiveOrderRecorder)
    monkeypatch.setattr("pm_bot.cli.LivePolymarketExecutor", StubLivePolymarketExecutor)
    monkeypatch.setattr("pm_bot.cli.LiveTradingService", StubLiveTradingService)
    monkeypatch.setattr("pm_bot.cli.time.sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pm-bot",
            "live-loop",
            "--interval",
            "15m",
            "--balance",
            "250",
            "--sleep-seconds",
            "7",
            "--iterations",
            "2",
            "--confirm-live",
        ],
    )

    exit_code = main()

    assert exit_code == 0
    assert len(created_recorders) == 1
    assert len(StubLiveTradingService.instances) == 1
    service = StubLiveTradingService.instances[0]
    assert service.calls == [
        {
            "interval": "15m",
            "balance": 250.0,
            "live_confirmed": True,
            "path": config.live_orders_path,
        },
        {
            "interval": "15m",
            "balance": 250.0,
            "live_confirmed": True,
            "path": config.live_orders_path,
        },
    ]
    assert sleep_calls == [7]
    output_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["submission_id"] for line in output_lines] == ["submission-1", "submission-2"]


def test_live_command_returns_structured_json_for_runtime_failures(monkeypatch, capsys):
    class StubLiveTradingService:
        def oneshot(self, interval: str, balance: float = 1_000.0, *, live_confirmed: bool = False):
            raise RuntimeError("live venue unavailable")

    monkeypatch.setattr("pm_bot.cli._build_live_service", lambda: StubLiveTradingService())
    monkeypatch.setattr(
        sys,
        "argv",
        ["pm-bot", "live", "--interval", "5m", "--confirm-live"],
    )

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {"action": "error", "error": "live venue unavailable"}


@pytest.mark.parametrize(
    "argv",
    [
        ["pm-bot", "oneshot", "--interval", "5m"],
        ["pm-bot", "loop", "--interval", "5m", "--iterations", "1"],
    ],
)
def test_paper_commands_stay_paper_safe_when_env_requests_live(monkeypatch, tmp_path: Path, capsys, argv: list[str]):
    paper_config = AppConfig(trading_mode="paper", paper_trades_path=tmp_path / "paper_trades.jsonl")
    built_configs: list[AppConfig] = []

    class StubTradingService:
        def __init__(self, config: AppConfig | None = None, *args, **kwargs) -> None:
            self.config = config or AppConfig.from_env()
            built_configs.append(self.config)

        def oneshot(self, interval: str, balance: float = 1_000.0, *, live_confirmed: bool = False):
            assert self.config.trading_mode == "paper"
            assert live_confirmed is False
            return OneShotResult(
                interval=interval,
                market_id="paper-btc-5m",
                action="paper_trade",
                reasons=[],
            )

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("SIGNATURE_TYPE", "0")
    monkeypatch.setenv("FUNDER_ADDRESS", "0x0000000000000000000000000000000000000001")
    monkeypatch.setattr("pm_bot.cli.AppConfig.paper_from_env", classmethod(lambda cls: paper_config))
    monkeypatch.setattr(
        "pm_bot.cli.AppConfig.from_env",
        classmethod(
            lambda cls: (_ for _ in ()).throw(AssertionError("paper commands must not load live config"))
        ),
    )
    monkeypatch.setattr("pm_bot.cli.TradingService", StubTradingService)
    monkeypatch.setattr(sys, "argv", argv)

    exit_code = main()

    assert exit_code == 0
    assert built_configs == [paper_config]
    output_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output_lines == [
        {
            "interval": "5m",
            "market_id": "paper-btc-5m",
            "action": "paper_trade",
            "reasons": [],
            "signal_name": None,
            "confidence": 0.0,
            "side": None,
            "stake": 0.0,
            "execution_status": None,
            "execution_message": None,
            "order_id": None,
            "submission_id": None,
        }
    ]


def test_oneshot_fixture_runs_full_pipeline(monkeypatch, tmp_path: Path, capsys):
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "market": {
                    "market_id": "fixture-btc-5m",
                    "slug": "btc-updown-5m-fixture",
                    "interval": "5m",
                    "active": True,
                    "closed": False,
                    "seconds_to_expiry": 240,
                    "liquidity": 20000,
                    "spread": 0.02,
                    "up": {"price": 0.51},
                    "down": {"price": 0.49},
                    "reference_price": 100000.0,
                },
                "latest_price": {"price": 100060.0, "volume": 120.0},
                "candles": [
                    {"open": 99980.0, "high": 100010.0, "low": 99970.0, "close": 100000.0},
                    {"open": 100000.0, "high": 100040.0, "low": 99995.0, "close": 100030.0},
                    {"open": 100030.0, "high": 100070.0, "low": 100020.0, "close": 100060.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    monkeypatch.setenv("PAPER_TRADES_PATH", str(paper_trades_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["pm-bot", "oneshot", "--interval", "5m", "--fixture", str(fixture_path)],
    )

    exit_code = main()

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "paper_trade"
    assert output["signal_name"] == "oracle_delay"
    records = [json.loads(line) for line in paper_trades_path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["market_id"] == "fixture-btc-5m"
    assert records[0]["signal"]["signal_name"] == "oracle_delay"


def test_oneshot_fixture_stays_paper_only_with_live_config_present(monkeypatch, tmp_path: Path, capsys):
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "market": {
                    "market_id": "fixture-btc-5m",
                    "slug": "btc-updown-5m-fixture",
                    "interval": "5m",
                    "active": True,
                    "closed": False,
                    "seconds_to_expiry": 240,
                    "liquidity": 20000,
                    "spread": 0.02,
                    "up": {"price": 0.51},
                    "down": {"price": 0.49},
                    "reference_price": 100000.0,
                },
                "latest_price": {"price": 100060.0, "volume": 120.0},
                "candles": [
                    {"open": 99980.0, "high": 100010.0, "low": 99970.0, "close": 100000.0},
                    {"open": 100000.0, "high": 100040.0, "low": 99995.0, "close": 100030.0},
                    {"open": 100030.0, "high": 100070.0, "low": 100020.0, "close": 100060.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    live_orders_path = tmp_path / "live_orders.jsonl"
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0xabc123")
    monkeypatch.setenv("SIGNATURE_TYPE", "0")
    monkeypatch.setenv("FUNDER_ADDRESS", "0x0000000000000000000000000000000000000001")
    monkeypatch.setenv("LIVE_ALLOW_MARKET_IDS", "fixture-btc-5m")
    monkeypatch.setenv("LIVE_MAX_ORDER_USD", "NaN")
    monkeypatch.setenv("PAPER_TRADES_PATH", str(paper_trades_path))
    monkeypatch.setenv("LIVE_ORDERS_PATH", str(live_orders_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["pm-bot", "oneshot", "--interval", "5m", "--fixture", str(fixture_path)],
    )

    exit_code = main()

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "paper_trade"
    assert paper_trades_path.exists()
    assert not live_orders_path.exists()


def test_oneshot_missing_fixture_returns_json_error(monkeypatch, tmp_path: Path, capsys):
    missing_fixture = tmp_path / "missing.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["pm-bot", "oneshot", "--interval", "5m", "--fixture", str(missing_fixture)],
    )

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "error"
    assert str(missing_fixture) in output["error"]


@pytest.mark.parametrize(
    ("market_patch", "message"),
    [
        ({"reference_price": "NaN"}, "unexpected fixture market reference_price"),
        ({"up": {"price": "NaN"}}, "unexpected fixture market up.price"),
    ],
)
def test_oneshot_fixture_rejects_non_finite_market_numbers(
    monkeypatch,
    tmp_path: Path,
    capsys,
    market_patch: dict,
    message: str,
):
    market = {
        "market_id": "fixture-btc-5m",
        "slug": "btc-updown-5m-fixture",
        "interval": "5m",
        "active": True,
        "closed": False,
        "seconds_to_expiry": 240,
        "liquidity": 20000,
        "spread": 0.02,
        "up": {"price": 0.51},
        "down": {"price": 0.49},
        "reference_price": 100000.0,
    }
    market.update(market_patch)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "market": market,
                "latest_price": {"price": 100060.0, "volume": 120.0},
                "candles": [
                    {"open": 99980.0, "high": 100010.0, "low": 99970.0, "close": 100000.0},
                    {"open": 100000.0, "high": 100040.0, "low": 99995.0, "close": 100030.0},
                    {"open": 100030.0, "high": 100070.0, "low": 100020.0, "close": 100060.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pm-bot", "oneshot", "--interval", "5m", "--fixture", str(fixture_path)],
    )

    exit_code = main()

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "error"
    assert output["error"] == message


def test_python_m_pm_bot_cli_executes_with_fixture(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "market": {
                    "market_id": "fixture-btc-5m",
                    "slug": "btc-updown-5m-fixture",
                    "interval": "5m",
                    "active": True,
                    "closed": False,
                    "seconds_to_expiry": 240,
                    "liquidity": 20000,
                    "spread": 0.02,
                    "up": {"price": 0.51},
                    "down": {"price": 0.49},
                    "reference_price": 100000.0,
                },
                "latest_price": {"price": 100060.0, "volume": 120.0},
                "candles": [
                    {"open": 99980.0, "high": 100010.0, "low": 99970.0, "close": 100000.0},
                    {"open": 100000.0, "high": 100040.0, "low": 99995.0, "close": 100030.0},
                    {"open": 100030.0, "high": 100070.0, "low": 100020.0, "close": 100060.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pm_bot.cli",
            "oneshot",
            "--interval",
            "5m",
            "--fixture",
            str(fixture_path),
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(repo_root / "src")},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["action"] == "paper_trade"
