import pytest

from datetime import UTC, datetime
from pathlib import Path

from pm_bot.clients import BinanceMarketDataClient, PolymarketMarketClient, load_fixture_clients


def test_binance_latest_price_rejects_non_mapping_payload(monkeypatch):
    monkeypatch.setattr("pm_bot.clients._get_json", lambda url: [])

    client = BinanceMarketDataClient(base_url="https://example.com")

    with pytest.raises(ValueError, match="unexpected Binance ticker payload"):
        client.latest_price()


def test_polymarket_active_markets_rejects_non_list_payload(monkeypatch):
    monkeypatch.setattr("pm_bot.clients._get_json", lambda url: {"markets": []})

    client = PolymarketMarketClient(base_url="https://example.com")

    with pytest.raises(ValueError, match="unexpected Polymarket markets payload"):
        client.active_markets("5m")


def test_polymarket_active_markets_rejects_missing_up_down_prices(monkeypatch):
    monkeypatch.setattr(
        "pm_bot.clients._get_json",
        lambda url: [
            {
                "slug": "btc-updown-5m-test",
                "outcomes": '["UP", "DOWN"]',
                "outcomePrices": '["0.61"]',
            }
        ],
    )

    client = PolymarketMarketClient(base_url="https://example.com")

    with pytest.raises(ValueError, match="unexpected Polymarket outcome prices"):
        client.active_markets("5m")


def test_binance_price_at_uses_last_trade_at_or_before_expiry(monkeypatch):
    monkeypatch.setattr(
        "pm_bot.clients._get_json",
        lambda url: [
            {
                "a": 12344,
                "p": "100010.00",
                "q": "0.1",
                "f": 1,
                "l": 1,
                "T": 1_744_934_560_000,
                "m": True,
                "M": True,
            },
            {
                "a": 12345,
                "p": "100025.00",
                "q": "0.1",
                "f": 1,
                "l": 1,
                "T": 1_744_934_589_000,
                "m": True,
                "M": True,
            },
        ],
    )

    client = BinanceMarketDataClient(base_url="https://example.com")

    tick = client.price_at(datetime(2025, 4, 18, 0, 3, 30, tzinfo=UTC))

    assert tick.price == 100025.0


def test_polymarket_active_markets_parses_live_order_metadata(monkeypatch):
    monkeypatch.setattr(
        "pm_bot.clients._get_json",
        lambda url: [
            {
                "id": "market-1",
                "slug": "btc-updown-5m-test",
                "active": True,
                "closed": False,
                "liquidityNum": "1234.5",
                "spread": "0.03",
                "outcomes": '["UP", "DOWN"]',
                "outcomePrices": '["0.61", "0.39"]',
                "clobTokenIds": '["token-up", "token-down"]',
                "tickSize": "0.01",
                "negRisk": True,
            }
        ],
    )

    client = PolymarketMarketClient(base_url="https://example.com")

    [market] = client.active_markets("5m")

    assert market.token_id_up == "token-up"
    assert market.token_id_down == "token-down"
    assert market.tick_size == 0.01
    assert market.neg_risk is True


def test_polymarket_active_markets_rejects_malformed_clob_token_ids(monkeypatch):
    monkeypatch.setattr(
        "pm_bot.clients._get_json",
        lambda url: [
            {
                "slug": "btc-updown-5m-test",
                "outcomes": '["UP", "DOWN"]',
                "outcomePrices": '["0.61", "0.39"]',
                "clobTokenIds": '["token-up"',
            }
        ],
    )

    client = PolymarketMarketClient(base_url="https://example.com")

    with pytest.raises(ValueError, match="unexpected Polymarket clob token ids"):
        client.active_markets("5m")


def test_polymarket_discover_markets_returns_token_ids_when_present(monkeypatch):
    monkeypatch.setattr(
        "pm_bot.clients._get_json",
        lambda url: [
            {
                "id": "market-1",
                "slug": "btc-updown-1h-test",
                "question": "Will BTC go up in the next hour?",
                "description": "BTC range market",
                "active": True,
                "closed": False,
                "liquidityNum": "1234.5",
                "outcomes": ["UP", "DOWN"],
                "outcomePrices": ["0.52", "0.48"],
                "clobTokenIds": ["token-up", "token-down"],
                "tickSize": "0.01",
                "negRisk": False,
            }
        ],
    )

    client = PolymarketMarketClient(base_url="https://example.com")

    [market] = client.discover_markets(["btc"])

    assert market["token_id_up"] == "token-up"
    assert market["token_id_down"] == "token-down"
    assert market["tick_size"] == 0.01
    assert market["neg_risk"] is False


def test_polymarket_discover_markets_rejects_invalid_neg_risk(monkeypatch):
    monkeypatch.setattr(
        "pm_bot.clients._get_json",
        lambda url: [
            {
                "id": "market-1",
                "slug": "btc-updown-1h-test",
                "question": "Will BTC go up in the next hour?",
                "description": "BTC range market",
                "active": True,
                "closed": False,
                "liquidityNum": "1234.5",
                "outcomes": ["UP", "DOWN"],
                "outcomePrices": ["0.52", "0.48"],
                "negRisk": "maybe",
            }
        ],
    )

    client = PolymarketMarketClient(base_url="https://example.com")

    with pytest.raises(ValueError, match="unexpected Polymarket negRisk"):
        client.discover_markets(["btc"])


def test_load_fixture_clients_parses_live_order_metadata(tmp_path: Path):
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        """
        {
          "market": {
            "market_id": "fixture-btc-5m",
            "slug": "btc-updown-5m-fixture",
            "interval": "5m",
            "active": true,
            "closed": false,
            "seconds_to_expiry": 240,
            "liquidity": 20000,
            "spread": 0.02,
            "up": {"price": 0.51},
            "down": {"price": 0.49},
            "reference_price": 100000.0,
            "token_id_up": "fixture-up",
            "token_id_down": "fixture-down",
            "tick_size": 0.01,
            "neg_risk": true
          },
          "latest_price": {
            "price": 100060.0,
            "volume": 120.0
          },
          "candles": [
            {
              "open": 99980.0,
              "high": 100010.0,
              "low": 99970.0,
              "close": 100000.0
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    _, polymarket, _ = load_fixture_clients(fixture_path)

    [market] = polymarket.active_markets("5m")
    [diagnostic] = polymarket.discover_markets(["btc"])

    assert market.token_id_up == "fixture-up"
    assert market.token_id_down == "fixture-down"
    assert market.tick_size == 0.01
    assert market.neg_risk is True
    assert diagnostic["token_id_up"] == "fixture-up"
    assert diagnostic["token_id_down"] == "fixture-down"


def test_load_fixture_clients_rejects_invalid_tick_size(tmp_path: Path):
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        """
        {
          "market": {
            "market_id": "fixture-btc-5m",
            "slug": "btc-updown-5m-fixture",
            "interval": "5m",
            "active": true,
            "closed": false,
            "seconds_to_expiry": 240,
            "liquidity": 20000,
            "spread": 0.02,
            "up": {"price": 0.51},
            "down": {"price": 0.49},
            "tick_size": "NaN"
          },
          "latest_price": {
            "price": 100060.0,
            "volume": 120.0
          },
          "candles": [
            {
              "open": 99980.0,
              "high": 100010.0,
              "low": 99970.0,
              "close": 100000.0
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected fixture market tick_size"):
        load_fixture_clients(fixture_path)
