from __future__ import annotations

import json

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pm_bot.config import AppConfig
from pm_bot.live_recorder import LiveOrderRecorder
from pm_bot.models import LiveOrderRecord, PaperTradeRecord, SignalDecision
from pm_bot.recorder import PaperTradeRecorder


def _new_submission_id() -> str:
    return uuid4().hex


@dataclass(slots=True)
class ExecutionRequest:
    market_id: str
    token_id: str | None
    side: str
    price: float
    size_usd: float
    order_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    submission_id: str = field(default_factory=_new_submission_id)
    client_order_id: str | None = None


@dataclass(slots=True)
class ExecutionResult:
    action: str
    status: str
    order_id: str | None
    submission_id: str | None
    submitted_price: float
    submitted_size: float
    message: str | None = None
    client_order_id: str | None = None


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
            submission_id=None,
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
    def __init__(
        self,
        config: AppConfig | None = None,
        client=None,
        recorder: LiveOrderRecorder | None = None,
    ) -> None:
        if client is None:
            from pm_bot.polymarket_live_client import PolymarketLiveClient

            client = PolymarketLiveClient(config or AppConfig.from_env())
        self.client = client
        self.recorder = recorder

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        submission_id = self._stable_submission_id(request)
        prepared_order = self.prepare_order(request)
        if self.recorder is not None:
            self._record_submission(request, submission_id, prepared_order)

        try:
            response = self._submit_prepared_order_with_version_retry(request, submission_id, prepared_order)
            result = self._normalize_response(response, request)
        except Exception as exc:
            if self.recorder is not None:
                try:
                    self.recorder.update_status(
                        submission_id,
                        status=_submission_failure_status(exc),
                        message=_exception_message(exc),
                    )
                except Exception as journal_exc:
                    _add_exception_note(exc, _append_status_journaling_warning(None, journal_exc))
            raise

        if self.recorder is not None:
            try:
                self.recorder.update_status(
                    submission_id,
                    status=result.status,
                    order_id=result.order_id,
                    message=result.message,
                )
            except Exception as exc:
                if not _is_trackable_result(result):
                    raise
                result = replace(
                    result,
                    message=_append_status_journaling_warning(result.message, exc),
                )
        return result

    def _record_submission(self, request: ExecutionRequest, submission_id: str, prepared_order: Any = None) -> None:
        submitted_order = self._submitted_order_record(request, submission_id, prepared_order)
        record_once = getattr(self.recorder, "record_once", None)
        if callable(record_once):
            _, created = record_once(submitted_order)
            if not created:
                raise RuntimeError(_duplicate_submission_id_message(submission_id))
            return

        get_by_submission_id = getattr(self.recorder, "get_by_submission_id", None)
        if callable(get_by_submission_id) and get_by_submission_id(submission_id) is not None:
            raise RuntimeError(_duplicate_submission_id_message(submission_id))

        self.recorder.record(submitted_order)

    def prepare_order(self, request: ExecutionRequest) -> Any:
        prepare_market_order = getattr(self.client, "prepare_market_order", None)
        if callable(prepare_market_order):
            return prepare_market_order(request)
        return None

    def post_prepared_order(self, prepared_order: Any, request: ExecutionRequest | None = None) -> Any:
        post_prepared_order = getattr(self.client, "post_prepared_order", None)
        if callable(post_prepared_order):
            return post_prepared_order(prepared_order)
        if request is None:
            raise ValueError("request required when live client does not support prepared orders")
        return self.client.post_order(request)

    def post_order(self, request: ExecutionRequest) -> Any:
        return self.client.post_order(request)

    def get_order(self, order_id: str) -> Any:
        return self.client.get_order(order_id)

    def _submit_prepared_order_with_version_retry(
        self,
        request: ExecutionRequest,
        submission_id: str,
        prepared_order: Any,
    ) -> Any:
        response = self._post_prepared_order_with_ambiguity_tracking(prepared_order, request)
        if prepared_order is None or not self.is_order_version_mismatch(response):
            return response

        refreshed_prepared_order = self.prepare_order(request)
        if refreshed_prepared_order is None:
            raise RuntimeError("live client could not re-prepare order after version mismatch")
        if self.recorder is not None:
            self._refresh_submission_prepared_order(submission_id, refreshed_prepared_order)
        return self._post_prepared_order_with_ambiguity_tracking(refreshed_prepared_order, request)

    def is_order_version_mismatch(self, response: Any) -> bool:
        is_order_version_mismatch = getattr(self.client, "is_order_version_mismatch", None)
        if callable(is_order_version_mismatch):
            return bool(is_order_version_mismatch(response))
        return _is_order_version_mismatch_response(response)

    def _post_prepared_order_with_ambiguity_tracking(
        self,
        prepared_order: Any,
        request: ExecutionRequest,
    ) -> Any:
        try:
            return self.post_prepared_order(prepared_order, request)
        except Exception as exc:
            if _can_reconcile_ambiguous_submission(prepared_order) and _is_ambiguous_post_exception(exc):
                _mark_pending_reconcile_exception(exc)
            raise

    def _refresh_submission_prepared_order(self, submission_id: str, prepared_order: Any) -> None:
        updated_record = self.recorder.update_status(
            submission_id,
            status="submitted",
            order_hash=_prepared_order_hash(prepared_order),
            signed_order_payload=_prepared_order_payload(prepared_order),
            signed_order_fingerprint=_prepared_order_fingerprint(prepared_order),
        )
        if updated_record is None:
            raise RuntimeError(_missing_submission_id_message(submission_id))

    def _normalize_response(self, response: Any, request: ExecutionRequest) -> ExecutionResult:
        payload = response if isinstance(response, dict) else {}
        order_id = _payload_text(payload, "orderID", "orderId", "id")
        client_order_id = _payload_text(payload, "clientOrderId", "client_order_id", "clientOrderID")
        if client_order_id is None:
            client_order_id = _normalize_optional_text(request.client_order_id)
        raw_message = _payload_value(payload, "errorMsg", "message", "error")
        message = _normalize_optional_text(raw_message)
        success = payload.get("success")
        has_error_message = message is not None or (raw_message is not None and not isinstance(raw_message, str))
        has_error = has_error_message or success is False
        is_trackable = order_id is not None
        # Fail-closed: without order_id we cannot track or reconcile the order,
        # regardless of whether the payload claims success.
        action = "live_trade" if is_trackable else "skip"
        status = "error" if has_error or not is_trackable else _normalize_status(payload.get("status"))
        return ExecutionResult(
            action=action,
            status=status,
            order_id=order_id,
            submission_id=_normalize_optional_text(request.submission_id),
            submitted_price=request.price,
            submitted_size=request.size_usd,
            message=message,
            client_order_id=client_order_id,
        )

    def _stable_submission_id(self, request: ExecutionRequest) -> str:
        submission_id = _normalize_optional_text(request.submission_id)
        if submission_id is None:
            submission_id = _new_submission_id()
        request.submission_id = submission_id
        return submission_id

    def _submitted_order_record(
        self,
        request: ExecutionRequest,
        submission_id: str,
        prepared_order: Any = None,
    ) -> LiveOrderRecord:
        return LiveOrderRecord(
            timestamp=_request_timestamp(request),
            market_id=request.market_id,
            token_id=_normalize_optional_text(request.token_id),
            side=request.side,
            submitted_price=request.price,
            submitted_size=request.size_usd,
            status="submitted",
            submission_id=submission_id,
            order_hash=_prepared_order_hash(prepared_order),
            signed_order_payload=_prepared_order_payload(prepared_order),
            signed_order_fingerprint=_prepared_order_fingerprint(prepared_order),
        )


def _payload_text(payload: dict[str, Any], *keys: str) -> str | None:
    return _normalize_optional_text(_payload_value(payload, *keys))


def _prepared_order_payload(prepared_order: Any) -> dict[str, Any] | None:
    payload = getattr(prepared_order, "signed_order_payload", None)
    if not isinstance(payload, dict):
        return None
    return payload


def _prepared_order_fingerprint(prepared_order: Any) -> str | None:
    return _normalize_optional_text(getattr(prepared_order, "signed_order_fingerprint", None))


def _prepared_order_hash(prepared_order: Any) -> str | None:
    return _normalize_optional_text(getattr(prepared_order, "order_hash", None))


def _normalize_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_status(value: Any) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or "submitted"
    if value is None:
        return "submitted"
    return str(value)


def _payload_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _is_trackable_result(result: ExecutionResult) -> bool:
    return result.action == "live_trade" and result.order_id is not None


def _submission_failure_status(exc: Exception) -> str:
    return "pending_reconcile" if _is_pending_reconcile_exception(exc) else "error"


def _can_reconcile_ambiguous_submission(prepared_order: Any) -> bool:
    return _prepared_order_payload(prepared_order) is not None


def _mark_pending_reconcile_exception(exc: Exception) -> None:
    setattr(exc, "_pm_bot_pending_reconcile", True)


def _is_ambiguous_post_exception(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, OSError)):
        return True
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and status_code >= 500


def _is_pending_reconcile_exception(exc: Exception) -> bool:
    return bool(getattr(exc, "_pm_bot_pending_reconcile", False))


def _append_status_journaling_warning(message: str | None, exc: Exception) -> str:
    warning = f"journal update failed: {_exception_message(exc)}"
    if message is None:
        return warning
    return f"{message}; {warning}"


def _duplicate_submission_id_message(submission_id: str) -> str:
    return f"submission_id '{submission_id}' already exists in live order journal"


def _missing_submission_id_message(submission_id: str) -> str:
    return f"submission_id '{submission_id}' missing from live order journal"


def _is_order_version_mismatch_response(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    error = response.get("error")
    if not error:
        return False
    message = error if isinstance(error, str) else json.dumps(error, separators=(",", ":"), ensure_ascii=False)
    return "order_version_mismatch" in message


def _add_exception_note(exc: Exception, note: str) -> None:
    add_note = getattr(exc, "add_note", None)
    if callable(add_note):
        add_note(note)


def _request_timestamp(request: ExecutionRequest) -> str:
    timestamp = _normalize_optional_text(request.metadata.get("timestamp"))
    if timestamp is not None:
        return timestamp
    return datetime.now(timezone.utc).isoformat()


def _exception_message(exc: Exception) -> str:
    return _normalize_optional_text(str(exc)) or exc.__class__.__name__
