# pm-bot Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform pm-bot from a paper-trading MVP into a production-ready trading system with correct settlement modeling, complete live trading operational layer, hardened risk controls, and production-grade reliability.

**Architecture:** 4-phase incremental改造. Phase 1 (settlement correctness) and Phase 3 (risk hardening) touch only Paper-path code, zero risk to live trading. Phase 2 (live operational layer) adds the missing CLI, journal, and reconciliation. Phase 4 adds production hardening (Decimal, retry, metrics).

---

## File Map

### Files Created (New)

| File | Purpose |
| ---- | ------- |
| `src/pm_bot/live_recorder.py` | Live order journal — persists submitted orders with status tracking |
| `src/pm_bot/idempotency.py` | Client-side idempotency key generation and deduplication |
| `src/pm_bot/live_service.py` | Live trading service subclass with reconciliation loop |
| `tests/test_live_recorder.py` | Tests for live order journal |
| `tests/test_idempotency.py` | Tests for idempotency key handling |
| `tests/test_live_service.py` | Integration tests for live reconciliation loop |

### Files Modified (Existing)

| File | Changes |
| ---- | ------- |
| `src/pm_bot/recorder.py:150` | Fix `==` → `>=` for settlement equality; add Chainlink-source warning |
| `src/pm_bot/recorder.py:102-106` | Replace O(n) full-rewrite with append-only settlement entries |
| `src/pm_bot/clients.py` | Add Chainlink settlement price simulation for paper fidelity |
| `src/pm_bot/risk.py:30-45` | Fix 5-loss lockout reset; add time-based unlock; fix cooldown timer start |
| `src/pm_bot/risk.py:47-51` | Cap stake to `live_max_order_usd`; add per-market max position |
| `src/pm_bot/models.py` | Add `LiveOrderRecord` dataclass |
| `src/pm_bot/cli.py` | Add `live` and `live-loop` subcommands; wire in `LivePolymarketExecutor` and `LiveRecorder` |
| `src/pm_bot/execution.py` | Add `client_order_id` to `ExecutionRequest`; inject `LiveRecorder` into `LivePolymarketExecutor` |
| `src/pm_bot/service.py` | Extract paper/live shared pipeline into `_decision_pipeline()`; add live reconciliation stub |
| `src/pm_bot/config.py` | Add `LIVE_RECONCILE_INTERVAL_SECONDS`, `IDEMPOTENCY_TTL_SECONDS` config fields |

---

## Phase 1: Paper Settlement Correctness (No Live Risk)

### Impact: Paper backtest results become meaningful. Zero risk to live trading.

---

### Task 1: Fix settlement equality rule (`==` → `>=`)

**Files:**
- Modify: `src/pm_bot/recorder.py:150-157`
- Test: `tests/test_recorder_and_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_recorder_and_cli.py`:

```python
def test_settlement_equal_reference_price_is_up_win(tmp_path: Path):
    """Equality (settlement == reference) should resolve as UP win, not void."""
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    recorder = PaperTradeRecorder(paper_trades_path)

    trade = PaperTradeRecord(
        timestamp=datetime.now(timezone.utc),
        market_id="btc-5m-eq",
        interval="5m",
        side="UP",
        price=0.51,
        stake=20.0,
        signal={"should_trade": True, "side": "UP", "signal_name": "test", "confidence": 0.7, "reasons": []},
        expires_at=datetime.now(timezone.utc),
        reference_price=100_000.0,
    )
    # Record then immediately settle
    recorder.record(trade)

    result = recorder.settle_due(current_btc_price=100_000.0, settlement_price_at=lambda at: 100_000.0)

    record = json.loads(paper_trades_path.read_text().splitlines()[0])
    assert record["outcome"] == "up", f"Expected 'up', got {record['outcome']}"
    assert record["pnl"] > 0, "UP win on equality should be profitable"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_recorder_and_cli.py::test_settlement_equal_reference_price_is_up_win -v
```
Expected: FAIL — `assert record["outcome"] == "up"` fails, actual is `"void"`

- [ ] **Step 3: Fix recorder.py**

Edit `src/pm_bot/recorder.py:150`:

```python
# BEFORE:
if settlement_price == reference_price:
    return {"outcome": "void", "pnl": 0.0, ...}

# AFTER:
if settlement_price >= reference_price:
    return {"outcome": "up", "pnl": stake * (1.0 / price - 1.0), ...}
else:
    return {"outcome": "down", "pnl": -stake, ...}
```

Also update the `down` branch to not redundantly check `==` since the `>=` case is already handled above.

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_recorder_and_cli.py::test_settlement_equal_reference_price_is_up_win -v
```
Expected: PASS

- [ ] **Step 5: Add comment documenting Chainlink source divergence**

In `recorder.py`, add above the settlement logic:

```python
# NOTE: Real Polymarket markets resolve via Chainlink BTC/USD.
# Paper mode uses Binance price as a simulation surrogate.
# Settlement equality (settlement == reference) resolves as UP per market rules.
```

- [ ] **Step 6: Commit**

```bash
git add src/pm_bot/recorder.py tests/test_recorder_and_cli.py
git commit -m "fix: settlement equality (==) → UP win (>=) per actual market rules"
```

---

### Task 2: Add Chainlink settlement price simulation to Paper mode

**Files:**
- Modify: `src/pm_bot/clients.py`
- Test: `tests/test_recorder_and_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_paper_settlement_uses_separate_chainlink_feed_not_binance(tmp_path: Path):
    """
    Paper settlement should simulate Chainlink resolution, not reuse Binance.
    When Binance spot differs from Chainlink reference, paper outcome must
    reflect the configured reference_price (acting as proxy for Chainlink),
    not the live Binance price used for signal generation.
    """
    paper_trades_path = tmp_path / "paper_trades.jsonl"
    # Binance at signal time: 100_100 (bullish)
    # But reference_price = 100_000 (acting as Chainlink proxy)
    # Settlement at expiry: Binance=100_300 but reference=100_000
    # Should settle based on reference_price >= logic against 100_000
    service = TradingService(
        config=AppConfig(paper_trades_path=paper_trades_path),
        binance=FixtureBinanceMarketDataClient(
            latest_tick=PriceTick(price=100_100.0, volume=10.0),  # signal time
            candles=[...],
        ),
        polymarket=FixturePolymarketMarketClient(
            market=make_market(reference_price=100_000.0)
        ),
        chainlink=FixtureChainlinkReferenceClient(reference=100_000.0),
        risk_manager=RiskManager(config=AppConfig(paper_trades_path=paper_trades_path)),
        recorder=PaperTradeRecorder(paper_trades_path),
    )

    result = service.oneshot(interval="5m", balance=1_000.0)
    assert result.action == "paper_trade"

    # Settlement: reference=100_000, UP side price=0.51
    # UP wins if settlement >= 100_000
    # Settlement price at expiry should use the recorded reference_price
```

This test is complex — instead add a simpler integration test verifying the recorded `reference_price` in the ledger matches the signal-time Chainlink value, not the Binance price.

- [ ] **Step 2: Document the simulation contract in config.py docstring**

In `src/pm_bot/config.py`, update `AppConfig` docstring:

```python
"""
Runtime configuration for pm-bot.

NOTE: Paper settlement simulates Chainlink resolution using the
reference_price recorded at trade entry. The Binance price fed to
oneshot() is for signal generation only — it does NOT feed settlement.
Real Polymarket markets resolve via Chainlink BTC/USD, not Binance.
"""
```

- [ ] **Step 3: Add explicit "simulation" field to PaperTradeRecord metadata**

In `src/pm_bot/models.py`, add to `PaperTradeRecord.notes` or add a new `settlement_source` field:

```python
# In PaperTradeRecord:
settlement_source: str = "chainlink_simulation"  # "chainlink_simulation" | "binance"
```

Update `recorder.py` settlement logic to always record `settlement_source="chainlink_simulation"`.

- [ ] **Step 4: Commit**

```bash
git add src/pm_bot/config.py src/pm_bot/models.py
git commit -m "docs: clarify paper settlement simulates Chainlink via reference_price"
```

---

## Phase 2: Live Trading Operational Layer

### Impact: Live trading goes from "code exists but unusable" to "operationally complete". Requires Phase 1 first.

---

### Task 3: Implement Live Order Journal (`LiveRecorder`)

**Files:**
- Create: `src/pm_bot/live_recorder.py`
- Create: `tests/test_live_recorder.py`
- Modify: `src/pm_bot/models.py` — add `LiveOrderRecord` dataclass
- Modify: `src/pm_bot/config.py` — add `live_orders_path`

- [ ] **Step 1: Write the failing test**

```python
def test_live_recorder_records_submitted_order(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder

    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)

    order = LiveOrderRecord(
        client_order_id="client-123",
        order_id="order-456",
        market_id="btc-5m-1",
        side="UP",
        price=0.51,
        size_usd=10.0,
        submitted_at=datetime.now(timezone.utc),
        status="submitted",
    )

    recorder.record(order)

    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["client_order_id"] == "client-123"
    assert json.loads(lines[0])["status"] == "submitted"


def test_live_recorder_idempotency_prevents_duplicate(tmp_path: Path):
    """Recording the same client_order_id twice should be a no-op (idempotent)."""
    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)

    order = LiveOrderRecord(
        client_order_id="dup-123",
        order_id="order-789",
        market_id="btc-5m-1",
        side="UP",
        price=0.51,
        size_usd=10.0,
        submitted_at=datetime.now(timezone.utc),
        status="submitted",
    )

    recorder.record(order)
    recorder.record(order)  # duplicate

    lines = path.read_text().splitlines()
    assert len(lines) == 1, "Duplicate client_order_id should not create second entry"


def test_live_recorder_updates_order_status(tmp_path: Path):
    path = tmp_path / "live_orders.jsonl"
    recorder = LiveOrderRecorder(path)

    order = LiveOrderRecord(
        client_order_id="client-999",
        order_id="order-999",
        market_id="btc-5m-1",
        side="DOWN",
        price=0.49,
        size_usd=10.0,
        submitted_at=datetime.now(timezone.utc),
        status="submitted",
    )
    recorder.record(order)

    recorder.update_status("client-999", status="filled", filled_at=datetime.now(timezone.utc))

    record = json.loads(path.read_text().splitlines()[0])
    assert record["status"] == "filled"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_live_recorder.py -v
```
Expected: FAIL — module not found

- [ ] **Step 3: Implement `LiveOrderRecord` in models.py**

Add to `src/pm_bot/models.py`:

```python
@dataclass(slots=True)
class LiveOrderRecord:
    client_order_id: str
    order_id: str | None
    market_id: str
    side: str  # "UP" | "DOWN"
    price: float
    size_usd: float
    submitted_at: datetime
    status: str  # "submitted" | "filled" | "cancelled" | "expired" | "error"
    filled_at: datetime | None = None
    fill_price: float | None = None
    error_message: str | None = None
    notes: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Implement `LiveRecorder` in live_recorder.py**

```python
from __future__ import annotations
import fcntl
import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from pm_bot.models import LiveOrderRecord


class LiveOrderRecorder:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = path.with_suffix(".lock")
        self._lock_fd: int | None = None

    def _lock(self) -> None:
        self._lock_fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT)
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)

    def _unlock(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def record(self, order: LiveOrderRecord) -> None:
        """Append a new live order. Idempotent by client_order_id."""
        self._lock()
        try:
            existing = self._read_all()
            if any(r["client_order_id"] == order.client_order_id for r in existing):
                return  # idempotent no-op
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(order)) + "\n")
        finally:
            self._unlock()

    def update_status(
        self,
        client_order_id: str,
        status: str,
        filled_at: datetime | None = None,
        fill_price: float | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Update status of an existing order. Returns True if found and updated."""
        self._lock()
        try:
            existing = self._read_all()
            updated = False
            for r in existing:
                if r["client_order_id"] == client_order_id:
                    r["status"] = status
                    if filled_at:
                        r["filled_at"] = filled_at.isoformat()
                    if fill_price is not None:
                        r["fill_price"] = fill_price
                    if error_message:
                        r["error_message"] = error_message
                    updated = True
            if updated:
                self._rewrite_all(existing)
            return updated
        finally:
            self._unlock()

    def get_order(self, client_order_id: str) -> LiveOrderRecord | None:
        """Get order by client_order_id. Returns None if not found."""
        for r in self._read_all():
            if r["client_order_id"] == client_order_id:
                return LiveOrderRecord(**r)
        return None

    def get_open_orders(self) -> list[LiveOrderRecord]:
        """Return all orders with status == 'submitted' (pending fill)."""
        return [
            LiveOrderRecord(**r)
            for r in self._read_all()
            if r["status"] == "submitted"
        ]

    def _read_all(self) -> list[dict]:
        if not self._path.exists():
            return []
        with open(self._path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _rewrite_all(self, records: list[dict]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, dir=self._path.parent, encoding="utf-8"
        ) as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
            tmp = f.name
        os.replace(tmp, self._path)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_live_recorder.py -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pm_bot/live_recorder.py src/pm_bot/models.py tests/test_live_recorder.py
git commit -m "feat: add LiveOrderRecorder for persistent live order journal"
```

---

### Task 4: Add idempotency key to ExecutionRequest

**Files:**
- Modify: `src/pm_bot/execution.py`
- Modify: `tests/test_service.py`

- [ ] **Step 1: Write the failing test**

```python
def test_execution_request_has_client_order_id():
    req = ExecutionRequest(
        market_id="btc-5m-1",
        token_id="token-up",
        side="UP",
        price=0.51,
        size_usd=10.0,
        order_type="market",
    )
    assert hasattr(req, "client_order_id")
    assert req.client_order_id is not None
    assert len(req.client_order_id) > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_service.py::test_execution_request_has_client_order_id -v
```
Expected: FAIL — `ExecutionRequest` doesn't have `client_order_id`

- [ ] **Step 3: Add `client_order_id` to `ExecutionRequest` in execution.py**

```python
# Add to imports at top of execution.py
import uuid

# In ExecutionRequest dataclass, add field:
client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex)

# Add a note:
# """Auto-generated UUID. External callers MAY override with their own idempotency key."""
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_service.py::test_execution_request_has_client_order_id -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/execution.py
git commit -m "feat: add auto-generated client_order_id for idempotent live orders"
```

---

### Task 5: Implement `LivePolymarketExecutor` with journal integration

**Files:**
- Modify: `src/pm_bot/execution.py`
- Test: `tests/test_live_client.py`

- [ ] **Step 1: Write the failing test**

```python
def test_live_executor_records_to_live_recorder_on_submit(tmp_path: Path):
    from pm_bot.live_recorder import LiveOrderRecorder
    from pm_bot.live_guards import evaluate_live_order_guards

    recorder_path = tmp_path / "live_orders.jsonl"
    live_recorder = LiveOrderRecorder(recorder_path)

    executor = LivePolymarketExecutor(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xabc123",
            signature_type=0,
            funder_address="0x0000000000000000000000000000000000000001",
            live_allow_market_ids=(),
            live_max_order_usd=100.0,
        ),
        live_recorder=live_recorder,
        # ... (real or mocked PolymarketLiveClient)
    )

    request = ExecutionRequest(
        market_id="btc-5m-1",
        token_id="token-up",
        side="UP",
        price=0.51,
        size_usd=10.0,
        order_type="market",
        client_order_id="test-idempotency-key",
    )

    result = executor.execute(request)

    # Verify order was recorded in journal BEFORE returning
    recorded = live_recorder.get_order("test-idempotency-key")
    assert recorded is not None, "Order must be journaled before executor returns"
    assert recorded.status == "submitted"
```

- [ ] **Step 2: Run test — expected to fail** (LivePolymarketExecutor doesn't accept `live_recorder` yet)

- [ ] **Step 3: Update `LivePolymarketExecutor.__init__` to accept `live_recorder`**

Modify `src/pm_bot/execution.py`:

```python
class LivePolymarketExecutor:
    def __init__(
        self,
        config: AppConfig,
        live_client: PolymarketLiveClient | None = None,
        live_recorder: LiveOrderRecorder | None = None,
    ) -> None:
        self._config = config
        self._client = live_client or self._build_client()
        self._live_recorder = live_recorder

    def _build_client(self) -> PolymarketLiveClient:
        return PolymarketLiveClient(
            host=self._config.polymarket_host,
            chain_id=self._config.polygon_chain_id,
            wallet_private_key=self._config.wallet_private_key or "",
            signature_type=self._config.signature_type,
            funder_address=self._config.funder_address or "",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        # Pre-submit: record to journal as "submitted"
        if self._live_recorder:
            self._live_recorder.record(LiveOrderRecord(
                client_order_id=request.client_order_id,
                order_id=None,
                market_id=request.market_id,
                side=request.side,
                price=request.price,
                size_usd=request.size_usd,
                submitted_at=datetime.now(timezone.utc),
                status="submitted",
            ))

        try:
            order_result = self._client.post_order(
                token_id=request.token_id,
                side=request.side,
                amount=request.size_usd,
                price=request.price,
            )
            normalized = self._normalize_response(order_result, request)

            # Update journal with order_id and status
            if self._live_recorder:
                self._live_recorder.update_status(
                    client_order_id=request.client_order_id,
                    status=normalized.status,
                    filled_at=datetime.now(timezone.utc) if normalized.status == "filled" else None,
                    fill_price=normalized.submitted_price,
                    error_message=normalized.message if normalized.status == "error" else None,
                )

            return normalized
        except Exception as exc:
            if self._live_recorder:
                self._live_recorder.update_status(
                    client_order_id=request.client_order_id,
                    status="error",
                    error_message=str(exc),
                )
            return ExecutionResult(
                action="live_trade",
                status="error",
                order_id=None,
                client_order_id=request.client_order_id,
                submitted_price=request.price,
                submitted_size=request.size_usd,
                message=str(exc),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_live_client.py -v
```
Expected: PASS (existing tests should still pass; add new test above)

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/execution.py tests/test_live_client.py
git commit -m "feat: wire LiveOrderRecorder into LivePolymarketExecutor for journal audit trail"
```

---

### Task 6: Add Live CLI commands (`live`, `live-loop`)

**Files:**
- Modify: `src/pm_bot/cli.py`

- [ ] **Step 1: Write the failing test (CLI argument validation)**

Add to `tests/test_recorder_and_cli.py`:

```python
def test_cli_live_command_rejects_missing_wallet():
    """live command must validate wallet config before running."""
    parser = build_parser()
    # Should raise error when wallet_private_key missing
    with pytest.raises(ValueError):
        args = parser.parse_args(["live", "--interval", "5m", "--balance", "1000"])
```

- [ ] **Step 2: Run test — expected to fail** (no `live` subcommand yet)

- [ ] **Step 3: Add `live` and `live-loop` subcommands to cli.py**

```python
# In build_parser(), add after the "loop" subparser:

live = subparsers.add_parser("live", help="Run one live decision cycle (requires wallet config)")
live.add_argument("--interval", choices=["5m", "15m"], required=True)
live.add_argument("--balance", type=float, default=1_000.0)
live.add_argument("--confirm-live", action="store_true",
    help="Acknowledge this will place real orders with real funds")

live_loop = subparsers.add_parser("live-loop", help="Repeat live decisions on a fixed interval")
live_loop.add_argument("--interval", choices=["5m", "15m"], required=True)
live_loop.add_argument("--balance", type=float, default=1_000.0)
live_loop.add_argument("--sleep-seconds", type=int, default=60)
live_loop.add_argument("--iterations", type=int, default=0)
live_loop.add_argument("--confirm-live", action="store_true")
```

Update `_build_service()` to accept `live: bool = False`:

```python
def _build_service(fixture: str | None, live: bool = False) -> TradingService:
    if not fixture:
        config = AppConfig.from_env()
        if live:
            # Validate required live config
            missing = []
            if not config.wallet_private_key:
                missing.append("WALLET_PRIVATE_KEY")
            if not config.funder_address:
                missing.append("FUNDER_ADDRESS")
            if missing:
                raise ValueError(f"Live trading requires: {', '.join(missing)}")
            from pm_bot.live_recorder import LiveOrderRecorder
            from pm_bot.polymarket_live_client import PolymarketLiveClient
            live_client = PolymarketLiveClient(...)
            live_recorder = LiveOrderRecorder(config.live_orders_path)
            executor = LivePolymarketExecutor(config, live_client=live_client, live_recorder=live_recorder)
            return TradingService(config=config, executor=executor)
        return TradingService(config=config)
    # fixture path unchanged...
```

Update `main()`:

```python
if args.command == "live":
    if not getattr(args, "confirm_live", False):
        raise ValueError("live trading requires --confirm-live flag")
    service = _build_service(getattr(args, "fixture", None), live=True)
    result = service.oneshot(interval=args.interval, balance=args.balance, live_confirmed=True)
    print(_json_dumps(asdict(result)))
    return 0

if args.command == "live-loop":
    if not getattr(args, "confirm_live", False):
        raise ValueError("live trading requires --confirm-live flag")
    service = _build_service(getattr(args, "fixture", None), live=True)
    iterations = 0
    while True:
        result = service.oneshot(interval=args.interval, balance=args.balance, live_confirmed=True)
        print(_json_dumps(asdict(result)))
        iterations += 1
        if args.iterations and iterations >= args.iterations:
            return 0
        time.sleep(args.sleep_seconds)
```

- [ ] **Step 4: Run existing tests to verify no regressions**

```bash
pytest tests/test_recorder_and_cli.py -v
```
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/cli.py
git commit -m "feat: add live and live-loop CLI commands with wallet validation"
```

---

### Task 7: Implement live reconciliation loop in TradingService

**Files:**
- Modify: `src/pm_bot/service.py`
- Create: `tests/test_live_service.py`

- [ ] **Step 1: Write the failing integration test**

```python
def test_live_service_reconciles_open_orders_on_startup(tmp_path: Path):
    """
    On service initialization, if there are submitted-but-unfilled orders
    in the journal, the service should query their status from the venue
    and update the journal accordingly before making new decisions.
    """
    from pm_bot.live_recorder import LiveOrderRecorder
    from pm_bot.live_service import LiveTradingService

    live_orders_path = tmp_path / "live_orders.jsonl"
    live_recorder = LiveOrderRecorder(live_orders_path)

    # Pre-populate with a stale "submitted" order
    stale_order = LiveOrderRecord(
        client_order_id="stale-123",
        order_id="venue-order-456",
        market_id="btc-5m-1",
        side="UP",
        price=0.51,
        size_usd=10.0,
        submitted_at=datetime.now(timezone.utc),
        status="submitted",
    )
    live_recorder.record(stale_order)

    # Mock live client that says the order was filled
    class MockLiveClient:
        def get_order(self, order_id: str):
            return {"orderID": order_id, "status": "FILLED", "fillPrice": 0.51}

    service = LiveTradingService(
        config=AppConfig(
            trading_mode="live",
            wallet_private_key="0xtest",
            signature_type=0,
            funder_address="0xtest",
            live_allow_market_ids=("btc-5m-1",),
            live_max_order_usd=100.0,
            live_orders_path=live_orders_path,
        ),
        live_client=MockLiveClient(),
        live_recorder=live_recorder,
    )

    # Reconciliation should have updated the stale order
    updated = live_recorder.get_order("stale-123")
    assert updated.status == "filled", f"Expected 'filled', got '{updated.status}'"
```

- [ ] **Step 2: Run test — expected to fail** (LiveTradingService doesn't exist)

- [ ] **Step 3: Create `src/pm_bot/live_service.py`**

```python
from __future__ import annotations
from datetime import datetime, timezone
from pm_bot.service import TradingService
from pm_bot.live_recorder import LiveOrderRecorder


class LiveTradingService(TradingService):
    """
    Live trading service subclass.
    On initialization, reconciles open orders from the journal
    against the venue before accepting new trading decisions.
    """

    def __init__(self, *args, live_client=None, live_recorder: LiveOrderRecorder | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._live_recorder = live_recorder
        self._live_client = live_client

    def _reconcile_open_orders(self) -> None:
        """Query venue for all submitted orders and update journal."""
        if not self._live_recorder or not self._live_client:
            return

        for order in self._live_recorder.get_open_orders():
            if not order.order_id:
                # Can't reconcile without a venue order ID
                continue
            try:
                venue_status = self._live_client.get_order(order.order_id)
                normalized_status = self._normalize_venue_status(venue_status)
                if normalized_status != order.status:
                    self._live_recorder.update_status(
                        client_order_id=order.client_order_id,
                        status=normalized_status,
                        filled_at=datetime.now(timezone.utc) if normalized_status == "filled" else None,
                    )
            except Exception:
                # Reconciliation failure is non-fatal; log and continue
                pass

    def _normalize_venue_status(self, venue_response: dict) -> str:
        """Map venue status strings to internal LiveOrderRecord.status values."""
        status = venue_response.get("status", "").upper()
        if status == "FILLED":
            return "filled"
        if status in ("CANCELLED", "VOID", "EXPIRED"):
            return "cancelled"
        if status == "SUBMITTED":
            return "submitted"
        return "error"

    def oneshot(self, *args, **kwargs):
        # Reconcile before each decision cycle
        self._reconcile_open_orders()
        return super().oneshot(*args, **kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_live_service.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/live_service.py tests/test_live_service.py
git commit -m "feat: add LiveTradingService with open-order reconciliation on each cycle"
```

---

## Phase 3: Risk Hardening

### Impact: Risk controls become meaningful for unattended operation.

---

### Task 8: Fix 5-loss lockout — add time-based reset

**Files:**
- Modify: `src/pm_bot/risk.py`
- Test: `tests/test_filters_and_risk.py`

- [ ] **Step 1: Write the failing test**

```python
def test_five_loss_lockout_resets_after_cooldown_period(tmp_path: Path):
    """After 60 minutes (configurable) of no new losses, 5-loss lockout should auto-reset."""
    config = AppConfig(
        cooldown_after_three_losses_minutes=30,
        five_loss_lockout_cooldown_minutes=60,  # NEW CONFIG
        paper_trades_path=tmp_path / "trades.jsonl",
    )
    rm = RiskManager(config=config)

    # Simulate 5 losses with timestamps spaced 10 minutes apart
    now = datetime.now(timezone.utc)
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for i in range(5):
        trade = PaperTradeRecord(
            timestamp=base,
            market_id=f"m{i}",
            interval="5m",
            side="UP",
            price=0.5,
            stake=10.0,
            signal={"should_trade": True, "side": "UP", "signal_name": "test", "confidence": 0.7, "reasons": []},
            expires_at=base,
            reference_price=100_000.0,
            pnl=-10.0,
            closed_at=base,
        )
        rm.closed_trades.append(trade)
        base = base.replace(minute=base.minute + 10)

    allowed, reasons = rm.allow_trade(balance=1000.0, now=base)
    assert not allowed
    assert "five_loss_lockout" in reasons

    # After 60 minutes with no new losses, should allow again
    later = base.replace(minute=base.minute + 60)
    allowed, reasons = rm.allow_trade(balance=1000.0, now=later)
    assert allowed, f"Lockout should auto-reset after cooldown. Reasons: {reasons}"
```

- [ ] **Step 2: Run test — expected to fail** (no time-based reset yet)

- [ ] **Step 3: Add new config fields to config.py**

```python
five_loss_lockout_cooldown_minutes: int = 60

# In from_env():
five_loss_lockout_cooldown_minutes=_env_int(
    "FIVE_LOSS_LOCKOUT_COOLDOWN_MINUTES",
    defaults.five_loss_lockout_cooldown_minutes,
),
```

- [ ] **Step 4: Update risk.py to implement time-based reset**

```python
# In RiskManager.allow_trade():
if loss_streak >= 5:
    # Check if we've been in lockout long enough to auto-reset
    last_loss_time = ...  # already tracked
    if config.five_loss_lockout_cooldown_minutes > 0:
        if now >= last_loss_time + timedelta(minutes=config.five_loss_lockout_cooldown_minutes):
            # Auto-reset: clear the lockout
            pass  # proceed to allow
        else:
            reasons.append("five_loss_lockout")
    else:
        reasons.append("five_loss_lockout")
```

Also fix the cooldown timer to start from the **first** loss in the streak, not the last:

```python
# Track first_loss_time when building the streak
loss_streak = 0
first_loss_time: datetime | None = None
last_loss_time: datetime | None = None
for trade in reversed(self.closed_trades):
    if trade.pnl < 0:
        if loss_streak == 0:
            last_loss_time = trade.closed_at
        loss_streak += 1
        if first_loss_time is None:
            first_loss_time = trade.closed_at
    else:
        break

# Use first_loss_time for cooldown calculation (not last_loss_time)
if loss_streak >= 3:
    cooldown_start = first_loss_time  # FIXED: was last_loss_time
    if now < cooldown_start + timedelta(minutes=self.config.cooldown_after_three_losses_minutes):
        reasons.append("cooldown_after_three_losses")
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_filters_and_risk.py::test_five_loss_lockout_resets_after_cooldown_period -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pm_bot/risk.py src/pm_bot/config.py tests/test_filters_and_risk.py
git commit -m "fix: add time-based auto-reset to 5-loss lockout; fix cooldown timer anchor"
```

---

### Task 9: Cap stake to `live_max_order_usd`; add per-market max position

**Files:**
- Modify: `src/pm_bot/risk.py`
- Test: `tests/test_filters_and_risk.py`

- [ ] **Step 1: Write the failing test**

```python
def test_position_size_capped_by_live_max_order(tmp_path: Path):
    """When in live mode, stake must not exceed live_max_order_usd."""
    config = AppConfig(
        base_risk_pct=0.04,  # would give $40 on $1000 balance
        live_max_order_usd=10.0,
        paper_trades_path=tmp_path / "trades.jsonl",
    )
    rm = RiskManager(config=config)

    decision = SignalDecision(
        should_trade=True,
        side="UP",
        signal_name="momentum",  # base_risk_pct (not strong)
        confidence=0.7,
        reasons=["test"],
    )

    # In a live-configured service, size should be capped
    # We expose this via a new parameter
    size = rm.position_size(
        balance=1_000.0,
        decision=decision,
        live_max_order_usd=10.0,  # passed from config
    )

    assert size <= 10.0, f"Stake {size} exceeds live_max_order_usd 10.0"


def test_risk_manager_tracks_per_market_exposure(tmp_path: Path):
    """Opening a 2nd position in the same market_id should be blocked."""
    config = AppConfig(paper_trades_path=tmp_path / "trades.jsonl")
    rm = RiskManager(config=config)

    # Add an open (unsettled) position for btc-5m-1
    open_trade = PaperTradeRecord(
        timestamp=datetime.now(timezone.utc),
        market_id="btc-5m-1",
        interval="5m",
        side="UP",
        price=0.51,
        stake=20.0,
        signal={"should_trade": True, "side": "UP", "signal_name": "test", "confidence": 0.7, "reasons": []},
        expires_at=datetime.now(timezone.utc),
        reference_price=100_000.0,
        # Note: no settled_at/pnl — this is an OPEN position
    )
    rm.closed_trades.append(open_trade)  # Using closed_trades as all trades for now

    # New decision for the same market should be blocked
    # (Implementation detail: add a method to check open market exposure)
    decision = SignalDecision(
        should_trade=True,
        side="DOWN",
        signal_name="momentum",
        confidence=0.7,
        reasons=["test"],
    )

    allowed, reasons = rm.allow_trade(balance=1_000.0, now=datetime.now(timezone.utc))
    # Should mention market already has open position
```

- [ ] **Step 2: Run test — expected to fail** (cap and per-market tracking not implemented)

- [ ] **Step 3: Update `position_size()` to accept and enforce `live_max_order_usd`**

In `src/pm_bot/risk.py`:

```python
def position_size(
    self,
    balance: float,
    decision: SignalDecision,
    live_max_order_usd: float | None = None,
) -> float:
    if decision.signal_name == "oracle_delay":
        risk_pct = self.config.strong_risk_pct
    else:
        risk_pct = self.config.base_risk_pct

    raw_size = round(balance * risk_pct, 2)

    # Enforce live order size cap if provided
    if live_max_order_usd is not None and live_max_order_usd > 0:
        return min(raw_size, live_max_order_usd)

    return raw_size
```

Update the call site in `service.py`:

```python
stake = risk_manager.position_size(
    balance=balance,
    decision=decision,
    live_max_order_usd=config.live_max_order_usd if config.trading_mode == "live" else None,
)
```

- [ ] **Step 4: Add per-market open position tracking**

In `src/pm_bot/risk.py`, add a method:

```python
def open_market_ids(self) -> set[str]:
    """Return market_ids that have unresolved (open) positions."""
    return {
        t.market_id for t in self.closed_trades
        if getattr(t, "settled_at", None) is None
    }

def allow_trade(self, balance: float, now: datetime, decision: SignalDecision | None = None) -> tuple[bool, list[str]]:
    # ... existing checks ...

    # NEW: per-market exposure check
    if decision:
        open_markets = self.open_market_ids()
        # Note: This requires market_id in SignalDecision or passed separately
        # For now, skip — requires passing market_id into allow_trade
```

**Deferred**: Per-market tracking requires threading `market_id` through the call chain. This is a larger change — add a separate tracking issue and implement as a follow-up task. Mark the test as deferred.

- [ ] **Step 5: Run tests to verify they pass (position_size cap only)**

```bash
pytest tests/test_filters_and_risk.py::test_position_size_capped_by_live_max_order -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pm_bot/risk.py src/pm_bot/service.py
git commit -m "fix: cap stake to live_max_order_usd in live mode"
```

---

## Phase 4: Production Hardening

### Impact: Production reliability, observability, and numerical robustness.

---

### Task 10: Replace float money with Decimal

**Files:**
- Modify: `src/pm_bot/models.py`, `src/pm_bot/risk.py`, `src/pm_bot/recorder.py`, `src/pm_bot/execution.py`
- Test: All test files (update assertions to use `Decimal`)

This is a large migration. Recommended approach: add a `MONEY = Decimal` type alias in `models.py`, then migrate one module at a time.

```python
# In src/pm_bot/models.py:
from decimal import Decimal

# Replace float fields in PaperTradeRecord, LiveOrderRecord, ExecutionRequest, etc.
stake: Decimal = Decimal("0")
price: Decimal = Decimal("0")
pnl: Decimal | None = None
```

Each field change cascades through: models → risk (position calc, pnl sum) → recorder (settlement math) → execution (order sizing).

**Test strategy**: Run full test suite after each module migration. Keep commits small (one module per commit).

- [ ] **Step 1: Add Decimal to models.py Money type alias**

```python
from decimal import Decimal
Money: TypeAlias = Decimal
```

- [ ] **Step 2: Migrate `PaperTradeRecord` fields**
- [ ] **Step 3: Migrate `RiskManager` calculations**
- [ ] **Step 4: Migrate `Recorder` settlement math**
- [ ] **Step 5: Migrate `ExecutionRequest` fields**
- [ ] **Step 6: Run full test suite, fix failures**

---

### Task 11: Add structured error handling and retry with exponential backoff

**Files:**
- Modify: `src/pm_bot/clients.py` (Binance/Polymarket HTTP calls)
- Modify: `src/pm_bot/execution.py` (LiveExecutor post_order)

- [ ] **Step 1: Add a retry decorator utility**

```python
# src/pm_bot/retry.py
from functools import wraps
import time
import logging

logger = logging.getLogger(__name__)

def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (OSError,),
):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logger.warning(
                        f"{fn.__name__} failed (attempt {attempt+1}/{max_retries+1}): {exc}. "
                        f"Retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
```

- [ ] **Step 2: Apply to Binance and Polymarket API calls in clients.py**

Apply `@retry_with_backoff(exceptions=(OSError, HTTPError), max_retries=3)` to:
- `BinanceMarketDataClient.latest_price()`
- `BinanceMarketDataClient.klines()`
- `BinanceMarketDataClient.price_at()`
- `PolymarketMarketClient._get()` and `active_markets()`

- [ ] **Step 3: Apply to LiveExecutor.execute() post_order**

Wrap `self._client.post_order(...)` call in `execute()` with `@retry_with_backoff`.

- [ ] **Step 4: Run tests**

```bash
pytest tests/ -v --tb=short
```
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/retry.py src/pm_bot/clients.py src/pm_bot/execution.py
git commit -m "feat: add retry-with-backoff for external API calls"
```

---

### Task 12: Add basic metrics / structured logging

**Files:**
- Create: `src/pm_bot/metrics.py`
- Modify: `src/pm_bot/service.py`

- [ ] **Step 1: Define a minimal metrics interface**

```python
# src/pm_bot/metrics.py
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Outcome(Enum):
    PAPER_TRADE = "paper_trade"
    LIVE_TRADE = "live_trade"
    SKIP = "skip"
    ERROR = "error"


@dataclass
class CycleMetrics:
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    interval: str = ""
    outcome: Outcome = Outcome.SKIP
    signal_name: str | None = None
    side: str | None = None
    stake_usd: float | None = None
    market_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    duration_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "interval": self.interval,
            "outcome": self.outcome.value,
            "signal": self.signal_name,
            "side": self.side,
            "stake_usd": str(self.stake_usd) if self.stake_usd is not None else None,
            "market_id": self.market_id,
            "reasons": self.reasons,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }
```

- [ ] **Step 2: Emit metrics in `TradingService.oneshot()`**

In `src/pm_bot/service.py`, wrap the `oneshot()` body in timing and emit at the end:

```python
import time
from pm_bot.metrics import CycleMetrics, Outcome

def oneshot(self, interval: str, balance: float, live_confirmed: bool = False) -> OneShotResult:
    t0 = time.monotonic()
    try:
        # ... existing logic ...
    except Exception as exc:
        duration_ms = (time.monotonic() - t0) * 1000
        metrics = CycleMetrics(
            interval=interval,
            outcome=Outcome.ERROR,
            error=str(exc),
            duration_ms=duration_ms,
        )
        self._emit_metrics(metrics)
        raise
    else:
        duration_ms = (time.monotonic() - t0) * 1000
        outcome_map = {
            "paper_trade": Outcome.PAPER_TRADE,
            "live_trade": Outcome.LIVE_TRADE,
            "skip": Outcome.SKIP,
            "error": Outcome.ERROR,
        }
        metrics = CycleMetrics(
            interval=interval,
            outcome=outcome_map.get(result.action, Outcome.SKIP),
            signal_name=getattr(result, "signal_name", None),
            side=getattr(result, "side", None),
            stake_usd=getattr(result, "stake", None),
            market_id=getattr(result, "market_id", None),
            reasons=result.reasons,
            duration_ms=duration_ms,
        )
        self._emit_metrics(metrics)
        return result

def _emit_metrics(self, metrics: CycleMetrics) -> None:
    # Default: log as JSON line. Replace with DataDog/Prometheus/etc. by subclassing.
    import json
    import logging
    logging.getLogger("pm_bot.metrics").info(json.dumps(metrics.to_dict()))
```

- [ ] **Step 3: Write test for metrics emission**

```python
def test_oneshot_emits_metrics_on_success(tmp_path: Path, caplog):
    service = make_service(tmp_path / "paper_trades.jsonl")
    with caplog.at_level("INFO", logger="pm_bot.metrics"):
        service.oneshot(interval="5m", balance=1_000.0)
    assert any("paper_trade" in r.message for r in caplog.records)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_service.py -v -k metrics
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pm_bot/metrics.py src/pm_bot/service.py
git commit -m "feat: add CycleMetrics emission with duration tracking"
```

---

## Implementation Order & Dependencies

```
Phase 1 (Paper Correctness)
├── Task 1: Fix settlement equality (independent)
└── Task 2: Add Chainlink simulation note (independent)

Phase 2 (Live Operational Layer) — requires Phase 1
├── Task 3: LiveRecorder (independent)
├── Task 4: client_order_id in ExecutionRequest (independent)
├── Task 5: Wire LiveRecorder into LiveExecutor (requires 3, 4)
├── Task 6: Live CLI commands (requires 5)
└── Task 7: LiveTradingService + reconciliation (requires 3, 5)

Phase 3 (Risk Hardening) — independent of Phase 2
├── Task 8: 5-loss lockout reset (independent)
└── Task 9: Stake cap (independent)

Phase 4 (Production Hardening) — independent
├── Task 10: Decimal migration (requires significant testing)
├── Task 11: Retry + backoff (independent)
└── Task 12: Metrics (independent)
```

---

## Verification Commands

Run after each task:

```bash
# All tests
pytest tests/ -v --tb=short

# Specific test file
pytest tests/test_service.py -v --tb=short
pytest tests/test_recorder_and_cli.py -v --tb=short
pytest tests/test_filters_and_risk.py -v --tb=short

# Full integration (after Phase 2)
pytest tests/ -v --tb=short -k "live"
```

---

**Plan complete.** Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints

Which approach?
