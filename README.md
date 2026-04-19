# pm-bot

Minimal Python MVP for BTC 5m/15m Polymarket paper trading. Primary behavior is `oneshot`; `loop` is a thin wrapper that repeats the same decision cycle.

## Assumptions

- Binance is the fast price source used for momentum and oracle-delay heuristics.
- Polymarket market discovery is read-only and expects Gamma-style market payloads.
- Chainlink reference access is read-only and, in this MVP, defaults to the market payload's `referencePrice` when present.
- Paper trading records recommendations plus expiry/reference metadata, and later `oneshot`/`loop` runs settle expired paper trades into realized PnL before new risk checks.
- Live-trading config fields now exist for future work, but live execution is still gated and not implemented. Current runtime behavior remains paper-only.

## Package layout

- `src/pm_bot/config.py`: minimal config defaults
- `src/pm_bot/clients.py`: read-only Binance, Polymarket, Chainlink clients
- `src/pm_bot/signals.py`: oracle-delay, momentum, mean-reversion heuristics
- `src/pm_bot/filters.py`: hard no-trade filters
- `src/pm_bot/risk.py`: position sizing and circuit breakers
- `src/pm_bot/recorder.py`: JSONL paper-trade recorder and settlement pass
- `src/pm_bot/service.py`: one decision cycle
- `src/pm_bot/cli.py`: `discover`, `oneshot`, and thin `loop`

## CLI

Install once in editable mode:

```bash
python -m pip install -e .
```

Then run:

```bash
pm-bot discover
pm-bot discover --keyword btc --keyword bitcoin --limit 10
pm-bot oneshot --interval 5m
pm-bot oneshot --interval 15m --balance 1500
pm-bot loop --interval 5m --sleep-seconds 60
pm-bot loop --interval 15m --sleep-seconds 300 --iterations 3
```

For quick local execution without installation:

```bash
PYTHONPATH=src python -m pm_bot.cli discover --limit 10
PYTHONPATH=src python -m pm_bot.cli oneshot --interval 5m
PYTHONPATH=src python -m pm_bot.cli oneshot --interval 5m --fixture fixtures/btc-5m-paper-trade.json
```

`discover` prints a JSON array of BTC-relevant active markets with fields for `slug`, `question`, `end_date`, `seconds_to_expiry`, `liquidity`, `active`, `closed`, and available `yes`/`no` or `up`/`down` prices.

`oneshot` and `loop` accept `--fixture <path>` to run the full filter/signal/risk/recorder pipeline offline from a deterministic local snapshot. A sample fixture is included at `fixtures/btc-5m-paper-trade.json`.

The CLI prints one JSON result per run/iteration with the selected action, reason set, signal metadata, and suggested stake.

Paper trade records now persist `expires_at` and `reference_price`. When a later `oneshot` or `loop` run sees an expired unresolved record, it settles it from the current BTC price, writes `settled_at`/`settlement_price`/`outcome`/`pnl`, and feeds that realized PnL into the existing risk manager before evaluating a new trade.

## Config modes

`AppConfig.trading_mode` defaults to `paper`, and that remains the only runtime mode today.

- Paper mode: no wallet credentials are required, and `oneshot`/`loop` continue to write only the paper ledger at `paper_trades_path`.
- Future live mode: the config surface includes `polymarket_host`, `polygon_chain_id`, `wallet_private_key`, `signature_type`, `funder_address`, `live_max_order_usd`, `live_allow_market_ids`, `live_require_explicit_confirm`, and `live_orders_path` so the live executor can be wired in later.

Those live fields are documentation and config plumbing only in the current codebase. Setting them does not place orders, and there is still no live CLI entrypoint in this round.

## Testing

```bash
pytest -q
```
