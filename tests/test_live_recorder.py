import json
import threading
from pathlib import Path

from pm_bot.models import LiveOrderRecord


def make_record(**overrides) -> LiveOrderRecord:
    payload = {
        "timestamp": "2026-04-19T00:00:00+00:00",
        "market_id": "btc-5m-1",
        "token_id": "token-up",
        "side": "UP",
        "submitted_price": 0.51,
        "submitted_size": 12.5,
        "status": "submitted",
        "submission_id": "submission-123",
        "order_id": None,
        "message": None,
    }
    payload.update(overrides)
    return LiveOrderRecord(**payload)


def test_live_recorder_appends_jsonl_and_fetches_by_submission_id(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)
    record = make_record()

    persisted = recorder.record(record)

    assert persisted == record
    assert json.loads(path.read_text(encoding="utf-8").strip()) == {
        "timestamp": "2026-04-19T00:00:00+00:00",
        "market_id": "btc-5m-1",
        "token_id": "token-up",
        "side": "UP",
        "submitted_price": 0.51,
        "submitted_size": 12.5,
        "status": "submitted",
        "submission_id": "submission-123",
    }
    assert recorder.get_by_submission_id("submission-123") == record


def test_live_recorder_round_trips_signed_order_payload_fingerprint_and_order_hash(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)
    record = make_record(
        signed_order_payload={
            "order": {
                "tokenId": "token-up",
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

    persisted = recorder.record(record)

    assert persisted == record
    assert json.loads(path.read_text(encoding="utf-8").strip()) == {
        "timestamp": "2026-04-19T00:00:00+00:00",
        "market_id": "btc-5m-1",
        "token_id": "token-up",
        "side": "UP",
        "submitted_price": 0.51,
        "submitted_size": 12.5,
        "status": "submitted",
        "submission_id": "submission-123",
        "signed_order_payload": {
            "order": {
                "tokenId": "token-up",
                "signature": "signature-123",
            },
            "owner": "api-key",
            "orderType": "FOK",
            "postOnly": False,
            "deferExec": False,
        },
        "signed_order_fingerprint": "fingerprint-123",
        "order_hash": "0xorder-hash-123",
    }
    assert recorder.get_by_submission_id("submission-123") == record



def test_live_recorder_hydrates_legacy_client_order_id_as_submission_id(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-19T00:00:00+00:00",
                "market_id": "btc-5m-1",
                "token_id": "token-up",
                "side": "UP",
                "submitted_price": 0.51,
                "submitted_size": 12.5,
                "status": "accepted",
                "client_order_id": "  legacy-submission-123  ",
                "order_id": "order-123",
                "message": "filled",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    recorder = LiveOrderRecorder(path)

    assert recorder.get_by_submission_id("legacy-submission-123") == make_record(
        submission_id="legacy-submission-123",
        status="accepted",
        order_id="order-123",
        message="filled",
    )


def test_live_recorder_canonicalizes_blank_message_on_record_round_trip(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)

    persisted = recorder.record(make_record(message="   "))

    assert persisted == make_record(message=None)
    assert json.loads(path.read_text(encoding="utf-8").strip()) == {
        "timestamp": "2026-04-19T00:00:00+00:00",
        "market_id": "btc-5m-1",
        "token_id": "token-up",
        "side": "UP",
        "submitted_price": 0.51,
        "submitted_size": 12.5,
        "status": "submitted",
        "submission_id": "submission-123",
    }
    assert recorder.get_by_submission_id("submission-123") == make_record(message=None)


def test_live_recorder_record_is_idempotent_by_submission_id(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)
    first = make_record()
    duplicate = make_record(
        submission_id="  submission-123  ",
        status="  matched  ",
        order_id="  order-999  ",
        message="duplicate",
    )

    assert recorder.record(first) == first
    assert recorder.record(duplicate) == first

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "timestamp": "2026-04-19T00:00:00+00:00",
        "market_id": "btc-5m-1",
        "token_id": "token-up",
        "side": "UP",
        "submitted_price": 0.51,
        "submitted_size": 12.5,
        "status": "submitted",
        "submission_id": "submission-123",
    }


def test_live_recorder_updates_status_with_atomic_rewrite(tmp_path: Path, monkeypatch):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)
    recorder.record(make_record())
    original_open = Path.open

    def guarded_open(self: Path, mode: str = "r", *args, **kwargs):
        if self == path and "w" in mode:
            raise AssertionError("ledger must not be rewritten in place")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    updated = recorder.update_status(
        "  submission-123  ",
        status=" matched ",
        order_id=" order-123 ",
        message="filled",
    )

    assert updated == make_record(status="matched", order_id="order-123", message="filled")
    assert recorder.get_by_submission_id("submission-123") == updated


def test_live_recorder_update_status_replaces_signed_order_payload_fingerprint_and_order_hash(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)
    recorder.record(
        make_record(
            signed_order_payload={"order": {"attempt": 1}},
            signed_order_fingerprint="fingerprint-1",
            order_hash="0xorder-hash-1",
        )
    )

    updated = recorder.update_status(
        "submission-123",
        status="accepted",
        signed_order_payload={"order": {"attempt": 2}},
        signed_order_fingerprint="fingerprint-2",
        order_hash="0xorder-hash-2",
    )

    assert updated == make_record(
        status="accepted",
        signed_order_payload={"order": {"attempt": 2}},
        signed_order_fingerprint="fingerprint-2",
        order_hash="0xorder-hash-2",
    )
    assert recorder.get_by_submission_id("submission-123") == updated


def test_live_recorder_lists_only_open_submitted_orders(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(make_record(submission_id="submission-submitted", status="submitted"))
    recorder.record(make_record(submission_id="submission-accepted", status="Accepted"))
    recorder.record(make_record(submission_id="submission-delayed", status="delayed"))
    recorder.record(make_record(submission_id="submission-unmatched", status="unmatched"))
    recorder.record(make_record(submission_id="submission-open", status="open"))
    recorder.record(make_record(submission_id="submission-pending-reconcile", status="pending_reconcile"))
    recorder.record(make_record(submission_id="submission-matched", status="matched", order_id="order-1"))
    recorder.record(make_record(submission_id="submission-cancelled", status="cancelled", order_id="order-2"))
    recorder.record(make_record(submission_id="submission-error", status="error", message="rejected"))

    open_orders = recorder.open_submitted_orders()

    assert [record.submission_id for record in open_orders] == [
        "submission-submitted",
        "submission-accepted",
        "submission-delayed",
        "submission-unmatched",
        "submission-open",
        "submission-pending-reconcile",
    ]


def test_live_recorder_lists_trackable_error_submitted_orders(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(
        make_record(submission_id="submission-error-trackable", status="error", order_id="order-error")
    )

    open_orders = recorder.open_submitted_orders()

    assert [record.submission_id for record in open_orders] == ["submission-error-trackable"]


def test_live_recorder_excludes_untrackable_error_submitted_orders(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(make_record(submission_id="submission-error", status="error", message="rejected"))

    assert recorder.open_submitted_orders() == []


def test_live_recorder_serializes_concurrent_duplicate_records(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)
    record = make_record()
    release = threading.Event()
    read_started = threading.Event()
    original_read_text = Path.read_text

    def blocking_read_text(self: Path, *args, **kwargs):
        content = original_read_text(self, *args, **kwargs)
        if self == path and not read_started.is_set():
            read_started.set()
            release.wait(timeout=2)
        return content

    threads = [threading.Thread(target=lambda: recorder.record(record)) for _ in range(2)]

    try:
        path.write_text("", encoding="utf-8")
        Path.read_text = blocking_read_text
        threads[0].start()
        assert read_started.wait(timeout=2)
        threads[1].start()
        release.set()
        for thread in threads:
            thread.join(timeout=2)
    finally:
        Path.read_text = original_read_text

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["submission_id"] == "submission-123"
