# pm-bot Order Hash Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pending_reconcile` live journal rows recoverable even when no `order_id` was returned, by persisting the canonical upstream order hash before POST and reconciling by that hash.

**Architecture:** Keep the existing `submission_id`-based local journal, but add an upstream-correlatable `order_hash` computed from the exact signed order. `order_hash` becomes the first recovery handle when `order_id` is unknown. `LiveTradingService` should reconcile by `order_id` when present, otherwise by `order_hash`, and only fall back to exact replay once.

**Tech Stack:** Python 3.11, py-clob-client-v2, JSONL persistence, pytest

---

## File Map

### Files Modified

| File | Purpose |
| ---- | ------- |
| `src/pm_bot/models.py` | Extend `LiveOrderRecord` with persisted upstream `order_hash` |
| `src/pm_bot/live_recorder.py` | Persist/hydrate `order_hash`; support updates that backfill `order_id` from `order_hash` recovery |
| `src/pm_bot/polymarket_live_client.py` | Compute canonical upstream order hash from prepared signed order; add helper to query by hash and parse duplicate replay errors |
| `src/pm_bot/execution.py` | Persist `order_hash` before post alongside `submission_id` and signed payload |
| `src/pm_bot/live_service.py` | Reconcile `pending_reconcile` rows by `order_hash` when `order_id` is missing; exact-replay once if not found |
| `tests/test_live_client.py` | TDD coverage for order-hash computation, duplicate replay parsing, and journal persistence |
| `tests/test_live_recorder.py` | TDD coverage for `order_hash` round-trip and update behavior |
| `tests/test_live_service.py` | TDD coverage for reconcile-by-hash and one-shot exact replay fallback |

---

## Task 1: Persist upstream `order_hash` before post

**Files:**
- Modify: `src/pm_bot/models.py`
- Modify: `src/pm_bot/polymarket_live_client.py`
- Modify: `src/pm_bot/execution.py`
- Modify: `src/pm_bot/live_recorder.py`
- Test: `tests/test_live_client.py`
- Test: `tests/test_live_recorder.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_live_client.py`:

```python
def test_prepared_market_order_exposes_upstream_order_hash():
    client = make_live_client()
    request = make_execution_request()

    prepared = client.prepare_market_order(request)

    assert prepared.order_hash
    assert prepared.order_hash.startswith("0x")


def test_live_executor_persists_order_hash_before_post(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    executor = make_live_executor(recorder=recorder)
    request = make_execution_request()

    executor.execute(request)

    record = recorder.get_by_submission_id(request.submission_id)
    assert record is not None
    assert record.order_hash
```

Add to `tests/test_live_recorder.py`:

```python
def test_live_recorder_round_trips_order_hash(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    record = make_live_record(submission_id="sub-1", order_hash="0xabc123")

    recorder.record(record)

    loaded = recorder.get_by_submission_id("sub-1")
    assert loaded is not None
    assert loaded.order_hash == "0xabc123"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_live_client.py -k order_hash
.venv/bin/pytest -q tests/test_live_recorder.py -k order_hash
```

Expected: FAIL because no `order_hash` exists yet.

- [ ] **Step 3: Implement the minimal `order_hash` persistence path**

Update `src/pm_bot/models.py`:

```python
@dataclass(slots=True)
class LiveOrderRecord:
    ...
    submission_id: str
    order_hash: str | None = None
    order_id: str | None = None
    signed_order_fingerprint: str | None = None
    signed_order_payload: dict[str, Any] | None = None
    ...
```

Update `src/pm_bot/polymarket_live_client.py`:

```python
@dataclass(slots=True)
class PreparedMarketOrder:
    signed_order: Any
    payload: dict[str, Any]
    fingerprint: str
    order_hash: str


def _compute_order_hash(self, signed_order: Any) -> str:
    if _is_v2_order(signed_order):
        typed_data = self._builder_v2.build_order_typed_data(signed_order)
        return self._builder_v2.build_order_hash(typed_data)
    typed_data = self._builder_v1.build_order_typed_data(signed_order)
    return self._builder_v1.build_order_hash(typed_data)
```

Make `prepare_market_order()` populate `PreparedMarketOrder.order_hash`.

Update `src/pm_bot/execution.py` so the pre-post journal row writes `order_hash=prepared.order_hash`.

Update `src/pm_bot/live_recorder.py` to serialize/hydrate `order_hash`.

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```bash
.venv/bin/pytest -q tests/test_live_client.py -k order_hash
.venv/bin/pytest -q tests/test_live_recorder.py -k order_hash
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/models.py src/pm_bot/polymarket_live_client.py src/pm_bot/execution.py src/pm_bot/live_recorder.py tests/test_live_client.py tests/test_live_recorder.py
git commit -m "feat: persist upstream order hash for live submissions"
```

---

## Task 2: Reconcile by `order_hash` before exact replay

**Files:**
- Modify: `src/pm_bot/polymarket_live_client.py`
- Modify: `src/pm_bot/live_service.py`
- Modify: `src/pm_bot/live_recorder.py`
- Test: `tests/test_live_service.py`
- Test: `tests/test_live_client.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_live_service.py`:

```python
def test_live_service_recovers_missing_order_id_via_order_hash(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(
        make_live_record(
            submission_id="sub-1",
            order_hash="0xhash-1",
            order_id=None,
            status="pending_reconcile",
        )
    )

    live_client = StubLiveClient(order_by_hash={"0xhash-1": {"id": "0xhash-1", "status": "live"}})
    service = make_live_service(live_client=live_client, recorder=recorder)

    result = service.oneshot(interval="5m", balance=1000.0, live_confirmed=True)

    assert result.action == "skip"
    repaired = recorder.get_by_submission_id("sub-1")
    assert repaired.order_id == "0xhash-1"
```

Add to `tests/test_live_client.py`:

```python
def test_live_client_can_query_by_order_hash():
    client = make_live_client()
    payload = client.get_order_by_hash("0xhash-1")
    assert payload["id"] == "0xhash-1"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_live_service.py -k order_hash
.venv/bin/pytest -q tests/test_live_client.py -k order_hash
```

Expected: FAIL because the recovery path does not exist yet.

- [ ] **Step 3: Implement order-hash reconciliation**

Update `src/pm_bot/polymarket_live_client.py`:

```python
def get_order_by_hash(self, order_hash: str) -> dict[str, Any]:
    return self.get_order(order_hash)
```

Update `src/pm_bot/live_service.py`:

```python
if row.order_id:
    venue = self._live_client.get_order(row.order_id)
elif row.order_hash:
    venue = self._live_client.get_order_by_hash(row.order_hash)
else:
    blockers.append("live_order_missing_order_hash")
    continue

if venue:
    self._live_recorder.update_status(
        row.submission_id,
        status=_normalize_venue_status(venue),
        order_id=venue.get("id") or venue.get("orderID") or row.order_hash,
    )
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```bash
.venv/bin/pytest -q tests/test_live_service.py -k order_hash
.venv/bin/pytest -q tests/test_live_client.py -k order_hash
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/polymarket_live_client.py src/pm_bot/live_service.py tests/test_live_service.py tests/test_live_client.py
git commit -m "feat: reconcile pending live submissions by order hash"
```

---

## Task 3: Exact replay once, then parse duplicate hash

**Files:**
- Modify: `src/pm_bot/polymarket_live_client.py`
- Modify: `src/pm_bot/live_service.py`
- Test: `tests/test_live_service.py`
- Test: `tests/test_live_client.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_live_service.py`:

```python
def test_live_service_replays_exact_signed_order_once_when_hash_lookup_misses(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(
        make_live_record(
            submission_id="sub-1",
            order_hash="0xhash-1",
            order_id=None,
            status="pending_reconcile",
            signed_order_payload={"order": {"salt": 1}},
        )
    )

    live_client = StubLiveClient(
        order_by_hash={},
        replay_response={"success": True, "orderID": "0xhash-1", "status": "live", "errorMsg": ""},
    )
    service = make_live_service(live_client=live_client, recorder=recorder)

    service.oneshot(interval="5m", balance=1000.0, live_confirmed=True)

    repaired = recorder.get_by_submission_id("sub-1")
    assert repaired.order_id == "0xhash-1"
```

Add to `tests/test_live_client.py`:

```python
def test_parse_duplicate_error_extracts_order_hash():
    assert parse_duplicate_order_hash("order 0xabc is invalid. Duplicated.") == "0xabc"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_live_service.py -k replay
.venv/bin/pytest -q tests/test_live_client.py -k duplicate_order_hash
```

Expected: FAIL because replay/parsing does not exist yet.

- [ ] **Step 3: Implement exact replay-once fallback**

Update `src/pm_bot/polymarket_live_client.py`:

```python
_DUPLICATE_RE = re.compile(r"order\s+(0x[a-fA-F0-9]+)\s+is invalid\.\s+Duplicated\.")


def parse_duplicate_order_hash(message: str | None) -> str | None:
    if not message:
        return None
    match = _DUPLICATE_RE.search(message)
    return match.group(1) if match else None


def replay_signed_order(self, signed_order_payload: dict[str, Any]) -> dict[str, Any]:
    return self._get_authenticated_client().post_order(signed_order_payload["order"], OrderType.FOK)
```

Update `src/pm_bot/live_service.py`:

```python
if not venue and row.signed_order_payload:
    replay = self._live_client.replay_signed_order(row.signed_order_payload)
    duplicate_hash = parse_duplicate_order_hash(replay.get("errorMsg"))
    recovered_order_id = replay.get("orderID") or duplicate_hash
    if recovered_order_id:
        self._live_recorder.update_status(
            row.submission_id,
            status=_normalize_venue_status(replay),
            order_id=recovered_order_id,
            message=replay.get("errorMsg"),
        )
```

Ensure the replay happens at most once per blocked row during a reconciliation pass.

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```bash
.venv/bin/pytest -q tests/test_live_service.py -k replay
.venv/bin/pytest -q tests/test_live_client.py -k duplicate_order_hash
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pm_bot/models.py src/pm_bot/live_recorder.py src/pm_bot/polymarket_live_client.py src/pm_bot/execution.py src/pm_bot/live_service.py tests/test_live_client.py tests/test_live_recorder.py tests/test_live_service.py
git commit -m "feat: recover pending live submissions by order hash"
```

---

## Self-Review

### Spec coverage
- Canonical upstream `order_hash` persisted before post: covered by Task 1.
- Reconcile by hash when `order_id` is missing: covered by Task 2.
- Exact replay-once fallback + duplicate hash parsing: covered by Task 3.

### Placeholder scan
- No `TODO` / `TBD` placeholders left.
- Each task has exact file paths, test names, code blocks, and commands.

### Type consistency
- `submission_id` remains the local durable key.
- `order_hash` is the upstream correlator and can be backfilled into `order_id` when recovered.
- `signed_order_payload` is reused for exact replay, not regenerated.
