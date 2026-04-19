from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pm_bot.config import AppConfig
from pm_bot.models import PaperTradeRecord, SignalDecision
from pm_bot.recorder import PaperTradeRecorder


@dataclass(slots=True)
class ExecutionRequest:
    market_id: str
    token_id: str | None
    side: str
    price: float
    size_usd: float
    order_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionResult:
    action: str
    status: str
    order_id: str | None
    client_order_id: str | None
    submitted_price: float
    submitted_size: float
    message: str | None = None


class PaperExecutor:
    def __init__(self, recorder: PaperTradeRecorder) -> None:
        self.recorder = recorder

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        signal = _signal_from_metadata(request.metadata.get("signal"))
        if signal is None:
            raise ValueError("paper execution requires signal metadata")

        self.recorder.record(
            PaperTradeRecord(
                timestamp=str(request.metadata["timestamp"]),
                market_id=request.market_id,
                interval=str(request.metadata["interval"]),
                side=request.side,
                price=request.price,
                stake=request.size_usd,
                expires_at=request.metadata.get("expires_at"),
                reference_price=request.metadata.get("reference_price"),
                signal=signal,
                notes=list(request.metadata.get("notes", [])),
            )
        )
        return ExecutionResult(
            action="paper_trade",
            status="recorded",
            order_id=None,
            client_order_id=None,
            submitted_price=request.price,
            submitted_size=request.size_usd,
            message="paper trade recorded",
        )


def _signal_from_metadata(payload: Any) -> SignalDecision | None:
    if isinstance(payload, SignalDecision):
        return payload
    if not isinstance(payload, dict):
        return None

    reasons = payload.get("reasons", [])
    if not isinstance(reasons, list):
        return None

    side = payload.get("side")
    signal_name = payload.get("signal_name")
    return SignalDecision(
        should_trade=bool(payload.get("should_trade")),
        side=side if isinstance(side, str) else None,
        signal_name=signal_name if isinstance(signal_name, str) else None,
        confidence=float(payload.get("confidence", 0.0)),
        reasons=[str(reason) for reason in reasons],
    )


class LivePolymarketExecutor:
    def __init__(self, config: AppConfig | None = None, client=None) -> None:
        if client is None:
            from pm_bot.polymarket_live_client import PolymarketLiveClient

            client = PolymarketLiveClient(config or AppConfig.from_env())
        self.client = client

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return self._normalize_response(self.post_order(request), request)

    def post_order(self, request: ExecutionRequest) -> Any:
        return self.client.post_order(request)

    def get_order(self, order_id: str) -> Any:
        return self.client.get_order(order_id)

    def _normalize_response(self, response: Any, request: ExecutionRequest) -> ExecutionResult:
        payload = response if isinstance(response, dict) else {}
        order_id = _payload_value(payload, "orderID", "orderId", "id")
        client_order_id = _payload_value(payload, "clientOrderId", "client_order_id", "clientOrderID")
        message = _payload_value(payload, "errorMsg", "message", "error")
        success = payload.get("success")
        # Fail-closed: without order_id we cannot track or reconcile the order,
        # regardless of whether the payload claims success.
        has_error = (
            message is not None
            or success is False
            or order_id is None
            or not isinstance(order_id, str)
            or not order_id.strip()
        )
        action = "skip" if has_error else "live_trade"
        status = "error" if has_error else str(payload.get("status", "submitted"))
        return ExecutionResult(
            action=action,
            status=status,
            order_id=order_id,
            client_order_id=client_order_id,
            submitted_price=request.price,
            submitted_size=request.size_usd,
            message=message,
        )


def _payload_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None
