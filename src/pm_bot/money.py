from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

_USD_CENTS = Decimal("0.01")


def to_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def parse_decimal(value: object) -> Decimal | None:
    try:
        parsed = to_decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def quantize_usd(value: Decimal) -> Decimal:
    return value.quantize(_USD_CENTS, rounding=ROUND_HALF_EVEN)


def to_float(value: Decimal) -> float:
    return float(value)
