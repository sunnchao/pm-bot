from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from pm_bot.models import Candle, MarketSnapshot, OrderBookSide, PriceTick


def _get_json(url: str) -> object:
    request = Request(
        url,
        headers={
            "User-Agent": "pm-bot/0.1 (+https://github.com/sunnchao/pm-bot)",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass(slots=True)
class BinanceMarketDataClient:
    base_url: str = "https://api.binance.com"

    def latest_price(self, symbol: str = "BTCUSDT") -> PriceTick:
        payload = _require_mapping(
            _get_json(f"{self.base_url}/api/v3/ticker/price?symbol={symbol}"),
            "Binance ticker",
        )
        price = _coerce_float(payload.get("price"))
        if price is None:
            raise ValueError("unexpected Binance ticker payload")
        return PriceTick(price=price)

    def klines(self, interval: str, limit: int = 3, symbol: str = "BTCUSDT") -> list[Candle]:
        binance_interval = "1m" if interval == "5m" else "5m"
        payload = _require_list(
            _get_json(f"{self.base_url}/api/v3/klines?symbol={symbol}&interval={binance_interval}&limit={limit}"),
            "Binance klines",
        )
        candles: list[Candle] = []
        for item in payload:
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) < 5:
                raise ValueError("unexpected Binance klines payload")
            candles.append(
                Candle(
                    open=_require_float(item[1], "Binance klines payload"),
                    high=_require_float(item[2], "Binance klines payload"),
                    low=_require_float(item[3], "Binance klines payload"),
                    close=_require_float(item[4], "Binance klines payload"),
                )
            )
        return candles

    def price_at(self, at: datetime, symbol: str = "BTCUSDT") -> PriceTick:
        end_time_ms = int(at.astimezone(UTC).timestamp() * 1000)
        start_time_ms = max(0, end_time_ms - 60_000)
        payload = _require_list(
            _get_json(
                f"{self.base_url}/api/v3/aggTrades?symbol={symbol}&startTime={start_time_ms}&endTime={end_time_ms}&limit=1000"
            ),
            "Binance aggTrades",
        )
        if not payload:
            raise ValueError("unexpected Binance aggTrades payload")
        item = _require_mapping(payload[-1], "Binance aggTrades")
        trade_time = _coerce_float(item.get("T"))
        if trade_time is None or trade_time > end_time_ms:
            raise ValueError("unexpected Binance aggTrades payload")
        return PriceTick(price=_require_float(item.get("p"), "Binance aggTrades"))


@dataclass(slots=True)
class PolymarketMarketClient:
    base_url: str = "https://gamma-api.polymarket.com"

    def active_markets(self, interval: str) -> list[MarketSnapshot]:
        payload = _require_list(
            _get_json(f"{self.base_url}/markets?active=true&closed=false&limit=500"),
            "Polymarket markets",
        )
        snapshots: list[MarketSnapshot] = []
        wanted = f"btc-updown-{interval}"
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("unexpected Polymarket markets payload")
            slug = str(item.get("slug", ""))
            if wanted not in slug:
                continue

            up_price, down_price = _extract_outcome_prices(item)
            token_ids = _extract_outcome_token_ids(item)
            spread = _optional_float(item.get("spread"), "Polymarket spread")
            if spread is None:
                spread = 0.02
            liquidity_value = item.get("liquidityNum")
            if liquidity_value in (None, ""):
                liquidity_value = item.get("liquidity")
            liquidity = 0.0 if liquidity_value in (None, "") else _require_float(liquidity_value, "Polymarket liquidity")
            reference_price = _optional_float(item.get("referencePrice"), "Polymarket reference price")
            tick_size = _optional_positive_float(item.get("tickSize"), "Polymarket tickSize")
            neg_risk = _optional_bool(item.get("negRisk"), "Polymarket negRisk")

            snapshots.append(
                MarketSnapshot(
                    market_id=str(item.get("id", slug)),
                    slug=slug,
                    interval=interval,
                    active=bool(item.get("active", False)),
                    closed=bool(item.get("closed", False)),
                    seconds_to_expiry=_seconds_to_expiry(item.get("endDate")),
                    liquidity=liquidity,
                    spread=spread,
                    up=OrderBookSide(price=up_price),
                    down=OrderBookSide(price=down_price),
                    reference_price=reference_price,
                    end_date=_normalize_end_date(item.get("endDate")),
                    token_id_up=token_ids.get("UP"),
                    token_id_down=token_ids.get("DOWN"),
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                )
            )
        return snapshots

    def discover_markets(self, keywords: list[str], limit: int = 20) -> list[dict]:
        payload = _require_list(
            _get_json(f"{self.base_url}/markets?active=true&limit=500"),
            "Polymarket markets",
        )
        normalized_keywords = [keyword.strip().lower() for keyword in keywords if keyword.strip()]
        results: list[dict] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("unexpected Polymarket markets payload")
            haystack = " ".join(
                [
                    str(item.get("slug", "")),
                    str(item.get("question", "")),
                    str(item.get("description", "")),
                ]
            ).lower()
            if normalized_keywords and not any(keyword in haystack for keyword in normalized_keywords):
                continue

            prices = _extract_outcome_price_map(item)
            token_ids = _extract_outcome_token_ids(item)
            liquidity_value = item.get("liquidityNum")
            if liquidity_value in (None, ""):
                liquidity_value = item.get("liquidity")
            liquidity = 0.0 if liquidity_value in (None, "") else _require_float(liquidity_value, "Polymarket liquidity")
            results.append(
                {
                    "market_id": str(item.get("id") or item.get("conditionId") or item.get("slug", "")),
                    "slug": str(item.get("slug", "")),
                    "question": str(item.get("question", "")),
                    "end_date": item.get("endDate"),
                    "seconds_to_expiry": _seconds_to_expiry(item.get("endDate")),
                    "liquidity": liquidity,
                    "active": bool(item.get("active", False)),
                    "closed": bool(item.get("closed", False)),
                    "yes_price": prices.get("YES"),
                    "no_price": prices.get("NO"),
                    "up_price": prices.get("UP"),
                    "down_price": prices.get("DOWN"),
                    "token_id_up": token_ids.get("UP"),
                    "token_id_down": token_ids.get("DOWN"),
                    "tick_size": _optional_positive_float(item.get("tickSize"), "Polymarket tickSize"),
                    "neg_risk": _optional_bool(item.get("negRisk"), "Polymarket negRisk"),
                }
            )

        results.sort(key=lambda item: item["liquidity"], reverse=True)
        return results[:limit]


@dataclass(slots=True)
class ChainlinkReferenceClient:
    def reference_price(self, market: MarketSnapshot) -> float | None:
        return market.reference_price


@dataclass(slots=True)
class FixtureBinanceMarketDataClient:
    latest_tick: PriceTick
    candles: list[Candle]

    def latest_price(self, symbol: str = "BTCUSDT") -> PriceTick:
        return self.latest_tick

    def klines(self, interval: str, limit: int = 3, symbol: str = "BTCUSDT") -> list[Candle]:
        return self.candles[:limit]

    def price_at(self, at: datetime, symbol: str = "BTCUSDT") -> PriceTick:
        return self.latest_tick


@dataclass(slots=True)
class FixturePolymarketMarketClient:
    market: MarketSnapshot

    def active_markets(self, interval: str) -> list[MarketSnapshot]:
        if self.market.interval != interval:
            return []
        return [self.market]

    def discover_markets(self, keywords: list[str], limit: int = 20) -> list[dict]:
        diagnostic = {
            "market_id": self.market.market_id,
            "slug": self.market.slug,
            "question": self.market.slug,
            "end_date": None,
            "seconds_to_expiry": self.market.seconds_to_expiry,
            "liquidity": self.market.liquidity,
            "active": self.market.active,
            "closed": self.market.closed,
            "yes_price": None,
            "no_price": None,
            "up_price": self.market.up.price,
            "down_price": self.market.down.price,
            "token_id_up": self.market.token_id_up,
            "token_id_down": self.market.token_id_down,
            "tick_size": self.market.tick_size,
            "neg_risk": self.market.neg_risk,
        }
        return [diagnostic][:limit]


@dataclass(slots=True)
class FixtureChainlinkReferenceClient:
    reference: float | None

    def reference_price(self, market: MarketSnapshot) -> float | None:
        return self.reference


def load_fixture_clients(path: str | Path) -> tuple[
    FixtureBinanceMarketDataClient,
    FixturePolymarketMarketClient,
    FixtureChainlinkReferenceClient,
]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    market_payload = payload["market"]
    reference_price = _optional_float(market_payload.get("reference_price"), "fixture market reference_price")
    market = MarketSnapshot(
        market_id=str(market_payload["market_id"]),
        slug=str(market_payload["slug"]),
        interval=str(market_payload["interval"]),
        active=bool(market_payload["active"]),
        closed=bool(market_payload["closed"]),
        seconds_to_expiry=int(market_payload["seconds_to_expiry"]),
        liquidity=_require_float(market_payload["liquidity"], "fixture market liquidity"),
        spread=_require_float(market_payload["spread"], "fixture market spread"),
        up=OrderBookSide(price=_require_probability(market_payload["up"]["price"], "fixture market up.price")),
        down=OrderBookSide(price=_require_probability(market_payload["down"]["price"], "fixture market down.price")),
        reference_price=reference_price,
        end_date=_normalize_end_date(market_payload.get("end_date")),
        token_id_up=_optional_string(market_payload.get("token_id_up"), "fixture market token_id_up"),
        token_id_down=_optional_string(market_payload.get("token_id_down"), "fixture market token_id_down"),
        tick_size=_optional_positive_float(market_payload.get("tick_size"), "fixture market tick_size"),
        neg_risk=_optional_bool(market_payload.get("neg_risk"), "fixture market neg_risk"),
    )
    latest_price_payload = payload["latest_price"]
    latest_tick = PriceTick(
        price=_require_float(latest_price_payload["price"], "fixture latest_price.price"),
        volume=_require_float(latest_price_payload.get("volume", 0.0), "fixture latest_price.volume"),
    )
    candles = [
        Candle(
            open=_require_float(item["open"], "fixture candle.open"),
            high=_require_float(item["high"], "fixture candle.high"),
            low=_require_float(item["low"], "fixture candle.low"),
            close=_require_float(item["close"], "fixture candle.close"),
        )
        for item in payload["candles"]
    ]
    fixture_reference = _optional_float(payload.get("reference_price"), "fixture reference_price")
    return (
        FixtureBinanceMarketDataClient(latest_tick=latest_tick, candles=candles),
        FixturePolymarketMarketClient(market=market),
        FixtureChainlinkReferenceClient(reference=fixture_reference if fixture_reference is not None else market.reference_price),
    )


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _require_float(value: object, context: str) -> float:
    number = _coerce_float(value)
    if number is None:
        raise ValueError(f"unexpected {context}")
    return number


def _require_probability(value: object, context: str) -> float:
    number = _require_float(value, context)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"unexpected {context}")
    return number


def _optional_float(value: object, context: str) -> float | None:
    if value is None:
        return None
    return _require_float(value, context)


def _optional_positive_float(value: object, context: str) -> float | None:
    if value is None:
        return None
    number = _require_float(value, context)
    if number <= 0.0:
        raise ValueError(f"unexpected {context}")
    return number


def _optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"unexpected {context}")
    return text


def _optional_bool(value: object, context: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise ValueError(f"unexpected {context}")


def _require_mapping(payload: object, context: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected {context} payload")
    return payload


def _require_list(payload: object, context: str) -> list:
    if not isinstance(payload, list):
        raise ValueError(f"unexpected {context} payload")
    return payload


def _extract_outcome_prices(item: dict) -> tuple[float, float]:
    mapping = _extract_outcome_price_map(item)
    up_price = mapping.get("UP")
    down_price = mapping.get("DOWN")
    if up_price is None or down_price is None:
        raise ValueError("unexpected Polymarket outcome prices")
    return up_price, down_price


def _extract_outcome_price_map(item: dict) -> dict[str, float]:
    prices_raw = item.get("outcomePrices")
    outcomes_raw = item.get("outcomes")
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else list(prices_raw or [])
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else list(outcomes_raw or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}

    mapping: dict[str, float] = {}
    for name, price in zip(outcomes, prices, strict=False):
        coerced_price = _coerce_float(price)
        if coerced_price is None or not 0.0 <= coerced_price <= 1.0:
            continue
        mapping[str(name).upper()] = coerced_price
    return mapping


def _extract_outcome_token_ids(item: dict) -> dict[str, str]:
    token_ids_raw = item.get("clobTokenIds")
    if token_ids_raw is None:
        return {}
    outcomes_raw = item.get("outcomes")
    try:
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else list(token_ids_raw)
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else list(outcomes_raw or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("unexpected Polymarket clob token ids") from None
    if len(token_ids) != len(outcomes):
        raise ValueError("unexpected Polymarket clob token ids")

    mapping: dict[str, str] = {}
    for name, token_id in zip(outcomes, token_ids, strict=True):
        normalized = _optional_string(token_id, "Polymarket clob token ids")
        if normalized is None:
            raise ValueError("unexpected Polymarket clob token ids")
        mapping[str(name).upper()] = normalized
    return mapping


def _seconds_to_expiry(end_date: object) -> int:
    if not end_date:
        return 0
    try:
        end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((end_dt - datetime.now(UTC)).total_seconds()))


def _normalize_end_date(end_date: object) -> str | None:
    if not end_date:
        return None
    try:
        end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
    except ValueError:
        return None
    if end_dt.tzinfo is None or end_dt.utcoffset() is None:
        return None
    return end_dt.isoformat()
