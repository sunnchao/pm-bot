from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from py_clob_client_v2 import (
    ApiCreds,
    ClobClient,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)
from py_clob_client_v2.client import POST_ORDER, order_to_json_v1, order_to_json_v2
from py_clob_client_v2.config import get_contract_config
from py_clob_client_v2.exceptions import PolyApiException
from py_clob_client_v2.order_utils import ExchangeOrderBuilderV1, ExchangeOrderBuilderV2

from pm_bot.config import AppConfig
from pm_bot.execution import ExecutionRequest
from pm_bot.retry import READ_ONLY_API_RETRY_POLICY, is_retryable_status_code, retry_with_backoff


@dataclass(frozen=True, slots=True)
class PreparedMarketOrder:
    signed_order: Any
    signed_order_payload: dict[str, Any]
    signed_order_fingerprint: str
    order_hash: str


class PolymarketLiveClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._wallet_private_key = _require_text(config.wallet_private_key)
        self._funder_address = _require_text(config.funder_address)
        if config.signature_type is None:
            raise ValueError("live wallet config incomplete")
        self._signature_type = config.signature_type
        self._client = ClobClient(
            host=config.polymarket_host,
            chain_id=config.polygon_chain_id,
            key=self._wallet_private_key,
            signature_type=self._signature_type,
            funder=self._funder_address,
        )
        self._api_creds: ApiCreds | None = None
        self._authenticated_client: ClobClient | None = None

    def prepare_market_order(self, request: ExecutionRequest) -> PreparedMarketOrder:
        order_args, options = _build_market_order(request)
        authenticated_client = self._get_authenticated_client()
        signed_order = authenticated_client.create_market_order(order_args, options=options)
        signed_order_payload = _build_signed_order_payload(
            authenticated_client,
            signed_order,
            order_type=OrderType.FOK,
        )
        order_hash = _build_signed_order_hash(
            authenticated_client,
            signed_order,
            neg_risk=_resolve_order_neg_risk(authenticated_client, order_args.token_id, options),
        )
        return PreparedMarketOrder(
            signed_order=signed_order,
            signed_order_payload=signed_order_payload,
            signed_order_fingerprint=_fingerprint_payload(signed_order_payload),
            order_hash=order_hash,
        )

    def post_prepared_order(self, prepared_order: PreparedMarketOrder) -> Any:
        return self._get_authenticated_client().post_order(
            prepared_order.signed_order,
            order_type=OrderType.FOK,
        )

    def post_order(self, request: ExecutionRequest) -> Any:
        return self.post_prepared_order(self.prepare_market_order(request))

    def get_order(self, order_id: str) -> Any:
        authenticated_client = self._get_authenticated_client()
        return retry_with_backoff(
            lambda: authenticated_client.get_order(order_id),
            should_retry=_is_retryable_get_order_error,
            policy=READ_ONLY_API_RETRY_POLICY,
        )

    def get_order_by_hash(self, order_hash: str) -> Any:
        return self.get_order(order_hash)

    def replay_signed_order_payload(self, signed_order_payload: dict[str, Any]) -> Any:
        authenticated_client = self._get_authenticated_client()
        serialized = json.dumps(
            signed_order_payload,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        headers = authenticated_client._l2_headers(
            "POST",
            POST_ORDER,
            body=signed_order_payload,
            serialized_body=serialized,
        )
        return authenticated_client._post(
            f"{authenticated_client.host}{POST_ORDER}",
            headers=headers,
            data=serialized,
        )

    def _get_authenticated_client(self) -> ClobClient:
        if self._authenticated_client is None:
            creds = self._get_api_creds()
            client = ClobClient(
                host=self.config.polymarket_host,
                chain_id=self.config.polygon_chain_id,
                key=self._wallet_private_key,
                signature_type=self._signature_type,
                funder=self._funder_address,
            )
            client.set_api_creds(creds)
            self._authenticated_client = client
        return self._authenticated_client

    def _get_api_creds(self) -> ApiCreds:
        if self._api_creds is None:
            self._api_creds = self._client.create_or_derive_api_key()
        return self._api_creds


def _is_retryable_get_order_error(exc: Exception) -> bool:
    if isinstance(exc, PolyApiException):
        return exc.status_code is None or is_retryable_status_code(exc.status_code)
    return isinstance(exc, OSError)


def _build_market_order(
    request: ExecutionRequest,
) -> tuple[MarketOrderArgs, PartialCreateOrderOptions | None]:
    if request.order_type.lower() != "market":
        raise ValueError("only market orders are supported")
    token_id = _require_text(request.token_id, message="live execution requires token_id")
    if request.side not in {"UP", "DOWN"}:
        raise ValueError("live execution requires UP or DOWN side")

    options = _build_options(request.metadata)
    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=request.size_usd,
        side=Side.BUY,
        order_type=OrderType.FOK,
        price=request.price,
    )
    return order_args, options


def _build_signed_order_payload(
    authenticated_client: ClobClient,
    signed_order: Any,
    *,
    order_type: OrderType,
) -> dict[str, Any]:
    owner = _api_key_for_payload(authenticated_client)
    serializer = order_to_json_v2 if _is_v2_signed_order(signed_order) else order_to_json_v1
    return serializer(signed_order, owner, order_type, False, False)


def _build_signed_order_hash(
    authenticated_client: ClobClient,
    signed_order: Any,
    *,
    neg_risk: bool,
) -> str:
    builder = _order_hash_builder(authenticated_client, signed_order, neg_risk=neg_risk)
    typed_data = builder.build_order_typed_data(signed_order)
    return builder.build_order_hash(typed_data)


def _is_v2_signed_order(signed_order: Any) -> bool:
    return hasattr(signed_order, "timestamp")


def _resolve_order_neg_risk(
    authenticated_client: ClobClient,
    token_id: str,
    options: PartialCreateOrderOptions | None,
) -> bool:
    configured_neg_risk = None if options is None else options.neg_risk
    if configured_neg_risk is not None:
        return bool(configured_neg_risk)
    return bool(authenticated_client.get_neg_risk(token_id))


def _order_hash_builder(
    authenticated_client: ClobClient,
    signed_order: Any,
    *,
    neg_risk: bool,
):
    contract_config = get_contract_config(authenticated_client.chain_id)
    signer = getattr(getattr(authenticated_client, "builder", None), "signer", None)
    if _is_v2_signed_order(signed_order):
        contract_address = contract_config.neg_risk_exchange_v2 if neg_risk else contract_config.exchange_v2
        return ExchangeOrderBuilderV2(contract_address, authenticated_client.chain_id, signer)
    contract_address = contract_config.neg_risk_exchange if neg_risk else contract_config.exchange
    return ExchangeOrderBuilderV1(contract_address, authenticated_client.chain_id, signer)


def _api_key_for_payload(client: ClobClient) -> str:
    creds = getattr(client, "creds", None)
    api_key = getattr(creds, "api_key", None)
    return api_key or ""


_DUPLICATE_ORDER_HASH_RE = re.compile(r"order\s+(0x[a-fA-F0-9]+)\s+is invalid\.\s+Duplicated\.", re.IGNORECASE)


def parse_duplicate_order_hash(message: str | None) -> str | None:
    if message is None:
        return None
    match = _DUPLICATE_ORDER_HASH_RE.search(message)
    if match is None:
        return None
    return match.group(1)


def _fingerprint_payload(payload: dict[str, Any]) -> str:
    canonical_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _require_text(value: str | None, *, message: str = "live wallet config incomplete") -> str:
    if value is None or not value.strip():
        raise ValueError(message)
    return value.strip()


def _build_options(metadata: dict[str, Any]) -> PartialCreateOrderOptions | None:
    tick_size = metadata.get("tick_size")
    neg_risk = metadata.get("neg_risk")
    if tick_size is None and neg_risk is None:
        return None
    normalized_tick_size = None if tick_size is None else str(tick_size)
    return PartialCreateOrderOptions(tick_size=normalized_tick_size, neg_risk=neg_risk)
