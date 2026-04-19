from __future__ import annotations

import math

from pm_bot.config import AppConfig
from pm_bot.execution import ExecutionRequest
from pm_bot.models import MarketSnapshot


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def evaluate_live_order_guards(
    *,
    config: AppConfig,
    market: MarketSnapshot,
    request: ExecutionRequest,
    live_confirmed: bool,
) -> list[str]:
    reasons: list[str] = []
    valid_sides = {"UP", "DOWN"}

    if config.trading_mode != "live":
        reasons.append("live_mode_required")
    if config.live_require_explicit_confirm and not live_confirmed:
        reasons.append("live_confirmation_required")
    if not config.live_allow_market_ids:
        reasons.append("live_market_allowlist_required")
    elif market.market_id not in config.live_allow_market_ids:
        reasons.append("live_market_not_allowlisted")
    if not math.isfinite(request.size_usd) or request.size_usd <= 0:
        reasons.append("live_order_size_invalid")
    elif not math.isfinite(config.live_max_order_usd) or config.live_max_order_usd <= 0:
        reasons.append("live_order_size_limit_invalid")
    elif request.size_usd > config.live_max_order_usd:
        reasons.append("live_order_size_exceeds_limit")
    if request.side not in valid_sides:
        reasons.append("live_side_invalid")
    if not math.isfinite(request.price) or request.price <= 0:
        reasons.append("live_price_invalid")
    elif not math.isfinite(config.max_side_price) or config.max_side_price <= 0:
        reasons.append("live_price_limit_invalid")
    elif request.price > config.max_side_price:
        reasons.append("live_price_exceeds_max_side_price")

    min_seconds = config.min_seconds_15m if market.interval == "15m" else config.min_seconds_5m
    if min_seconds <= 0:
        reasons.append("live_min_seconds_invalid")
    elif market.seconds_to_expiry < min_seconds:
        reasons.append("live_market_too_close_to_expiry")
    if not market.token_id_up or not market.token_id_down:
        reasons.append("live_market_token_ids_incomplete")
    expected_token_id = None
    if request.side == "UP":
        expected_token_id = market.token_id_up
    elif request.side == "DOWN":
        expected_token_id = market.token_id_down
    if not request.token_id:
        reasons.append("live_token_id_missing")
    elif expected_token_id is not None and request.token_id != expected_token_id:
        reasons.append("live_token_id_mismatch")
    if market.neg_risk is None:
        reasons.append("live_neg_risk_missing")
    if (
        _is_blank(config.wallet_private_key)
        or config.signature_type is None
        or _is_blank(config.funder_address)
    ):
        reasons.append("live_wallet_config_incomplete")

    return reasons
