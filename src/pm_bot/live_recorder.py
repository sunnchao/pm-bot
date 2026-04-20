from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from dataclasses import replace
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

from pm_bot.models import LiveOrderRecord

_OPEN_SUBMITTED_STATUSES = frozenset(
    {
        "submitted",
        "accepted",
        "live",
        "open",
        "delayed",
        "unmatched",
        "pending_reconcile",
        "partially_filled",
        "partially-filled",
        "partial_fill",
    }
)


class LiveOrderRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, order: LiveOrderRecord) -> LiveOrderRecord:
        persisted, _ = self.record_once(order)
        return persisted

    def record_once(self, order: LiveOrderRecord) -> tuple[LiveOrderRecord, bool]:
        normalized_order = _normalize_record_keys(order)
        with self._locked_ledger():
            existing = self._get_by_submission_id_locked(normalized_order.submission_id)
            if existing is not None:
                return existing, False
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(_dump_record(normalized_order) + "\n")
            return normalized_order, True

    def update_status(
        self,
        submission_id: str,
        *,
        status: str,
        order_id: str | None = None,
        order_hash: str | None = None,
        message: str | None = None,
        signed_order_payload: dict | None = None,
        signed_order_fingerprint: str | None = None,
    ) -> LiveOrderRecord | None:
        normalized_submission_id = _require_text(submission_id, message="submission_id required")
        normalized_status = _require_text(status, message="status required")
        with self._locked_ledger():
            if not self.path.exists():
                return None

            updated_record: LiveOrderRecord | None = None
            output_lines: list[str] = []
            for raw_line in self.path.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    output_lines.append(raw_line)
                    continue

                payload = _load_payload(raw_line)
                record = _hydrate_record(payload)
                if record is None or record.submission_id != normalized_submission_id:
                    output_lines.append(raw_line)
                    continue

                updated_record = _normalize_record_keys(
                    replace(
                        record,
                        status=normalized_status,
                        order_id=record.order_id if order_id is None else _normalize_optional_text(order_id),
                        order_hash=(
                            record.order_hash if order_hash is None else _normalize_optional_text(order_hash)
                        ),
                        message=record.message if message is None else _normalize_optional_text(message),
                        signed_order_payload=(
                            record.signed_order_payload
                            if signed_order_payload is None
                            else _normalize_optional_payload(signed_order_payload)
                        ),
                        signed_order_fingerprint=(
                            record.signed_order_fingerprint
                            if signed_order_fingerprint is None
                            else _normalize_optional_text(signed_order_fingerprint)
                        ),
                    )
                )
                output_lines.append(_dump_record(updated_record))

            if updated_record is None:
                return None

            self._rewrite_lines(output_lines)
            return updated_record

    def get_by_submission_id(self, submission_id: str) -> LiveOrderRecord | None:
        normalized_submission_id = _require_text(submission_id, message="submission_id required")
        with self._locked_ledger():
            return self._get_by_submission_id_locked(normalized_submission_id)

    def open_submitted_orders(self) -> list[LiveOrderRecord]:
        with self._locked_ledger():
            return [record for record in self._records_locked() if _is_open_submitted_order(record)]

    @contextmanager
    def _locked_ledger(self):
        with self.lock_path.open("a", encoding="utf-8") as lock_handle:
            flock(lock_handle.fileno(), LOCK_EX)
            try:
                yield
            finally:
                flock(lock_handle.fileno(), LOCK_UN)

    def _get_by_submission_id_locked(self, submission_id: str) -> LiveOrderRecord | None:
        for record in self._records_locked():
            if record.submission_id == submission_id:
                return record
        return None

    def _records_locked(self) -> list[LiveOrderRecord]:
        if not self.path.exists():
            return []

        records: list[LiveOrderRecord] = []
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            payload = _load_payload(raw_line)
            record = _hydrate_record(payload)
            if record is not None:
                records.append(record)
        return records

    def _rewrite_lines(self, lines: list[str]) -> None:
        temp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
        temp_path.replace(self.path)


def _is_open_submitted_order(record: LiveOrderRecord) -> bool:
    normalized_status = record.status.strip().lower()
    return normalized_status in _OPEN_SUBMITTED_STATUSES or (
        normalized_status == "error" and record.order_id is not None
    )


def _normalize_record_keys(record: LiveOrderRecord) -> LiveOrderRecord:
    return replace(
        record,
        status=_require_text(record.status, message="status required"),
        submission_id=_require_text(record.submission_id, message="submission_id required"),
        order_hash=_normalize_optional_text(record.order_hash),
        order_id=_normalize_optional_text(record.order_id),
        message=_normalize_optional_text(record.message),
        signed_order_payload=_normalize_optional_payload(record.signed_order_payload),
        signed_order_fingerprint=_normalize_optional_text(record.signed_order_fingerprint),
    )


def _hydrate_record(payload: dict | None) -> LiveOrderRecord | None:
    if payload is None:
        return None

    timestamp = _normalize_required_text(payload.get("timestamp"))
    market_id = _normalize_required_text(payload.get("market_id"))
    side = _normalize_required_text(payload.get("side"))
    status = _normalize_required_text(payload.get("status"))
    submission_id = _normalize_required_text(payload.get("submission_id"))
    if submission_id is None:
        submission_id = _normalize_required_text(payload.get("client_order_id"))
    submitted_price = _parse_float(payload.get("submitted_price"))
    submitted_size = _parse_float(payload.get("submitted_size"))
    if any(
        value is None
        for value in (timestamp, market_id, side, status, submission_id, submitted_price, submitted_size)
    ):
        return None

    return LiveOrderRecord(
        timestamp=timestamp,
        market_id=market_id,
        token_id=_normalize_optional_text(payload.get("token_id")),
        side=side,
        submitted_price=submitted_price,
        submitted_size=submitted_size,
        status=status,
        submission_id=submission_id,
        order_hash=_normalize_optional_text(payload.get("order_hash")),
        order_id=_normalize_optional_text(payload.get("order_id")),
        message=_normalize_optional_text(payload.get("message")),
        signed_order_payload=_normalize_optional_payload(payload.get("signed_order_payload")),
        signed_order_fingerprint=_normalize_optional_text(payload.get("signed_order_fingerprint")),
    )


def _load_payload(raw_line: str) -> dict | None:
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _dump_record(record: LiveOrderRecord) -> str:
    return json.dumps(record.to_dict(), allow_nan=False)


def _parse_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _normalize_required_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_optional_payload(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    return value


def _require_text(value: object, *, message: str) -> str:
    normalized = _normalize_required_text(value)
    if normalized is None:
        raise ValueError(message)
    return normalized
