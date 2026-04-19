from __future__ import annotations

from typing import Any

from py_clob_client_v2 import (
    ApiCreds,
    ClobClient,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

from pm_bot.config import AppConfig
from pm_bot.execution import ExecutionRequest


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

    def post_order(self, request: ExecutionRequest) -> Any:
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
        return self._get_authenticated_client().create_and_post_market_order(
            order_args,
            options=options,
            order_type=OrderType.FOK,
        )

    def get_order(self, order_id: str) -> Any:
        return self._get_authenticated_client().get_order(order_id)

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
