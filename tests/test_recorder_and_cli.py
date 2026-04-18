import json
import subprocess
import sys
from pathlib import Path

import pytest

from pm_bot.cli import build_parser, main
from pm_bot.config import AppConfig
from pm_bot.models import PaperTradeRecord, SignalDecision
from pm_bot.recorder import PaperTradeRecorder


def test_recorder_appends_jsonl(tmp_path: Path):
    recorder = PaperTradeRecorder(tmp_path / "paper_trades.jsonl")
    record = PaperTradeRecord(
        timestamp="2026-04-18T00:00:00Z",
        market_id="btc-5m-1",
        interval="5m",
        side="UP",
        price=0.51,
        stake=20.0,
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
    assert payload["signal"]["signal_name"] == "momentum"


def test_cli_supports_oneshot_and_loop():
    parser = build_parser()

    oneshot = parser.parse_args(["oneshot", "--interval", "5m"])
    loop = parser.parse_args(["loop", "--interval", "15m", "--sleep-seconds", "60"])

    assert oneshot.command == "oneshot"
    assert oneshot.interval == "5m"
    assert loop.command == "loop"
    assert loop.interval == "15m"
    assert loop.sleep_seconds == 60


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
    monkeypatch.setattr(
        "pm_bot.config.AppConfig.from_env",
        classmethod(lambda cls: AppConfig(paper_trades_path=paper_trades_path)),
    )
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
