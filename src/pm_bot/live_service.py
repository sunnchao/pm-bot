from __future__ import annotations

from typing import Any

from pm_bot.live_recorder import LiveOrderRecorder
from pm_bot.models import LiveOrderRecord
from pm_bot.polymarket_live_client import parse_duplicate_order_hash
from pm_bot.service import OneShotResult, TradingService


class LiveTradingService(TradingService):
    def __init__(self, *args, live_client=None, live_recorder: LiveOrderRecorder | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.live_client = live_client
        self.live_recorder = live_recorder
        if self.live_recorder is None and self.config.trading_mode == "live":
            self.live_recorder = LiveOrderRecorder(self.config.live_orders_path)

    def oneshot(self, interval: str, balance: float = 1_000.0, *, live_confirmed: bool = False) -> OneShotResult:
        if self.config.trading_mode == "live":
            blocker_reasons = self.reconcile_open_orders()
            if blocker_reasons:
                return OneShotResult(
                    interval=interval,
                    market_id=None,
                    action="skip",
                    reasons=blocker_reasons,
                    execution_status="blocked_live_order",
                    execution_message=",".join(blocker_reasons),
                )
        return super().oneshot(interval=interval, balance=balance, live_confirmed=live_confirmed)

    def reconcile_open_orders(self) -> list[str]:
        recorder = self.live_recorder
        if recorder is None:
            return []

        open_orders = recorder.open_submitted_orders()
        if not open_orders:
            return []

        live_client = self._resolve_live_client()
        get_order = getattr(live_client, "get_order", None) if live_client is not None else None
        get_order_by_hash = getattr(live_client, "get_order_by_hash", None) if live_client is not None else None
        replay_signed_order_payload = getattr(live_client, "replay_signed_order_payload", None) if live_client is not None else None
        lookup_failed = False

        if any(record.order_id is not None for record in open_orders) and not callable(get_order):
            lookup_failed = True
        else:
            for record in open_orders:
                try:
                    if record.order_id is not None:
                        venue_order = get_order(record.order_id)
                    else:
                        venue_order = _recover_live_order_without_order_id(
                            record,
                            get_order=get_order,
                            get_order_by_hash=get_order_by_hash,
                            replay_signed_order_payload=replay_signed_order_payload,
                        )
                except Exception:
                    lookup_failed = True
                    continue
                if venue_order is None:
                    continue
                updated_record = recorder.update_status(
                    record.submission_id,
                    status=_venue_order_status(venue_order, fallback=record.status),
                    order_id=_venue_order_id(venue_order) or record.order_id or _normalize_optional_text(record.order_hash),
                    message=_venue_order_message(venue_order),
                )
                if updated_record is None:
                    lookup_failed = True

        remaining_open_orders = recorder.open_submitted_orders()
        blocker_reasons: list[str] = []
        if lookup_failed:
            blocker_reasons.append("live_order_reconcile_failed")
        if any(_is_pending_reconcile_without_order_id(record) for record in remaining_open_orders):
            blocker_reasons.append("live_order_pending_reconcile")
        if any(_is_missing_order_id(record) for record in remaining_open_orders):
            blocker_reasons.append("live_order_missing_order_id")
        if any(record.order_id is not None for record in remaining_open_orders):
            blocker_reasons.append("live_order_open")
        return blocker_reasons

    def _resolve_live_client(self):
        if self.live_client is not None:
            return self.live_client
        executor = self.executor
        if executor is not None and callable(getattr(executor, "get_order", None)):
            return executor
        return None


def _recover_live_order_without_order_id(
    record: LiveOrderRecord,
    *,
    get_order,
    get_order_by_hash,
    replay_signed_order_payload,
):
    order_hash = _normalize_optional_text(record.order_hash)
    if order_hash is not None and callable(get_order_by_hash):
        venue_order = get_order_by_hash(order_hash)
        if venue_order is not None:
            return venue_order

    signed_order_payload = record.signed_order_payload if isinstance(record.signed_order_payload, dict) else None
    if signed_order_payload is None or not callable(replay_signed_order_payload):
        return None

    replay_response = replay_signed_order_payload(signed_order_payload)
    replay_order_id = _venue_order_id(replay_response)
    if replay_order_id is None:
        replay_order_id = parse_duplicate_order_hash(_venue_order_message(replay_response))
    if replay_order_id is None:
        replay_order_id = order_hash
    if replay_order_id is None:
        return replay_response if isinstance(replay_response, dict) else None
    if callable(get_order):
        venue_order = get_order(replay_order_id)
        if venue_order is not None:
            if isinstance(venue_order, dict) and _venue_order_id(venue_order) is None:
                venue_order = dict(venue_order)
                venue_order["id"] = replay_order_id
            return venue_order
    if not isinstance(replay_response, dict):
        return {"id": replay_order_id}
    payload = dict(replay_response)
    payload.setdefault("id", replay_order_id)
    return payload


def _is_pending_reconcile_without_order_id(record: LiveOrderRecord) -> bool:
    return _normalized_status(record.status) == "pending_reconcile" and record.order_id is None


def _is_missing_order_id(record: LiveOrderRecord) -> bool:
    return record.order_id is None and _normalized_status(record.status) != "pending_reconcile"


def _venue_order_status(payload: Any, *, fallback: str) -> str:
    if isinstance(payload, dict):
        status = _normalize_optional_text(payload.get("status"))
        if status is not None:
            return status
    return fallback


def _venue_order_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("orderID", "orderId", "id"):
        order_id = _normalize_optional_text(payload.get(key))
        if order_id is not None:
            return order_id
    return None


def _venue_order_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("errorMsg", "message", "error"):
        message = _normalize_optional_text(payload.get(key))
        if message is not None:
            return message
    return None


def _normalized_status(value: object) -> str:
    normalized = _normalize_optional_text(value)
    return "" if normalized is None else normalized.lower()


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
