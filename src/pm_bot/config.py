from __future__ import annotations

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
    paper_trades_path: Path = Path("data/paper_trades.jsonl")

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls()
