from __future__ import annotations

from statistics import mean

from pm_bot.models import Candle, MarketSnapshot, PriceTick, SignalDecision


class SignalEngine:
    def decide(
        self,
        market: MarketSnapshot,
        latest_tick: PriceTick,
        recent_ticks: list[PriceTick],
        candles: list[Candle],
    ) -> SignalDecision:
        if market.interval == "15m":
            detectors = [self.mean_reversion_signal, self.oracle_delay_signal, self.momentum_signal]
        else:
            detectors = [self.oracle_delay_signal, self.momentum_signal, self.mean_reversion_signal]
        trade_decisions: list[SignalDecision] = []
        for detector in detectors:
            decision = detector(market=market, latest_tick=latest_tick, recent_ticks=recent_ticks, candles=candles)
            if decision.should_trade:
                trade_decisions.append(decision)

        if not trade_decisions:
            return SignalDecision(
                should_trade=False,
                side=None,
                signal_name=None,
                confidence=0.0,
                reasons=["no_signal"],
            )

        sides = {decision.side for decision in trade_decisions if decision.side is not None}
        if len(sides) > 1:
            return SignalDecision(
                should_trade=False,
                side=None,
                signal_name=None,
                confidence=0.0,
                reasons=["conflicting_signals"],
            )

        return trade_decisions[0]

    def oracle_delay_signal(
        self,
        market: MarketSnapshot,
        latest_tick: PriceTick,
        recent_ticks: list[PriceTick],
        candles: list[Candle],
    ) -> SignalDecision:
        if not recent_ticks or market.midpoint_distance() > 0.05:
            return SignalDecision(False, None, None, 0.0, ["oracle_delay_not_applicable"])
        baseline = market.reference_price if market.reference_price is not None else recent_ticks[0].price
        delta = latest_tick.price - baseline
        if abs(delta) < 35:
            return SignalDecision(False, None, None, 0.0, ["oracle_delta_too_small"])
        side = "UP" if delta > 0 else "DOWN"
        confidence = min(0.9, 0.6 + abs(delta) / 200.0)
        return SignalDecision(True, side, "oracle_delay", confidence, [f"delta={delta:.2f}"])

    def momentum_signal(
        self,
        market: MarketSnapshot,
        latest_tick: PriceTick,
        recent_ticks: list[PriceTick],
        candles: list[Candle],
    ) -> SignalDecision:
        if len(candles) < 3:
            return SignalDecision(False, None, None, 0.0, ["not_enough_candles"])
        closes = [c.close for c in candles[-3:]]
        if closes[0] < closes[1] < closes[2]:
            return SignalDecision(True, "UP", "momentum", 0.68, ["three_higher_closes"])
        if closes[0] > closes[1] > closes[2]:
            return SignalDecision(True, "DOWN", "momentum", 0.68, ["three_lower_closes"])
        return SignalDecision(False, None, None, 0.0, ["momentum_not_aligned"])

    def mean_reversion_signal(
        self,
        market: MarketSnapshot,
        latest_tick: PriceTick,
        recent_ticks: list[PriceTick],
        candles: list[Candle],
    ) -> SignalDecision:
        if market.interval != "15m" or len(candles) < 3:
            return SignalDecision(False, None, None, 0.0, ["mean_reversion_not_applicable"])
        closes = [c.close for c in candles[-3:]]
        average_close = mean(closes)
        latest_close = closes[-1]
        move = latest_close - average_close
        if move <= -70:
            return SignalDecision(True, "UP", "mean_reversion", 0.64, ["oversold_vs_recent_mean"])
        if move >= 70:
            return SignalDecision(True, "DOWN", "mean_reversion", 0.64, ["overbought_vs_recent_mean"])
        return SignalDecision(False, None, None, 0.0, ["mean_reversion_not_extreme"])
