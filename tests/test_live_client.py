from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from py_clob_client_v2.exceptions import PolyApiException

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


def test_execution_request_auto_populates_submission_id():
    first_request = make_request()
    second_request = make_request()

    assert isinstance(first_request.submission_id, str)
    assert first_request.submission_id
    assert first_request.submission_id != second_request.submission_id


def test_execution_request_allows_explicit_submission_id_override():
    request = make_request(submission_id="submission-override")

    assert request.submission_id == "submission-override"


@dataclass
class RecordedMarketOrder:
    token_id: str
    amount: float
    side: object
    order_type: object
    price: float = 0.0
    timestamp: str | None = None


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




def test_live_client_retries_transient_get_order_errors(monkeypatch):
    from pm_bot import polymarket_live_client as live_module
    from pm_bot.polymarket_live_client import PolymarketLiveClient

    sleep_calls: list[float] = []
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
            self.get_order_calls = []
            instances.append(self)

        def create_or_derive_api_key(self):
            return live_module.ApiCreds(
                api_key="api-key",
                api_secret="api-secret",
                api_passphrase="passphrase",
            )

        def set_api_creds(self, creds):
            self.creds = creds

        def get_order(self, order_id):
            self.get_order_calls.append(order_id)
            if len(self.get_order_calls) < 3:
                raise PolyApiException(error_msg="Request exception!")
            return {"orderID": order_id, "status": "matched"}

    monkeypatch.setattr("pm_bot.retry.time.sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(live_module, "ClobClient", FakeClobClient)

    client = PolymarketLiveClient(make_config())

    response = client.get_order("order-123")

    assert response == {"orderID": "order-123", "status": "matched"}
    assert len(instances) == 2
    assert instances[1].get_order_calls == ["order-123", "order-123", "order-123"]
    assert sleep_calls == [0.1, 0.2]


def test_live_client_does_not_retry_post_prepared_order(monkeypatch):
    from pm_bot import polymarket_live_client as live_module
    from pm_bot.polymarket_live_client import PolymarketLiveClient, PreparedMarketOrder

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
            self.post_order_calls = []
            instances.append(self)

        def create_or_derive_api_key(self):
            return live_module.ApiCreds(
                api_key="api-key",
                api_secret="api-secret",
                api_passphrase="passphrase",
            )

        def set_api_creds(self, creds):
            self.creds = creds

        def post_order(self, order, order_type=None, post_only=False, defer_exec=False):
            self.post_order_calls.append(
                {
                    "signed_order": order,
                    "order_type": order_type,
                    "post_only": post_only,
                    "defer_exec": defer_exec,
                }
            )
            raise PolyApiException(error_msg="Request exception!")

    monkeypatch.setattr(live_module, "ClobClient", FakeClobClient)

    client = PolymarketLiveClient(make_config())
    prepared = PreparedMarketOrder(
        signed_order=object(),
        signed_order_payload={"tokenId": "token-down"},
        signed_order_fingerprint="fingerprint",
        order_hash="0xorder-hash-123",
    )

    with pytest.raises(PolyApiException, match="Request exception"):
        client.post_prepared_order(prepared)

    assert len(instances) == 2
    assert len(instances[1].post_order_calls) == 1

def test_live_client_prepares_market_order_payload_order_hash_and_posts_prepared_order(monkeypatch):
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

        def create_market_order(self, order_args, options=None):
            self.market_orders.append(
                {
                    "order_args": order_args,
                    "options": options,
                }
            )
            return RecordedMarketOrder(
                token_id=order_args.token_id,
                amount=order_args.amount,
                side=order_args.side,
                order_type=order_args.order_type,
                price=order_args.price,
                timestamp="1710000000",
            )

        def post_order(self, order, order_type=None, post_only=False, defer_exec=False):
            self.market_orders.append(
                {
                    "signed_order": order,
                    "order_type": order_type,
                    "post_only": post_only,
                    "defer_exec": defer_exec,
                }
            )
            return {"success": True, "orderID": "order-123", "status": "live"}

        def get_order(self, order_id):
            self.get_order_calls.append(order_id)
            return {"orderID": order_id, "status": "matched"}

    monkeypatch.setattr(live_module, "ClobClient", FakeClobClient)
    monkeypatch.setattr(live_module, "MarketOrderArgs", RecordedMarketOrder)
    monkeypatch.setattr(live_module, "PartialCreateOrderOptions", RecordedPartialOptions)
    monkeypatch.setattr(
        live_module,
        "_build_signed_order_hash",
        lambda authenticated_client, signed_order, *, neg_risk: "0xorder-hash-123",
        raising=False,
    )
    monkeypatch.setattr(
        live_module,
        "order_to_json_v2",
        lambda order, owner, order_type, post_only=False, defer_exec=False: {
            "order": {
                "tokenId": order.token_id,
                "amount": order.amount,
                "price": order.price,
                "signature": "signature-123",
            },
            "owner": owner,
            "orderType": order_type,
            "postOnly": post_only,
            "deferExec": defer_exec,
        },
    )

    client = PolymarketLiveClient(make_config())
    prepared = client.prepare_market_order(make_request())
    first_response = client.post_prepared_order(prepared)
    second_response = client.get_order("order-123")
    expected_payload = {
        "order": {
            "tokenId": "token-down",
            "amount": 12.5,
            "price": 0.49,
            "signature": "signature-123",
        },
        "owner": "api-key",
        "orderType": live_module.OrderType.FOK,
        "postOnly": False,
        "deferExec": False,
    }
    expected_fingerprint = hashlib.sha256(
        json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

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
    assert prepared.signed_order == RecordedMarketOrder(
        token_id="token-down",
        amount=12.5,
        side=live_module.Side.BUY,
        order_type=live_module.OrderType.FOK,
        price=0.49,
        timestamp="1710000000",
    )
    assert prepared.signed_order_payload == expected_payload
    assert prepared.signed_order_fingerprint == expected_fingerprint
    assert prepared.order_hash == "0xorder-hash-123"
    assert len(authed_client.market_orders) == 2
    created = authed_client.market_orders[0]
    posted = authed_client.market_orders[1]
    assert created["order_args"] == RecordedMarketOrder(
        token_id="token-down",
        amount=12.5,
        side=live_module.Side.BUY,
        order_type=live_module.OrderType.FOK,
        price=0.49,
    )
    assert created["options"] == RecordedPartialOptions(tick_size="0.01", neg_risk=True)
    assert posted["signed_order"] == prepared.signed_order
    assert posted["order_type"] == live_module.OrderType.FOK
    assert posted["post_only"] is False
    assert posted["defer_exec"] is False


def test_build_signed_order_payload_dispatches_by_order_shape(monkeypatch):
    from pm_bot import polymarket_live_client as live_module

    calls = []

    def fake_v1(order, owner, order_type, post_only=False, defer_exec=False):
        calls.append(("v1", order, owner, order_type, post_only, defer_exec))
        return {"version": "v1", "owner": owner}

    def fake_v2(order, owner, order_type, post_only=False, defer_exec=False):
        calls.append(("v2", order, owner, order_type, post_only, defer_exec))
        return {"version": "v2", "owner": owner}

    monkeypatch.setattr(live_module, "order_to_json_v1", fake_v1)
    monkeypatch.setattr(live_module, "order_to_json_v2", fake_v2)

    authenticated_client = SimpleNamespace(creds=SimpleNamespace(api_key="api-key"))
    v1_order = SimpleNamespace(tokenId="token-v1")
    v2_order = SimpleNamespace(tokenId="token-v2", timestamp="1710000000")

    v1_payload = live_module._build_signed_order_payload(
        authenticated_client,
        v1_order,
        order_type=live_module.OrderType.FOK,
    )
    v2_payload = live_module._build_signed_order_payload(
        authenticated_client,
        v2_order,
        order_type=live_module.OrderType.FOK,
    )

    assert v1_payload == {"version": "v1", "owner": "api-key"}
    assert v2_payload == {"version": "v2", "owner": "api-key"}
    assert calls == [
        ("v1", v1_order, "api-key", live_module.OrderType.FOK, False, False),
        ("v2", v2_order, "api-key", live_module.OrderType.FOK, False, False),
    ]


def test_build_signed_order_hash_dispatches_by_order_shape(monkeypatch):
    from pm_bot import polymarket_live_client as live_module

    calls = []

    class FakeBuilderV1:
        def __init__(self, contract_address, chain_id, signer):
            calls.append(("v1-init", contract_address, chain_id, signer))

        def build_order_typed_data(self, order):
            calls.append(("v1-typed", order))
            return {"version": "v1", "order": order}

        def build_order_hash(self, typed_data):
            calls.append(("v1-hash", typed_data))
            return "0xhash-v1"

    class FakeBuilderV2:
        def __init__(self, contract_address, chain_id, signer):
            calls.append(("v2-init", contract_address, chain_id, signer))

        def build_order_typed_data(self, order):
            calls.append(("v2-typed", order))
            return {"version": "v2", "order": order}

        def build_order_hash(self, typed_data):
            calls.append(("v2-hash", typed_data))
            return "0xhash-v2"

    monkeypatch.setattr(
        live_module,
        "get_contract_config",
        lambda chain_id: SimpleNamespace(
            exchange="exchange-v1",
            neg_risk_exchange="exchange-v1-neg",
            exchange_v2="exchange-v2",
            neg_risk_exchange_v2="exchange-v2-neg",
        ),
        raising=False,
    )
    monkeypatch.setattr(live_module, "ExchangeOrderBuilderV1", FakeBuilderV1, raising=False)
    monkeypatch.setattr(live_module, "ExchangeOrderBuilderV2", FakeBuilderV2, raising=False)

    authenticated_client = SimpleNamespace(chain_id=137, builder=SimpleNamespace(signer="signer"))
    v1_order = SimpleNamespace(tokenId="token-v1")
    v2_order = SimpleNamespace(tokenId="token-v2", timestamp="1710000000")

    v1_hash = live_module._build_signed_order_hash(authenticated_client, v1_order, neg_risk=False)
    v2_hash = live_module._build_signed_order_hash(authenticated_client, v2_order, neg_risk=True)

    assert v1_hash == "0xhash-v1"
    assert v2_hash == "0xhash-v2"
    assert calls == [
        ("v1-init", "exchange-v1", 137, "signer"),
        ("v1-typed", v1_order),
        ("v1-hash", {"version": "v1", "order": v1_order}),
        ("v2-init", "exchange-v2-neg", 137, "signer"),
        ("v2-typed", v2_order),
        ("v2-hash", {"version": "v2", "order": v2_order}),
    ]


def test_live_executor_treats_non_empty_error_message_with_order_id_as_live_trade_error():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": True,
                "orderID": "order-123",
                "errorMsg": "not enough balance",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    executor = LivePolymarketExecutor(client=StubLiveClient())

    result = executor.execute(make_request())

    assert result.action == "live_trade"
    assert result.status == "error"
    assert result.order_id == "order-123"
    assert result.message == "not enough balance"


def test_live_executor_uses_later_error_alias_when_earlier_error_alias_is_blank():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": True,
                "orderID": "order-123",
                "errorMsg": "   ",
                "message": "real error",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    executor = LivePolymarketExecutor(client=StubLiveClient())

    result = executor.execute(make_request())

    assert result.action == "live_trade"
    assert result.status == "error"
    assert result.order_id == "order-123"
    assert result.message == "real error"


def test_live_executor_keeps_error_without_order_id_as_skip():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": False,
                "errorMsg": "not enough balance",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    executor = LivePolymarketExecutor(client=StubLiveClient())

    result = executor.execute(make_request())

    assert result.action == "skip"
    assert result.status == "error"
    assert result.order_id is None
    assert result.message == "not enough balance"


def test_live_executor_treats_empty_error_message_as_success():
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
                "errorMsg": "",
            }

        def get_order(self, order_id: str):
            self.queried.append(order_id)
            return {"orderID": order_id, "status": "matched"}

    client = StubLiveClient()
    executor = LivePolymarketExecutor(client=client)
    request = make_request(submission_id="submission-request-123")

    result = executor.execute(request)
    order = executor.get_order("order-123")

    assert client.posted == [request]
    assert client.queried == ["order-123"]
    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert result.submission_id == "submission-request-123"
    assert result.submitted_price == 0.49
    assert result.submitted_size == 12.5
    assert result.message is None
    assert order == {"orderID": "order-123", "status": "matched"}


def test_live_executor_normalizes_whitespace_padded_order_id_from_response():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": True,
                "orderID": "  order-123  ",
                "clientOrderId": "  client-123  ",
                "status": "  accepted  ",
                "errorMsg": "",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    executor = LivePolymarketExecutor(client=StubLiveClient())

    result = executor.execute(make_request(submission_id="submission-request-123"))

    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert result.submission_id == "submission-request-123"
    assert result.message is None


def test_live_executor_uses_later_order_id_alias_when_earlier_order_id_alias_is_blank():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": True,
                "orderID": "   ",
                "orderId": "order-123",
                "clientOrderId": "client-123",
                "status": "accepted",
                "errorMsg": "",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    executor = LivePolymarketExecutor(client=StubLiveClient())

    result = executor.execute(make_request(submission_id="submission-request-123"))

    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert result.submission_id == "submission-request-123"
    assert result.message is None


def test_live_executor_returns_request_submission_id_when_response_omits_client_order_id():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": True,
                "orderID": "order-123",
                "status": "accepted",
                "errorMsg": "",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    executor = LivePolymarketExecutor(client=StubLiveClient())
    request = make_request(submission_id="submission-request-123")

    result = executor.execute(request)

    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert result.submission_id == "submission-request-123"
    assert result.message is None


def test_live_executor_journals_success_with_request_submission_id(tmp_path):
    from pm_bot.execution import LivePolymarketExecutor
    from pm_bot.live_recorder import LiveOrderRecorder

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": True,
                "orderID": "order-123",
                "clientOrderId": "server-client-999",
                "status": "accepted",
                "errorMsg": "",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    executor = LivePolymarketExecutor(client=StubLiveClient(), recorder=recorder)
    request = make_request(
        submission_id="submission-request-123",
        metadata={
            "timestamp": "2026-04-19T12:34:56+00:00",
            "tick_size": 0.01,
            "neg_risk": True,
        },
    )

    result = executor.execute(request)
    journaled = recorder.get_by_submission_id("submission-request-123")

    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert result.submission_id == "submission-request-123"
    assert journaled is not None
    assert journaled.timestamp == "2026-04-19T12:34:56+00:00"
    assert journaled.market_id == "market-1"
    assert journaled.token_id == "token-down"
    assert journaled.side == "DOWN"
    assert journaled.submitted_price == 0.49
    assert journaled.submitted_size == 12.5
    assert journaled.submission_id == "submission-request-123"
    assert journaled.order_id == "order-123"
    assert journaled.status == "accepted"
    assert journaled.message is None
    assert recorder.get_by_submission_id("server-client-999") is None


def test_live_executor_persists_signed_payload_and_order_hash_before_post_and_keeps_it_after_success(
    tmp_path,
):
    from pm_bot.execution import LivePolymarketExecutor
    from pm_bot.live_recorder import LiveOrderRecorder

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")

    class StubLiveClient:
        def __init__(self) -> None:
            self.posted = []

        def prepare_market_order(self, request):
            return SimpleNamespace(
                signed_order={"prepared": True, "submission_id": request.submission_id},
                signed_order_payload={
                    "order": {
                        "tokenId": request.token_id,
                        "signature": "signature-123",
                    },
                    "owner": "api-key",
                    "orderType": "FOK",
                    "postOnly": False,
                    "deferExec": False,
                },
                signed_order_fingerprint="fingerprint-123",
                order_hash="0xorder-hash-123",
            )

        def post_prepared_order(self, prepared_order):
            journaled = recorder.get_by_submission_id("submission-request-123")

            assert journaled is not None
            assert journaled.status == "submitted"
            assert journaled.order_id is None
            assert journaled.message is None
            assert journaled.order_hash == "0xorder-hash-123"
            assert journaled.signed_order_payload == prepared_order.signed_order_payload
            assert journaled.signed_order_fingerprint == "fingerprint-123"

            self.posted.append(prepared_order)
            return {
                "success": True,
                "orderID": "order-123",
                "status": "accepted",
                "errorMsg": "",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    client = StubLiveClient()
    executor = LivePolymarketExecutor(client=client, recorder=recorder)

    result = executor.execute(make_request(submission_id="submission-request-123"))
    journaled = recorder.get_by_submission_id("submission-request-123")

    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert client.posted and client.posted[0].signed_order["submission_id"] == "submission-request-123"
    assert journaled is not None
    assert journaled.status == "accepted"
    assert journaled.order_id == "order-123"
    assert journaled.message is None
    assert journaled.order_hash == "0xorder-hash-123"
    assert journaled.signed_order_payload == {
        "order": {
            "tokenId": "token-down",
            "signature": "signature-123",
        },
        "owner": "api-key",
        "orderType": "FOK",
        "postOnly": False,
        "deferExec": False,
    }
    assert journaled.signed_order_fingerprint == "fingerprint-123"


def test_live_executor_retries_order_version_mismatch_with_reprepared_payload_order_hash_and_same_submission_id(
    tmp_path,
):
    from pm_bot.execution import LivePolymarketExecutor
    from pm_bot.live_recorder import LiveOrderRecorder

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")

    class StubLiveClient:
        def __init__(self) -> None:
            self.prepared_orders = []
            self.posted = []

        def prepare_market_order(self, request):
            attempt = len(self.prepared_orders) + 1
            prepared_order = SimpleNamespace(
                signed_order={"attempt": attempt, "submission_id": request.submission_id},
                signed_order_payload={
                    "order": {
                        "attempt": attempt,
                        "submission_id": request.submission_id,
                    }
                },
                signed_order_fingerprint=f"fingerprint-{attempt}",
                order_hash=f"0xorder-hash-{attempt}",
            )
            self.prepared_orders.append(prepared_order)
            return prepared_order

        def post_prepared_order(self, prepared_order):
            journaled = recorder.get_by_submission_id("submission-request-123")

            assert journaled is not None
            assert journaled.order_hash == prepared_order.order_hash
            assert journaled.signed_order_payload == prepared_order.signed_order_payload
            assert journaled.signed_order_fingerprint == prepared_order.signed_order_fingerprint

            self.posted.append(prepared_order)
            if len(self.posted) == 1:
                return {"success": False, "error": "order_version_mismatch"}
            return {
                "success": True,
                "orderID": "order-123",
                "status": "accepted",
                "errorMsg": "",
            }

        def is_order_version_mismatch(self, response):
            return response.get("error") == "order_version_mismatch"

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    client = StubLiveClient()
    executor = LivePolymarketExecutor(client=client, recorder=recorder)
    request = make_request(submission_id="submission-request-123")

    result = executor.execute(request)
    journaled = recorder.get_by_submission_id("submission-request-123")

    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert result.submission_id == "submission-request-123"
    assert len(client.prepared_orders) == 2
    assert len(client.posted) == 2
    assert [prepared.signed_order["attempt"] for prepared in client.posted] == [1, 2]
    assert [prepared.signed_order["submission_id"] for prepared in client.posted] == [
        "submission-request-123",
        "submission-request-123",
    ]
    assert journaled is not None
    assert journaled.status == "accepted"
    assert journaled.order_id == "order-123"
    assert journaled.message is None
    assert journaled.order_hash == "0xorder-hash-2"
    assert journaled.signed_order_payload == {
        "order": {
            "attempt": 2,
            "submission_id": "submission-request-123",
        }
    }
    assert journaled.signed_order_fingerprint == "fingerprint-2"


def test_live_executor_rejects_duplicate_submission_id_before_posting(tmp_path):
    from pm_bot.execution import LivePolymarketExecutor
    from pm_bot.live_recorder import LiveOrderRecorder

    class StubLiveClient:
        def __init__(self) -> None:
            self.posted = []

        def post_order(self, request):
            self.posted.append(request.submission_id)
            return {
                "success": True,
                "orderID": "order-123",
                "status": "accepted",
                "errorMsg": "",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    client = StubLiveClient()
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    executor = LivePolymarketExecutor(client=client, recorder=recorder)

    executor.execute(make_request(submission_id="submission-request-123"))

    with pytest.raises(RuntimeError, match="submission_id .* already exists"):
        executor.execute(make_request(submission_id="  submission-request-123  "))

    assert client.posted == ["submission-request-123"]


def test_live_executor_returns_trackable_result_when_status_journaling_fails():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": True,
                "orderID": "order-123",
                "status": "accepted",
                "errorMsg": "",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    class FailingStatusRecorder:
        def __init__(self) -> None:
            self.recorded = []
            self.update_calls = []

        def record(self, order):
            self.recorded.append(order)
            return order

        def update_status(self, submission_id, *, status, order_id=None, message=None):
            self.update_calls.append(
                {
                    "submission_id": submission_id,
                    "status": status,
                    "order_id": order_id,
                    "message": message,
                }
            )
            raise RuntimeError("journal write failed")

    recorder = FailingStatusRecorder()
    executor = LivePolymarketExecutor(client=StubLiveClient(), recorder=recorder)

    result = executor.execute(make_request(submission_id="submission-request-123"))

    assert result.action == "live_trade"
    assert result.status == "accepted"
    assert result.order_id == "order-123"
    assert result.submission_id == "submission-request-123"
    assert result.message == "journal update failed: journal write failed"
    assert len(recorder.recorded) == 1
    assert recorder.recorded[0].submission_id == "submission-request-123"
    assert recorder.update_calls == [
        {
            "submission_id": "submission-request-123",
            "status": "accepted",
            "order_id": "order-123",
            "message": None,
        }
    ]


def test_live_executor_journals_untrackable_failures_with_request_submission_id(tmp_path):
    from pm_bot.execution import LivePolymarketExecutor
    from pm_bot.live_recorder import LiveOrderRecorder

    class StubLiveClient:
        def post_order(self, request):
            return {
                "success": False,
                "errorMsg": "not enough balance",
            }

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    executor = LivePolymarketExecutor(client=StubLiveClient(), recorder=recorder)
    request = make_request(submission_id="submission-request-123")

    result = executor.execute(request)
    journaled = recorder.get_by_submission_id("submission-request-123")

    assert result.action == "skip"
    assert result.status == "error"
    assert result.order_id is None
    assert result.submission_id == "submission-request-123"
    assert result.message == "not enough balance"
    assert journaled is not None
    assert journaled.submission_id == "submission-request-123"
    assert journaled.order_id is None
    assert journaled.status == "error"
    assert journaled.message == "not enough balance"


def test_live_executor_marks_ambiguous_prepared_submission_failures_pending_reconcile_with_order_hash(
    tmp_path,
):
    from pm_bot.execution import LivePolymarketExecutor
    from pm_bot.live_recorder import LiveOrderRecorder

    class StubLiveClient:
        def prepare_market_order(self, request):
            return SimpleNamespace(
                signed_order={"prepared": True, "submission_id": request.submission_id},
                signed_order_payload={
                    "order": {
                        "tokenId": request.token_id,
                        "signature": "signature-123",
                    },
                    "owner": "api-key",
                    "orderType": "FOK",
                    "postOnly": False,
                    "deferExec": False,
                },
                signed_order_fingerprint="fingerprint-123",
                order_hash="0xorder-hash-123",
            )

        def post_prepared_order(self, prepared_order):
            raise TimeoutError("gateway timeout")

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    executor = LivePolymarketExecutor(client=StubLiveClient(), recorder=recorder)

    with pytest.raises(TimeoutError, match="gateway timeout"):
        executor.execute(make_request(submission_id="submission-request-123"))

    journaled = recorder.get_by_submission_id("submission-request-123")

    assert journaled is not None
    assert journaled.submission_id == "submission-request-123"
    assert journaled.order_id is None
    assert journaled.status == "pending_reconcile"
    assert journaled.message == "gateway timeout"
    assert journaled.order_hash == "0xorder-hash-123"
    assert journaled.signed_order_payload == {
        "order": {
            "tokenId": "token-down",
            "signature": "signature-123",
        },
        "owner": "api-key",
        "orderType": "FOK",
        "postOnly": False,
        "deferExec": False,
    }
    assert journaled.signed_order_fingerprint == "fingerprint-123"



def test_live_executor_keeps_definite_prepared_submission_rejections_as_error_with_order_hash(
    tmp_path,
):
    from pm_bot.execution import LivePolymarketExecutor
    from pm_bot.live_recorder import LiveOrderRecorder

    class DefiniteRejection(RuntimeError):
        def __init__(self, message: str, status_code: int) -> None:
            super().__init__(message)
            self.status_code = status_code

    class StubLiveClient:
        def prepare_market_order(self, request):
            return SimpleNamespace(
                signed_order={"prepared": True, "submission_id": request.submission_id},
                signed_order_payload={
                    "order": {
                        "tokenId": request.token_id,
                        "signature": "signature-123",
                    },
                    "owner": "api-key",
                    "orderType": "FOK",
                    "postOnly": False,
                    "deferExec": False,
                },
                signed_order_fingerprint="fingerprint-123",
                order_hash="0xorder-hash-123",
            )

        def post_prepared_order(self, prepared_order):
            raise DefiniteRejection("invalid order", 400)

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    executor = LivePolymarketExecutor(client=StubLiveClient(), recorder=recorder)

    with pytest.raises(DefiniteRejection, match="invalid order"):
        executor.execute(make_request(submission_id="submission-request-123"))

    journaled = recorder.get_by_submission_id("submission-request-123")

    assert journaled is not None
    assert journaled.submission_id == "submission-request-123"
    assert journaled.order_id is None
    assert journaled.status == "error"
    assert journaled.message == "invalid order"
    assert journaled.order_hash == "0xorder-hash-123"



def test_live_executor_preserves_submit_exception_when_failure_journaling_fails():
    from pm_bot.execution import LivePolymarketExecutor

    class StubLiveClient:
        def prepare_market_order(self, request):
            return SimpleNamespace(
                signed_order={"prepared": True, "submission_id": request.submission_id},
                signed_order_payload={
                    "order": {
                        "tokenId": request.token_id,
                        "signature": "signature-123",
                    }
                },
                signed_order_fingerprint="fingerprint-123",
                order_hash="0xorder-hash-123",
            )

        def post_prepared_order(self, prepared_order):
            raise TimeoutError("gateway timeout")

        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "matched"}

    class FailingStatusRecorder:
        def __init__(self) -> None:
            self.recorded = []
            self.update_calls = []

        def record(self, order):
            self.recorded.append(order)
            return order

        def update_status(self, submission_id, *, status, order_id=None, message=None):
            self.update_calls.append(
                {
                    "submission_id": submission_id,
                    "status": status,
                    "order_id": order_id,
                    "message": message,
                }
            )
            raise RuntimeError("journal write failed")

    recorder = FailingStatusRecorder()
    executor = LivePolymarketExecutor(client=StubLiveClient(), recorder=recorder)

    with pytest.raises(TimeoutError, match="gateway timeout") as exc_info:
        executor.execute(make_request(submission_id="submission-request-123"))

    assert exc_info.value.args == ("gateway timeout",)
    assert len(recorder.recorded) == 1
    assert recorder.recorded[0].submission_id == "submission-request-123"
    assert recorder.recorded[0].signed_order_payload == {
        "order": {
            "tokenId": "token-down",
            "signature": "signature-123",
        }
    }
    assert recorder.recorded[0].signed_order_fingerprint == "fingerprint-123"
    assert recorder.recorded[0].order_hash == "0xorder-hash-123"
    assert recorder.update_calls == [
        {
            "submission_id": "submission-request-123",
            "status": "pending_reconcile",
            "order_id": None,
            "message": "gateway timeout",
        }
    ]



def test_parse_duplicate_order_hash_extracts_order_id():
    from pm_bot.polymarket_live_client import parse_duplicate_order_hash

    assert parse_duplicate_order_hash("order 0xabc123 is invalid. Duplicated.") == "0xabc123"
    assert parse_duplicate_order_hash("not duplicated") is None
