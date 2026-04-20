from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    spread_limit: float = 0.03
    min_liquidity_5m: float = 8_000.0
    min_liquidity_15m: float = 15_000.0
    min_seconds_5m: int = 120
    min_seconds_15m: int = 300
    max_side_price: float = 0.62
    min_volatility_bps: float = 2.0
    base_risk_pct: float = 0.02
    strong_risk_pct: float = 0.04
    max_daily_drawdown_pct: float = 0.05
    cooldown_after_three_losses_minutes: int = 30
    cooldown_after_five_losses_minutes: int = 60
    trading_mode: str = "paper"
    polymarket_host: str = "https://clob.polymarket.com"
    polygon_chain_id: int = 137
    wallet_private_key: str | None = None
    signature_type: int | None = None
    funder_address: str | None = None
    live_max_order_usd: float = 10.0
    live_allow_market_ids: tuple[str, ...] = ()
    live_require_explicit_confirm: bool = True
    paper_trades_path: Path = Path("data/paper_trades.jsonl")
    live_orders_path: Path = Path("data/live_orders.jsonl")

    @classmethod
    def from_env(cls) -> "AppConfig":
        defaults = cls()
        return cls(
            spread_limit=_env_float("SPREAD_LIMIT", defaults.spread_limit),
            min_liquidity_5m=_env_float("MIN_LIQUIDITY_5M", defaults.min_liquidity_5m),
            min_liquidity_15m=_env_float("MIN_LIQUIDITY_15M", defaults.min_liquidity_15m),
            min_seconds_5m=_env_positive_int("MIN_SECONDS_5M", defaults.min_seconds_5m),
            min_seconds_15m=_env_positive_int("MIN_SECONDS_15M", defaults.min_seconds_15m),
            max_side_price=_env_float("MAX_SIDE_PRICE", defaults.max_side_price),
            min_volatility_bps=_env_float("MIN_VOLATILITY_BPS", defaults.min_volatility_bps),
            base_risk_pct=_env_float("BASE_RISK_PCT", defaults.base_risk_pct),
            strong_risk_pct=_env_float("STRONG_RISK_PCT", defaults.strong_risk_pct),
            max_daily_drawdown_pct=_env_float("MAX_DAILY_DRAWDOWN_PCT", defaults.max_daily_drawdown_pct),
            cooldown_after_three_losses_minutes=_env_int(
                "COOLDOWN_AFTER_THREE_LOSSES_MINUTES",
                defaults.cooldown_after_three_losses_minutes,
            ),
            cooldown_after_five_losses_minutes=_env_int(
                "COOLDOWN_AFTER_FIVE_LOSSES_MINUTES",
                defaults.cooldown_after_five_losses_minutes,
            ),
            trading_mode=_env_text("TRADING_MODE") or defaults.trading_mode,
            polymarket_host=_env_text("POLYMARKET_HOST") or defaults.polymarket_host,
            polygon_chain_id=_env_int("POLYGON_CHAIN_ID", defaults.polygon_chain_id),
            wallet_private_key=_env_text("WALLET_PRIVATE_KEY"),
            signature_type=_env_optional_int("SIGNATURE_TYPE"),
            funder_address=_env_text("FUNDER_ADDRESS"),
            live_max_order_usd=_env_float("LIVE_MAX_ORDER_USD", defaults.live_max_order_usd),
            live_allow_market_ids=_env_csv("LIVE_ALLOW_MARKET_IDS", defaults.live_allow_market_ids),
            live_require_explicit_confirm=_env_bool(
                "LIVE_REQUIRE_EXPLICIT_CONFIRM",
                defaults.live_require_explicit_confirm,
            ),
            paper_trades_path=_env_path("PAPER_TRADES_PATH", defaults.paper_trades_path),
            live_orders_path=_env_path("LIVE_ORDERS_PATH", defaults.live_orders_path),
        )

    @classmethod
    def paper_from_env(cls) -> "AppConfig":
        defaults = cls()
        return cls(
            spread_limit=_env_float("SPREAD_LIMIT", defaults.spread_limit),
            min_liquidity_5m=_env_float("MIN_LIQUIDITY_5M", defaults.min_liquidity_5m),
            min_liquidity_15m=_env_float("MIN_LIQUIDITY_15M", defaults.min_liquidity_15m),
            min_seconds_5m=_env_positive_int("MIN_SECONDS_5M", defaults.min_seconds_5m),
            min_seconds_15m=_env_positive_int("MIN_SECONDS_15M", defaults.min_seconds_15m),
            max_side_price=_env_float("MAX_SIDE_PRICE", defaults.max_side_price),
            min_volatility_bps=_env_float("MIN_VOLATILITY_BPS", defaults.min_volatility_bps),
            base_risk_pct=_env_float("BASE_RISK_PCT", defaults.base_risk_pct),
            strong_risk_pct=_env_float("STRONG_RISK_PCT", defaults.strong_risk_pct),
            max_daily_drawdown_pct=_env_float("MAX_DAILY_DRAWDOWN_PCT", defaults.max_daily_drawdown_pct),
            cooldown_after_three_losses_minutes=_env_int(
                "COOLDOWN_AFTER_THREE_LOSSES_MINUTES",
                defaults.cooldown_after_three_losses_minutes,
            ),
            cooldown_after_five_losses_minutes=_env_int(
                "COOLDOWN_AFTER_FIVE_LOSSES_MINUTES",
                defaults.cooldown_after_five_losses_minutes,
            ),
            trading_mode="paper",
            paper_trades_path=_env_path("PAPER_TRADES_PATH", defaults.paper_trades_path),
            live_orders_path=_env_path("LIVE_ORDERS_PATH", defaults.live_orders_path),
        )


def _env_text(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env_int(name: str, default: int) -> int:
    value = _env_text(name)
    return default if value is None else int(value)


def _env_positive_int(name: str, default: int) -> int:
    value = _env_text(name)
    if value is None:
        return default
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"invalid positive int for {name}: {value}")
    return parsed


def _env_optional_int(name: str) -> int | None:
    value = _env_text(name)
    return None if value is None else int(value)


def _env_float(name: str, default: float) -> float:
    value = _env_text(name)
    if value is None:
        return default
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"invalid float for {name}: {value}")
    return parsed


def _env_bool(name: str, default: bool) -> bool:
    value = _env_text(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean for {name}: {value}")


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = _env_text(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env_path(name: str, default: Path) -> Path:
    value = _env_text(name)
    return default if value is None else Path(value)
