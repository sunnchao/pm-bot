from __future__ import annotations

from dataclasses import asdict, dataclass, field


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
    timestamp: str
    market_id: str
    interval: str
    side: str
    price: float
    stake: float
    signal: SignalDecision
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["signal"] = asdict(self.signal)
        return payload
