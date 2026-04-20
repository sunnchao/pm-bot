from __future__ import annotations

import logging
from dataclasses import dataclass, field

LOGGER_NAME = "pm_bot.cycle"
_logger = logging.getLogger(LOGGER_NAME)

_LIVE_SUCCESS_EXECUTION_STATUSES = frozenset(
    {
        "accepted",
        "delayed",
        "live",
        "matched",
        "open",
        "partial_fill",
        "partially-filled",
        "partially_filled",
        "pending_reconcile",
        "submitted",
        "unmatched",
    }
)


@dataclass(slots=True)
class OneShotCycleMetrics:
    interval: str
    market_id: str | None = None
    signal_name: str | None = None
    confidence: float = 0.0
    side: str | None = None
    stake: float = 0.0
    reasons: list[str] = field(default_factory=list)


def emit_cycle_result(
    metrics: OneShotCycleMetrics,
    *,
    action: str,
    duration_ms: float,
    execution_status: str | None = None,
    execution_message: str | None = None,
    order_id: str | None = None,
    submission_id: str | None = None,
) -> None:
    outcome = _classify_outcome(action=action, execution_status=execution_status)
    _logger.info(
        "trading_cycle",
        extra=_payload(
            metrics,
            outcome=outcome,
            action=action,
            duration_ms=duration_ms,
            execution_status=execution_status,
            execution_message=execution_message,
            order_id=order_id,
            submission_id=submission_id,
        ),
    )


def emit_cycle_error(metrics: OneShotCycleMetrics, *, error: Exception, duration_ms: float) -> None:
    _logger.exception(
        "trading_cycle",
        extra=_payload(
            metrics,
            outcome="error",
            action="error",
            duration_ms=duration_ms,
            error_type=type(error).__name__,
            error_message=str(error),
        ),
    )


def _classify_outcome(*, action: str, execution_status: str | None) -> str:
    normalized_execution_status = _normalize_execution_status(execution_status)
    if action == "skip":
        return "error" if normalized_execution_status == "error" else "skip"
    if action == "error":
        return "error"
    if action == "live_trade" and normalized_execution_status not in _LIVE_SUCCESS_EXECUTION_STATUSES:
        return "error"
    return "success"


def _normalize_execution_status(execution_status: str | None) -> str | None:
    if not isinstance(execution_status, str):
        return None
    normalized = execution_status.strip().lower()
    return normalized or None


def _payload(
    metrics: OneShotCycleMetrics,
    *,
    outcome: str,
    action: str,
    duration_ms: float,
    execution_status: str | None = None,
    execution_message: str | None = None,
    order_id: str | None = None,
    submission_id: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> dict[str, object]:
    return {
        "event": "trading_cycle",
        "interval": metrics.interval,
        "outcome": outcome,
        "action": action,
        "market_id": metrics.market_id,
        "signal_name": metrics.signal_name,
        "confidence": metrics.confidence,
        "side": metrics.side,
        "stake": metrics.stake,
        "reasons": list(metrics.reasons),
        "duration_ms": duration_ms,
        "execution_status": execution_status,
        "execution_message": execution_message,
        "order_id": order_id,
        "submission_id": submission_id,
        "error_type": error_type,
        "error_message": error_message,
    }
