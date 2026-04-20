from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class PriceTick:
    price: float
    volume: float = 0.0


@dataclass(slots=True)
class Candle:
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class OrderBookSide:
    price: float


@dataclass(slots=True)
class MarketSnapshot:
    market_id: str
    slug: str
    interval: str
    active: bool
    closed: bool
    seconds_to_expiry: int
    liquidity: float
    spread: float
    up: OrderBookSide
    down: OrderBookSide
    reference_price: float | None = None
    end_date: str | None = None
    token_id_up: str | None = None
    token_id_down: str | None = None
    tick_size: float | None = None
    neg_risk: bool | None = None

    def midpoint_distance(self) -> float:
        return min(abs(self.up.price - 0.5), abs(self.down.price - 0.5))


@dataclass(slots=True)
class SignalDecision:
    should_trade: bool
    side: str | None
    signal_name: str | None
    confidence: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TradeRecommendation:
    allowed: bool
    reasons: list[str]
    stake: float


@dataclass(slots=True)
class PaperTradeRecord:
    """Persisted paper-trade ledger row.

    Paper trades still come from the normal signal pipeline, which is driven by
    Binance market data. The stored ``reference_price`` is the Chainlink-style
    resolution threshold captured at entry, and a later settlement pass compares
    that fixed reference against a surrogate BTC ``settlement_price`` (normally
    sampled from Binance at expiry) to simulate how the market would resolve.
    When ``settlement_price == reference_price``, paper settlement resolves to
    ``UP``.
    """

    timestamp: str
    market_id: str
    interval: str
    side: str
    price: float
    stake: float
    signal: SignalDecision
    expires_at: str | None = None
    reference_price: float | None = None
    notes: list[str] = field(default_factory=list)
    settled_at: str | None = None
    settlement_price: float | None = None
    outcome: str | None = None
    pnl: float | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["signal"] = asdict(self.signal)
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class LiveOrderRecord:
    """Persisted live-order journal row."""

    timestamp: str
    market_id: str
    token_id: str | None
    side: str
    submitted_price: float
    submitted_size: float
    status: str
    submission_id: str
    order_hash: str | None = None
    order_id: str | None = None
    message: str | None = None
    signed_order_payload: dict[str, Any] | None = None
    signed_order_fingerprint: str | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}
