# pm-bot Replay-Safe Live Submission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make pm-bot live order submission replay-safe by introducing a local `submission_id`, persisting the exact signed order payload before post, and treating ambiguous submission outcomes as `pending_reconcile` instead of terminal error.

**Architecture:** Keep the current executor/journal split, but change the live contract from “journal by client_order_id” to “journal by local submission_id + persisted signed order.” Refactor the Polymarket live wrapper from a one-step `create_and_post_market_order()` call into a two-step prepare/post flow so retries can replay the same signed payload rather than creating a brand-new order.

**Tech Stack:** Python 3.11, py-clob-client-v2, JSONL persistence with `flock`, pytest

---

## File Map

### Files Modified

| File | Purpose |
| ---- | ------- |
| `src/pm_bot/models.py` | Replace live-path `client_order_id` identity with `submission_id`; extend `LiveOrderRecord` to persist signed-order payload/fingerprint and submission state |
| `src/pm_bot/live_recorder.py` | Key the journal by `submission_id`; persist and hydrate signed-order payload/fingerprint and `pending_reconcile` state |
| `src/pm_bot/execution.py` | Make `ExecutionRequest` generate `submission_id`; emit `ExecutionResult.submission_id`; wire replay-safe journal semantics |
| `src/pm_bot/polymarket_live_client.py` | Split market-order flow into prepare-signed-order and post-signed-order methods |
| `tests/test_live_client.py` | Add red/green coverage for prepare/persist/post flow, `submission_id`, and ambiguous submission outcomes |
| `tests/test_live_recorder.py` | Add red/green coverage for `submission_id`-keyed persistence and `pending_reconcile` journal rows |

### Files Created (optional, only if needed)

| File | Purpose |
| ---- | ------- |
| `src/pm_bot/live_submission.py` | Optional small helper module if signed-order payload/fingerprint logic gets noisy in `execution.py` / `polymarket_live_client.py` |

---

## Task 1: Replace live-path identity with `submission_id`

**Files:**
- Modify: `src/pm_bot/execution.py`
- Modify: `src/pm_bot/models.py`
- Modify: `src/pm_bot/live_recorder.py`
- Test: `tests/test_live_client.py`
- Test: `tests/test_live_recorder.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_live_client.py` and `tests/test_live_recorder.py`:

```python
def test_execution_request_generates_submission_id_not_live_client_order_id():
    request = ExecutionRequest(
        market_id="m-1",
        token_id="token-up",
        side="UP",
        price=0.51,
        size_usd=10.0,
        order_type="market",
    )

    assert request.submission_id
    assert hasattr(request, "submission_id")


def test_live_recorder_keys_records_by_submission_id(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    record = LiveOrderRecord(
        timestamp=datetime.now(UTC),
        market_id="m-1",
        token_id="token-up",
        side="UP",
        submitted_price=0.51,
        submitted_size=10.0,
        status="prepared",
        submission_id="sub-123",
        signed_order_fingerprint="fp-123",
        signed_order_payload={"signed": "payload"},
    )

    recorder.record(record)
    loaded = recorder.get_by_submission_id("sub-123")

    assert loaded is not None
    assert loaded.submission_id == "sub-123"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_live_client.py -k submission_id
.venv/bin/pytest -q tests/test_live_recorder.py -k submission_id
```

Expected: FAIL because `submission_id` does not exist yet.

- [ ] **Step 3: Implement the minimal model and journal identity change**

Update `src/pm_bot/execution.py`:

```python
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
    submission_id: str = field(default_factory=_new_submission_id)
    metadata: dict[str, Any] = field(default_factory=dict)
```

Update `src/pm_bot/models.py`:

```python
@dataclass(slots=True)
class LiveOrderRecord:
    timestamp: datetime
    market_id: str
    token_id: str
    side: str
    submitted_price: float
    submitted_size: float
    status: str
    submission_id: str
    signed_order_fingerprint: str
    signed_order_payload: dict[str, Any]
    order_id: str | None = None
    message: str | None = None
    exchange_status: str | None = None
```

Update `src/pm_bot/live_recorder.py` so the primary lookup/dedupe key becomes `submission_id`.

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```bash
.venv/bin/pytest -q tests/test_live_client.py -k submission_id
.venv/bin/pytest -q tests/test_live_recorder.py -k submission_id
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/execution.py src/pm_bot/models.py src/pm_bot/live_recorder.py tests/test_live_client.py tests/test_live_recorder.py
git commit -m "refactor: key live submissions by submission_id"
```

---

## Task 2: Split live submission into prepare → persist → post

**Files:**
- Modify: `src/pm_bot/polymarket_live_client.py`
- Modify: `src/pm_bot/execution.py`
- Test: `tests/test_live_client.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_live_client.py`:

```python
def test_live_client_prepares_signed_market_order_before_post(monkeypatch):
    client = PolymarketLiveClient(make_live_config())

    request = ExecutionRequest(
        market_id="m-1",
        token_id="token-up",
        side="UP",
        price=0.51,
        size_usd=10.0,
        order_type="market",
    )

    signed_order = client.prepare_market_order(request)

    assert signed_order is not None


def test_live_executor_persists_signed_order_before_post(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    executor = LivePolymarketExecutor(live_client=FakePreparedClient(), live_recorder=recorder)
    request = ExecutionRequest(
        market_id="m-1",
        token_id="token-up",
        side="UP",
        price=0.51,
        size_usd=10.0,
        order_type="market",
    )

    result = executor.execute(request)
    record = recorder.get_by_submission_id(request.submission_id)

    assert result.submission_id == request.submission_id
    assert record is not None
    assert record.signed_order_payload
    assert record.signed_order_fingerprint
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_live_client.py -k "prepare_market_order or persists_signed_order_before_post"
```

Expected: FAIL because the wrapper only exposes `post_order()` today.

- [ ] **Step 3: Implement the two-step client flow**

Update `src/pm_bot/polymarket_live_client.py`:

```python
class PolymarketLiveClient:
    def prepare_market_order(self, request: ExecutionRequest) -> dict[str, Any]:
        token_id = _require_text(request.token_id, message="live execution requires token_id")
        options = _build_options(request.metadata)
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=request.size_usd,
            side=Side.BUY,
            order_type=OrderType.FOK,
            price=request.price,
        )
        signed = self._get_authenticated_client().create_market_order(
            order_args,
            options=options,
            order_type=OrderType.FOK,
        )
        return signed

    def post_prepared_order(self, signed_order: dict[str, Any]) -> Any:
        return self._get_authenticated_client().post_order(
            signed_order,
            order_type=OrderType.FOK,
        )
```

Update `src/pm_bot/execution.py`:

```python
def _signed_order_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LivePolymarketExecutor:
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        signed_order = self._client.prepare_market_order(request)
        fingerprint = _signed_order_fingerprint(signed_order)
        if self._live_recorder:
            self._live_recorder.record(
                LiveOrderRecord(
                    timestamp=self._journal_timestamp(request),
                    market_id=request.market_id,
                    token_id=_require_token_id(request.token_id),
                    side=request.side,
                    submitted_price=request.price,
                    submitted_size=request.size_usd,
                    status="prepared",
                    submission_id=request.submission_id,
                    signed_order_fingerprint=fingerprint,
                    signed_order_payload=signed_order,
                )
            )
        response = self._client.post_prepared_order(signed_order)
        ...
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```bash
.venv/bin/pytest -q tests/test_live_client.py -k "prepare_market_order or persists_signed_order_before_post"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/polymarket_live_client.py src/pm_bot/execution.py tests/test_live_client.py
git commit -m "refactor: persist signed live orders before post"
```

---

## Task 3: Treat ambiguous submission outcomes as `pending_reconcile`

**Files:**
- Modify: `src/pm_bot/execution.py`
- Modify: `src/pm_bot/models.py`
- Modify: `src/pm_bot/live_recorder.py`
- Test: `tests/test_live_client.py`
- Test: `tests/test_live_recorder.py`

- [ ] **Step 1: Write the failing tests**

Add these tests:

```python
def test_live_executor_marks_transport_exception_as_pending_reconcile(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    executor = LivePolymarketExecutor(live_client=RaisingPreparedClient(), live_recorder=recorder)
    request = ExecutionRequest(
        market_id="m-1",
        token_id="token-up",
        side="UP",
        price=0.51,
        size_usd=10.0,
        order_type="market",
    )

    with pytest.raises(RuntimeError):
        executor.execute(request)

    record = recorder.get_by_submission_id(request.submission_id)
    assert record is not None
    assert record.status == "pending_reconcile"


def test_live_recorder_open_submitted_orders_includes_pending_reconcile(tmp_path: Path):
    recorder = LiveOrderRecorder(tmp_path / "live_orders.jsonl")
    recorder.record(
        LiveOrderRecord(
            timestamp=datetime.now(UTC),
            market_id="m-1",
            token_id="token-up",
            side="UP",
            submitted_price=0.51,
            submitted_size=10.0,
            status="pending_reconcile",
            submission_id="sub-123",
            signed_order_fingerprint="fp-123",
            signed_order_payload={"signed": "payload"},
        )
    )

    open_records = recorder.open_submitted_orders()
    assert [r.submission_id for r in open_records] == ["sub-123"]
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_live_client.py -k pending_reconcile
.venv/bin/pytest -q tests/test_live_recorder.py -k pending_reconcile
```

Expected: FAIL because transport exceptions are currently journaled as `error`.

- [ ] **Step 3: Implement the minimal ambiguity-state change**

Update `src/pm_bot/execution.py`:

```python
except Exception as exc:
    if self._live_recorder:
        self._live_recorder.update_status(
            request.submission_id,
            status="pending_reconcile",
            message=str(exc),
        )
    raise
```

Update `src/pm_bot/live_recorder.py`:

```python
_OPEN_SUBMITTED_STATUSES = {
    "prepared",
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
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```bash
.venv/bin/pytest -q tests/test_live_client.py -k pending_reconcile
.venv/bin/pytest -q tests/test_live_recorder.py -k pending_reconcile
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pm_bot/execution.py src/pm_bot/models.py src/pm_bot/live_recorder.py tests/test_live_client.py tests/test_live_recorder.py
git commit -m "feat: make live submissions replay-safe for reconcile"
```

---

## Self-Review

### Spec coverage
- `submission_id` replaces ambiguous live `client_order_id`: covered by Task 1.
- exact signed order persisted before post: covered by Task 2.
- ambiguous outcomes become `pending_reconcile`: covered by Task 3.
- retry safety via replaying the same signed payload: enabled by Task 2 + Task 3.

### Placeholder scan
- No `TODO` / `TBD` placeholders left.
- Each step includes exact file paths, code blocks, and commands.

### Type consistency
- `submission_id` is the durable local key across request, result, and journal.
- `signed_order_payload` / `signed_order_fingerprint` are introduced in Task 1 and consumed in Tasks 2/3 with consistent names.
