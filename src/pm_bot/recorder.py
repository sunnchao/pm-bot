from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from collections.abc import Callable
from datetime import datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

from pm_bot.models import PaperTradeRecord


class PaperTradeRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, trade: PaperTradeRecord) -> None:
        with self._locked_ledger():
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(trade.to_dict(), allow_nan=False) + "\n")

    def settled_trades(self) -> list[dict]:
        with self._locked_ledger():
            if not self.path.exists():
                return []

            trades: list[dict] = []
            for raw_line in self.path.read_text(encoding="utf-8").splitlines():
                payload = _load_payload(raw_line)
                if payload is None:
                    continue

                closed_at = _closed_at_for_risk(payload)
                pnl = _parse_float(payload.get("pnl"))
                if closed_at is None or pnl is None:
                    continue

                trades.append(
                    {
                        "market_id": str(payload.get("market_id", "")),
                        "pnl": pnl,
                        "closed_at": closed_at,
                    }
                )

            return trades

    def settle_due(
        self,
        current_btc_price: float,
        now: datetime,
        settlement_price_at: Callable[[datetime], float | None] | None = None,
    ) -> list[dict]:
        with self._locked_ledger():
            if not self.path.exists():
                return []

            settlements: list[dict] = []
            output_lines: list[str] = []
            for raw_line in self.path.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                payload = _load_payload(raw_line)
                if payload is None:
                    output_lines.append(raw_line)
                    continue

                settlement = _settle_payload(
                    payload=payload,
                    current_btc_price=current_btc_price,
                    now=now,
                    settlement_price_at=settlement_price_at,
                )
                if settlement is not None:
                    updated_payload = dict(payload)
                    updated_payload.update(
                        {
                            "settled_at": now.isoformat(),
                            "settlement_price": settlement["settlement_price"],
                            "outcome": settlement["outcome"],
                            "pnl": settlement["pnl"],
                        }
                    )
                    serialized = _dump_payload(updated_payload)
                    if serialized is not None:
                        output_lines.append(serialized)
                        settlements.append(
                            {
                                "market_id": str(payload.get("market_id", "")),
                                "outcome": settlement["outcome"],
                                "pnl": settlement["pnl"],
                                "closed_at": settlement["closed_at"],
                            }
                        )
                        continue
                output_lines.append(raw_line)

            temp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            with temp_path.open("w", encoding="utf-8") as handle:
                for line in output_lines:
                    handle.write(line + "\n")
            temp_path.replace(self.path)

            return settlements

    @contextmanager
    def _locked_ledger(self):
        with self.lock_path.open("a", encoding="utf-8") as lock_handle:
            flock(lock_handle.fileno(), LOCK_EX)
            try:
                yield
            finally:
                flock(lock_handle.fileno(), LOCK_UN)


def _settle_payload(
    payload: dict,
    current_btc_price: float,
    now: datetime,
    settlement_price_at: Callable[[datetime], float | None] | None = None,
) -> dict | None:
    if payload.get("settled_at"):
        return None

    expires_at = _parse_iso_datetime(payload.get("expires_at"))
    reference_price = _parse_float(payload.get("reference_price"))
    entry_price = _parse_float(payload.get("price"))
    stake = _parse_float(payload.get("stake"))
    side = payload.get("side")

    if expires_at is None or reference_price is None or entry_price is None or stake is None:
        return None
    if side not in {"UP", "DOWN"}:
        return None
    if now < expires_at:
        return None
    if stake < 0 or not 0 < entry_price < 1:
        return None

    settlement_price = current_btc_price
    if settlement_price_at is not None:
        settlement_price = settlement_price_at(expires_at)
        if settlement_price is None:
            return None

    if settlement_price == reference_price:
        return {
            "outcome": "void",
            "pnl": 0.0,
            "settlement_price": settlement_price,
            "closed_at": expires_at,
        }

    winning_side = "UP" if settlement_price > reference_price else "DOWN"
    if side == winning_side:
        pnl = round((stake / entry_price) - stake, 2)
        return {"outcome": "win", "pnl": pnl, "settlement_price": settlement_price, "closed_at": expires_at}
    return {
        "outcome": "loss",
        "pnl": round(-stake, 2),
        "settlement_price": settlement_price,
        "closed_at": expires_at,
    }


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _parse_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _load_payload(raw_line: str) -> dict | None:
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _dump_payload(payload: dict) -> str | None:
    try:
        return json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError):
        return None


def _closed_at_for_risk(payload: dict) -> datetime | None:
    return _parse_iso_datetime(payload.get("expires_at")) or _parse_iso_datetime(payload.get("settled_at"))
