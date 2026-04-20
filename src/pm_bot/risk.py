from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pm_bot.config import AppConfig
from pm_bot.models import SignalDecision
from pm_bot.money import quantize_usd, to_decimal, to_float


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
        balance_amount = to_decimal(balance)
        max_daily_drawdown_pct = to_decimal(self.config.max_daily_drawdown_pct)
        daily_pnl = sum(
            (to_decimal(trade.pnl) for trade in self.closed_trades if trade.closed_at.date() == now.date()),
            start=to_decimal(0),
        )
        if daily_pnl <= -(balance_amount * max_daily_drawdown_pct):
            reasons.append("daily_drawdown_limit")

        five_loss_cooldown = timedelta(minutes=self.config.cooldown_after_five_losses_minutes)
        five_loss_streak, last_five_loss_time = self._loss_streak(reset_after=five_loss_cooldown)
        if (
            last_five_loss_time is not None
            and five_loss_streak >= 5
            and now < last_five_loss_time + five_loss_cooldown
        ):
            reasons.append("five_loss_lockout")
        else:
            three_loss_cooldown = timedelta(minutes=self.config.cooldown_after_three_losses_minutes)
            three_loss_streak, last_three_loss_time = self._loss_streak(reset_after=three_loss_cooldown)
            if (
                last_three_loss_time is not None
                and three_loss_streak >= 3
                and now < last_three_loss_time + three_loss_cooldown
            ):
                reasons.append("cooldown_after_losses")

        return not reasons, reasons

    def position_size(
        self,
        balance: float,
        decision: SignalDecision,
        *,
        live_mode: bool = False,
    ) -> float:
        if not decision.should_trade:
            return 0.0
        risk_pct = self.config.strong_risk_pct if decision.signal_name == "oracle_delay" else self.config.base_risk_pct
        stake = quantize_usd(to_decimal(balance) * to_decimal(risk_pct))
        live_max_order_usd = to_decimal(self.config.live_max_order_usd)
        if live_mode and live_max_order_usd.is_finite() and live_max_order_usd > 0:
            stake = min(stake, live_max_order_usd)
        return to_float(quantize_usd(stake))

    def _loss_streak(self, *, reset_after: timedelta | None = None) -> tuple[int, datetime | None]:
        loss_streak = 0
        last_loss_time: datetime | None = None
        newer_loss_time: datetime | None = None

        for trade in reversed(self.closed_trades):
            if to_decimal(trade.pnl) >= 0:
                break
            if (
                reset_after is not None
                and newer_loss_time is not None
                and newer_loss_time - trade.closed_at >= reset_after
            ):
                break

            loss_streak += 1
            if last_loss_time is None:
                last_loss_time = trade.closed_at
            newer_loss_time = trade.closed_at

        return loss_streak, last_loss_time
