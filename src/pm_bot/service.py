from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from pm_bot.clients import BinanceMarketDataClient, ChainlinkReferenceClient, PolymarketMarketClient
from pm_bot.config import AppConfig
from pm_bot.filters import evaluate_no_trade_filters
from pm_bot.models import MarketSnapshot, PaperTradeRecord, SignalDecision
from pm_bot.recorder import PaperTradeRecorder
from pm_bot.risk import RiskManager
from pm_bot.signals import SignalEngine


@dataclass(slots=True)
class OneShotResult:
    interval: str
    market_id: str | None
    action: str
    reasons: list[str]
    signal_name: str | None = None
    confidence: float = 0.0
    side: str | None = None
    stake: float = 0.0


class TradingService:
    def __init__(
        self,
        config: AppConfig | None = None,
        binance: BinanceMarketDataClient | None = None,
        polymarket: PolymarketMarketClient | None = None,
        chainlink: ChainlinkReferenceClient | None = None,
        signal_engine: SignalEngine | None = None,
        risk_manager: RiskManager | None = None,
        recorder: PaperTradeRecorder | None = None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.binance = binance or BinanceMarketDataClient()
        self.polymarket = polymarket or PolymarketMarketClient()
        self.chainlink = chainlink or ChainlinkReferenceClient()
        self.signal_engine = signal_engine or SignalEngine()
        self.risk_manager = risk_manager or RiskManager(config=self.config)
        self.recorder = recorder

    def discover(self, keywords: list[str], limit: int = 20) -> list[dict]:
        return self.polymarket.discover_markets(keywords=keywords, limit=limit)

    def oneshot(self, interval: str, balance: float = 1_000.0) -> OneShotResult:
        market = self._select_market(interval)
        if market is None:
            return OneShotResult(interval=interval, market_id=None, action="skip", reasons=["no_active_market"])

        latest_tick = self.binance.latest_price()
        candles = self.binance.klines(interval=interval, limit=3)
        recent_ticks = [PriceTick(price=c.close, volume=0.0) for c in candles[:-1]] + [latest_tick]
        realized_volatility_bps = _realized_volatility_bps(candles)
        blocked, filter_reasons = evaluate_no_trade_filters(
            market=market,
            min_volatility_bps=self.config.min_volatility_bps,
            realized_volatility_bps=realized_volatility_bps,
            config=self.config,
        )
        if blocked:
            return OneShotResult(interval=interval, market_id=market.market_id, action="skip", reasons=filter_reasons)

        reference_price = self.chainlink.reference_price(market)
        if reference_price is not None:
            if not math.isfinite(reference_price):
                raise ValueError("unexpected reference price")
            market.reference_price = reference_price

        decision = self.signal_engine.decide(
            market=market,
            latest_tick=latest_tick,
            recent_ticks=recent_ticks,
            candles=candles,
        )
        if not decision.should_trade:
            return OneShotResult(
                interval=interval,
                market_id=market.market_id,
                action="skip",
                reasons=decision.reasons,
                signal_name=decision.signal_name,
                confidence=decision.confidence,
            )

        allowed, risk_reasons = self.risk_manager.allow_trade(balance=balance, now=datetime.now(UTC))
        if not allowed:
            return OneShotResult(interval=interval, market_id=market.market_id, action="skip", reasons=risk_reasons)

        stake = self.risk_manager.position_size(balance=balance, decision=decision)
        entry_price = market.up.price if decision.side == "UP" else market.down.price
        recorder = self.recorder or PaperTradeRecorder(self.config.paper_trades_path)
        self.recorder = recorder
        recorder.record(
            PaperTradeRecord(
                timestamp=datetime.now(UTC).isoformat(),
                market_id=market.market_id,
                interval=interval,
                side=decision.side or "NONE",
                price=entry_price,
                stake=stake,
                signal=decision,
                notes=["paper_trade"],
            )
        )
        return OneShotResult(
            interval=interval,
            market_id=market.market_id,
            action="paper_trade",
            reasons=decision.reasons,
            signal_name=decision.signal_name,
            confidence=decision.confidence,
            side=decision.side,
            stake=stake,
        )

    def _select_market(self, interval: str) -> MarketSnapshot | None:
        markets = self.polymarket.active_markets(interval)
        if not markets:
            return None
        eligible = [m for m in markets if m.active and not m.closed]
        pool = eligible or markets
        return max(pool, key=lambda item: item.liquidity)


def _realized_volatility_bps(candles: list) -> float:
    if len(candles) < 2:
        return 0.0
    start = candles[0].close
    end = candles[-1].close
    return abs((end - start) / start) * 10_000


from pm_bot.models import PriceTick  # noqa: E402
