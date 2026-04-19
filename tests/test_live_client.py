from __future__ import annotations

from dataclasses import dataclass

import pytest

from pm_bot.config import AppConfig
from pm_bot.execution import ExecutionRequest


def make_config(**overrides) -> AppConfig:
    base = dict(
        trading_mode="live",
        polymarket_host="https://clob.polymarket.com",
        polygon_chain_id=137,
        wallet_private_key="0x1111111111111111111111111111111111111111111111111111111111111111",
        signature_type=0,
        funder_address="0x0000000000000000000000000000000000000001",
    )
    base.update(overrides)
    return AppConfig(**base)


def make_request(**overrides) -> ExecutionRequest:
    base = dict(
        market_id="market-1",
        token_id="token-down",
        side="DOWN",
        price=0.49,
        size_usd=12.5,
        order_type="market",
        metadata={"tick_size": 0.01, "neg_risk": True},
    )
    base.update(overrides)
    return ExecutionRequest(**base)


@dataclass
class RecordedMarketOrder:
    token_id: str
    amount: float
    side: object
    order_type: object
    price: float = 0.0


@dataclass
class RecordedPartialOptions:
    tick_size: str | None = None
    neg_risk: bool | None = None


def test_live_client_rejects_incomplete_wallet_config():
    from pm_bot.polymarket_live_client import PolymarketLiveClient

    with pytest.raises(ValueError, match="live wallet config incomplete"):
        PolymarketLiveClient(make_config(wallet_private_key="   "))


def test_live_client_rejects_non_market_orders():
    from pm_bot.polymarket_live_client import PolymarketLiveClient

    client = PolymarketLiveClient(make_config())

    with pytest.raises(ValueError, match="only market orders are supported"):
        client.post_order(make_request(order_type="limit"))


def test_live_client_posts_market_order_and_caches_authenticated_client(monkeypatch):
    from pm_bot import polymarket_live_client as live_module
    from pm_bot.polymarket_live_client import PolymarketLiveClient

    instances: list[FakeClobClient] = []

    class FakeClobClient:
        def __init__(
            self,
            host,
            chain_id,
            key=None,
            creds=None,
            signature_type=None,
            funder=None,
            builder_config=None,
            use_server_time=False,
            retry_on_error=False,
        ) -> None:
            self.host = host
            self.chain_id = chain_id
            self.key = key
            self.creds = creds
            self.signature_type = signature_type
            self.funder = funder
            self.set_api_creds_calls = []
            self.create_or_derive_api_key_calls = 0
            self.market_orders = []
            self.get_order_calls = []
            instances.append(self)

        def create_or_derive_api_key(self):
            self.create_or_derive_api_key_calls += 1
            return live_module.ApiCreds(
                api_key="api-key",
                api_secret="api-secret",
                api_passphrase="passphrase",
            )

        def set_api_creds(self, creds):
            self.creds = creds
            self.set_api_creds_calls.append(creds)

        def create_and_post_market_order(self, order_args, options=None, order_type=None):
            self.market_orders.append(
                {
                    "order_args": order_args,
                    "options": options,
                    "order_type": order_type,
                }
            )
            return {"success": True, "orderID": "order-123", "status": "live"}

        def get_order(self, order_id):
            self.get_order_calls.append(order_id)
            return {"orderID": order_id, "status": "matched"}

    monkeypatch.setattr(live_module, "ClobClient", FakeClobClient)
    monkeypatch.setattr(live_module, "MarketOrderArgs", RecordedMarketOrder)
    monkeypatch.setattr(live_module, "PartialCreateOrderOptions", RecordedPartialOptions)

    client = PolymarketLiveClient(make_config())
    first_response = client.post_order(make_request())
    second_response = client.get_order("order-123")

    assert first_response == {"success": True, "orderID": "order-123", "status": "live"}
    assert second_response == {"orderID": "order-123", "status": "matched"}
    assert len(instances) == 2
    root_client, authed_client = instances
    assert root_client.key == "0x1111111111111111111111111111111111111111111111111111111111111111"
    assert root_client.creds is None
    assert root_client.create_or_derive_api_key_calls == 1
    assert authed_client.key == "0x1111111111111111111111111111111111111111111111111111111111111111"
    assert authed_client.set_api_creds_calls == [
        live_module.ApiCreds(
            api_key="api-key",
            api_secret="api-secret",
            api_passphrase="passphrase",
        )
    ]
    assert authed_client.get_order_calls == ["order-123"]
    assert len(authed_client.market_orders) == 1
    posted = authed_client.market_orders[0]
    assert posted["order_args"] == RecordedMarketOrder(
        token_id="token-down",
        amount=12.5,
        side=live_module.Side.BUY,
        order_type=live_module.OrderType.FOK,
        price=0.49,
    )
    assert posted["options"] == RecordedPartialOptions(tick_size="0.01", neg_risk=True)
    assert posted["order_type"] == live_module.OrderType.FOK


def test_live_executor_normalizes_live_client_failure_as_skip():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def post_order(self, request):
            return {"error": "not enough balance"}

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    executor = LivePolymarketExecutor(client=StubLiveClient())

    result = executor.execute(make_request())

    assert result.action == "skip"
    assert result.status == "error"
    assert result.order_id is None
    assert result.message == "not enough balance"


def test_live_executor_normalizes_live_client_response():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def __init__(self) -> None:
            self.posted = []
            self.queried = []

        def post_order(self, request):
            self.posted.append(request)
            return {
                "success": True,
                "orderID": "order-123",
                "clientOrderId": "client-123",
                "status": "accepted",
                "errorMsg": None,
            }

        def get_order(self, order_id: str):
            self.queried.append(order_id)
            return {"orderID": order_id, "status": "matched"}

    client = StubLiveClient()
    executor = LivePolymarketExecutor(client=client)
    request = make_request()

    result = executor.execute(request)
    order = executor.get_order("order-123")

    assert client.posted == [request]
    assert client.queried == ["order-123"]
    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert result.client_order_id == "client-123"
    assert result.submitted_price == 0.49
    assert result.submitted_size == 12.5
    assert result.message is None
    assert order == {"orderID": "order-123", "status": "matched"}
