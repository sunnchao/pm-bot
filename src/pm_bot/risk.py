from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pm_bot.config import AppConfig
from pm_bot.models import SignalDecision


@dataclass(slots=True)
class ClosedTrade:
    pnl: float
    closed_at: datetime


@dataclass(slots=True)
class RiskManager:
    config: AppConfig = field(default_factory=AppConfig)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    def record_closed_trade(self, pnl: float, closed_at: datetime) -> None:
        self.closed_trades.append(ClosedTrade(pnl=pnl, closed_at=closed_at))

    def allow_trade(self, balance: float, now: datetime) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        daily_pnl = sum(t.pnl for t in self.closed_trades if t.closed_at.date() == now.date())
        if daily_pnl <= -(balance * self.config.max_daily_drawdown_pct):
            reasons.append("daily_drawdown_limit")

        loss_streak = 0
        for trade in reversed(self.closed_trades):
            if trade.pnl < 0:
                loss_streak += 1
            else:
                break

        if loss_streak >= 5:
            reasons.append("five_loss_lockout")
        elif loss_streak >= 3 and self.closed_trades:
            last_loss_time = self.closed_trades[-1].closed_at
            cooldown = timedelta(minutes=self.config.cooldown_after_three_losses_minutes)
            if now < last_loss_time + cooldown:
                reasons.append("cooldown_after_losses")

        return not reasons, reasons

    def position_size(self, balance: float, decision: SignalDecision) -> float:
        if not decision.should_trade:
            return 0.0
        risk_pct = self.config.strong_risk_pct if decision.signal_name == "oracle_delay" else self.config.base_risk_pct
        return round(balance * risk_pct, 2)
