# Live Trading Enablement Plan

> **For Hermes:** Use Codex to implement this plan task-by-task under TDD. Do **not** place real orders during development. All verification must use mocks/fixtures/stub executors until the final canary step.

**Goal:** Upgrade `pm-bot` from paper-trading-only to a safely gated Polymarket live trading bot that can sign and submit real orders on the CLOB.

**Architecture:** Keep the current read-only signal/risk pipeline. Add a separate execution layer behind a narrow interface so `TradingService` decides *what* to trade and an executor decides *how* to submit/cancel/manage real orders. Live mode must be opt-in, double-gated, and isolated from the existing paper path.

**Tech Stack:** Python 3.11, existing `pm_bot` package, Polymarket CLOB SDK (`py-clob-client-v2` per Polymarket docs), Polygon/Polymarket wallet auth, JSONL or dedicated live ledger for order audit.

---

## What is missing today

Current codebase is **not able to live trade yet** because:

1. `README.md` explicitly states there is **no live order placement**.
2. `pyproject.toml` has **zero dependencies**, so there is no Polymarket trading SDK, no wallet signing, no authenticated client.
3. `src/pm_bot/clients.py` is **read-only only**: Binance / Gamma / Chainlink reads; no CLOB auth, no order submission.
4. `src/pm_bot/config.py` has **no live credentials or safety config**.
5. `src/pm_bot/service.py` only returns `paper_trade` / `skip`; it never creates, posts, reconciles, or cancels orders.
6. `src/pm_bot/cli.py` has no explicit live subcommand / confirmation gate.

## External prerequisites from Polymarket docs

From Polymarket trading docs:
- Live trading uses the **CLOB**.
- Orders require **EIP-712 signing**.
- Trading requests use derived **L2 API credentials**.
- Recommended Python SDK: `py-clob-client-v2`.
- Need the correct wallet type:
  - `0 = EOA`
  - `1 = POLY_PROXY`
  - `2 = GNOSIS_SAFE`
- Need the correct **funder address**.
- Need **pUSD** to buy and **POL** for gas when using EOA type `0`.

---

## My recommendation

**Do not bolt `--live` directly onto current `oneshot`.** Too easy to misfire.

Use this rollout instead:
1. keep current `oneshot` = read-only/paper only
2. add a separate executor abstraction
3. add a separate live command with explicit confirmation
4. ship canary mode first (very small max notional, single-market allowlist)

That is the safe version.

---

## Implementation Tasks

### Task 1: Add live-trading config surface

**Objective:** Introduce explicit env/config values for live mode without changing runtime behavior yet.

**Files:**
- Modify: `src/pm_bot/config.py`
- Modify: `config.example.json`
- Modify: `README.md`
- Test: `tests/test_recorder_and_cli.py`

**Add config fields:**
- `trading_mode: str = "paper"`
- `polymarket_host: str = "https://clob.polymarket.com"`
- `polygon_chain_id: int = 137`
- `wallet_private_key: str | None = None`
- `signature_type: int | None = None`
- `funder_address: str | None = None`
- `live_max_order_usd: float = 10.0`
- `live_allow_market_ids: tuple[str, ...] = ()`
- `live_require_explicit_confirm: bool = True`
- `live_orders_path: Path = Path("data/live_orders.jsonl")`

**Acceptance:**
- default mode remains `paper`
- missing live credentials do not affect paper mode
- config docs clearly distinguish paper vs live

### Task 2: Create execution abstraction

**Objective:** Separate decision logic from execution logic.

**Files:**
- Create: `src/pm_bot/execution.py`
- Modify: `src/pm_bot/service.py`
- Modify: `src/pm_bot/models.py`
- Test: `tests/test_service.py`

**Add interfaces/classes:**
- `ExecutionRequest` (market_id, token_id, side, price, size_usd, order_type, metadata)
- `ExecutionResult` (action, status, order_id, client_order_id, submitted_price, submitted_size, message)
- `PaperExecutor`
- `LivePolymarketExecutor` (stub for now)

**Rule:**
`TradingService` should first produce a normalized execution request; only then hand it to an executor.

**Acceptance:**
- service still passes all current paper tests
- paper executor preserves existing behavior
- live executor can be injected and mocked in tests

### Task 3: Add token/market metadata needed for real orders

**Objective:** Carry enough market metadata to submit a real CLOB order.

**Files:**
- Modify: `src/pm_bot/models.py`
- Modify: `src/pm_bot/clients.py`
- Test: `tests/test_clients.py`

**Need to persist in `MarketSnapshot`:**
- `token_id_up`
- `token_id_down`
- `tick_size`
- `neg_risk`

**Implementation notes:**
- Parse `clobTokenIds` from Gamma response.
- Parse `tickSize` / `negRisk` if available; otherwise fail closed for live mode.
- Validate that UP/DOWN token IDs exist before any live execution path.

**Acceptance:**
- market discovery returns token IDs when present
- malformed or missing live-order metadata raises clean JSON errors

### Task 4: Add live order validator (hard safety gate)

**Objective:** Refuse unsafe live orders before any network write.

**Files:**
- Create: `src/pm_bot/live_guards.py`
- Modify: `src/pm_bot/service.py`
- Test: `tests/test_service.py`

**Checks:**
- mode must be `live`
- explicit CLI confirmation flag must be present
- market id must be allowlisted unless allowlist is empty by deliberate config
- submitted notional must be `<= live_max_order_usd`
- side price must still satisfy current risk constraints
- market must not be too close to expiry
- token ID must exist
- wallet config must be complete

**Acceptance:**
- all failures return deterministic machine-readable reasons
- no partial live requests leak through

### Task 5: Integrate Polymarket live client

**Objective:** Build authenticated live execution client using the official Python SDK.

**Files:**
- Modify: `pyproject.toml`
- Create: `src/pm_bot/polymarket_live_client.py`
- Test: `tests/test_live_client.py`

**Dependency:**
- add `py-clob-client-v2`

**Client responsibilities:**
- initialize SDK with host / chain / signer
- derive or create API creds
- create signed order payload
- submit order
- normalize response into `ExecutionResult`

**Important:**
Do not hardcode wallet assumptions. Signature type and funder address must come from config.

**Acceptance:**
- tests mock SDK calls
- no real network call in CI/tests
- initialization fails closed on incomplete credentials

### Task 6: Add live order journal and reconciliation hooks

**Objective:** Persist every live submission attempt and exchange response for audit.

**Files:**
- Create: `src/pm_bot/live_recorder.py`
- Modify: `src/pm_bot/service.py`
- Modify: `README.md`
- Test: `tests/test_live_recorder.py`

**Record per order:**
- timestamp
- market_id
- token_id
- side
- intended price/size
- order_id
- client_order_id
- exchange status
- raw error/message

**Acceptance:**
- every live submission is journaled
- rejected orders are also journaled
- paper ledger and live ledger stay separate

### Task 7: Add explicit live CLI entrypoint

**Objective:** Expose live execution behind a separate, harder-to-misuse command.

**Files:**
- Modify: `src/pm_bot/cli.py`
- Modify: `README.md`
- Test: `tests/test_recorder_and_cli.py`

**Preferred command shape:**
- `pm-bot trade-live --interval 5m --confirm-live YES`
- optional: `--max-order-usd 5`
- optional: `--market-id <id>` for canary targeting

**Do not** overload existing `oneshot` unless you want future accidents.

**Acceptance:**
- command refuses to run without explicit confirmation
- confirmation string must be exact
- command prints structured JSON like paper mode

### Task 8: Add canary mode before full live mode

**Objective:** Make the first live deployment tiny and reversible.

**Files:**
- Modify: `src/pm_bot/config.py`
- Modify: `src/pm_bot/live_guards.py`
- Modify: `README.md`
- Test: `tests/test_service.py`

**Canary defaults:**
- one market only
- max order size `<= $5` or `$10`
- one interval only (`5m` first)
- no auto-loop until multiple manual successes

**Acceptance:**
- canary config is stronger than normal live config
- canary violations fail closed

### Task 9: Add post-trade reconciliation before unattended loop

**Objective:** Don’t trust submit success; verify order/trade state from Polymarket after placement.

**Files:**
- Modify: `src/pm_bot/polymarket_live_client.py`
- Modify: `src/pm_bot/service.py`
- Test: `tests/test_live_client.py`

**Need:**
- fetch open orders
- fetch fills/trades
- fetch order status by id if available
- map partial fill / open / rejected / cancelled

**Acceptance:**
- returned JSON clearly distinguishes `submitted` vs `filled` vs `open`
- bot does not assume a submitted order is filled

### Task 10: Only then add unattended live loop

**Objective:** Permit automated looping only after manual live oneshots are proven.

**Files:**
- Modify: `src/pm_bot/cli.py`
- Modify: `src/pm_bot/service.py`
- Test: `tests/test_recorder_and_cli.py`

**Guardrails:**
- require `trade-live-loop` or equivalent separate command
- require canary mode off or explicit override
- require reconciliation on each cycle
- require cooldown / open-order checks before posting another order

**Acceptance:**
- no duplicate order spam
- no new order if unresolved live order already exists for same market/window

---

## Operational checklist before first real order

1. Prepare a dedicated wallet for this bot.
2. Confirm wallet type: EOA / POLY_PROXY / GNOSIS_SAFE.
3. Confirm correct `funder_address`.
4. Fund with:
   - `pUSD` for purchases
   - `POL` for gas if using EOA
5. Export private key only into controlled env, not source files.
6. Enable canary config: one market, tiny order size.
7. Run mocked tests.
8. Run paper mode one more time on the exact target market.
9. Run one **manual** live order.
10. Verify on Polymarket UI / API that order status matches the bot journal.
11. Only after repeated success, enable live loop.

---

## What “开启真实交易” means in practice

For this repo, it is **not** a single switch today.

It requires adding:
- authenticated Polymarket CLOB client
- wallet/key config
- live execution abstraction
- explicit CLI gate
- canary safeguards
- order journal + reconciliation

Until those exist, the bot should stay paper-only.

---

## Suggested next build step

**Best next step:** implement **Tasks 1–4 only** first.

That gives us:
- live config surface
- executor abstraction
- token metadata
- hard safety gates

But still **no real orders yet**.

That is the right checkpoint before wiring in the SDK.
