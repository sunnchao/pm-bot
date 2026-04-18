from __future__ import annotations

from pm_bot.config import AppConfig
from pm_bot.models import MarketSnapshot


def evaluate_no_trade_filters(
    market: MarketSnapshot,
    min_volatility_bps: float,
    realized_volatility_bps: float,
    config: AppConfig | None = None,
) -> tuple[bool, list[str]]:
    config = config or AppConfig()
    reasons: list[str] = []

    if not market.active or market.closed:
        reasons.append("market_inactive")
    if market.spread > config.spread_limit:
        reasons.append("spread_above_limit")
    min_liquidity = config.min_liquidity_15m if market.interval == "15m" else config.min_liquidity_5m
    if market.liquidity < min_liquidity:
        reasons.append("liquidity_below_limit")
    min_seconds = config.min_seconds_15m if market.interval == "15m" else config.min_seconds_5m
    if market.seconds_to_expiry < min_seconds:
        reasons.append("insufficient_time_remaining")
    if max(market.up.price, market.down.price) > config.max_side_price:
        reasons.append("extreme_market_price")
    if realized_volatility_bps < min_volatility_bps:
        reasons.append("volatility_too_low")

    return bool(reasons), reasons
