import pytest

from pm_bot.clients import BinanceMarketDataClient, PolymarketMarketClient


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
