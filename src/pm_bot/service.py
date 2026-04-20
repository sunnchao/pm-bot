from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter_ns

from pm_bot.clients import BinanceMarketDataClient, ChainlinkReferenceClient, PolymarketMarketClient
from pm_bot.config import AppConfig
from pm_bot.execution import ExecutionRequest, PaperExecutor
from pm_bot.filters import evaluate_no_trade_filters
from pm_bot.live_guards import evaluate_live_order_guards
from pm_bot.metrics import OneShotCycleMetrics, emit_cycle_error, emit_cycle_result
from pm_bot.models import MarketSnapshot
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
    execution_status: str | None = None
    execution_message: str | None = None
    order_id: str | None = None
    submission_id: str | None = None


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
        executor=None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.binance = binance or BinanceMarketDataClient()
        self.polymarket = polymarket or PolymarketMarketClient()
        self.chainlink = chainlink or ChainlinkReferenceClient()
        self.signal_engine = signal_engine or SignalEngine()
        self.risk_manager = risk_manager or RiskManager(config=self.config)
        self.recorder = recorder
        self.executor = executor
        self._loaded_historical_settlements = False

    def discover(self, keywords: list[str], limit: int = 20) -> list[dict]:
        return self.polymarket.discover_markets(keywords=keywords, limit=limit)

    def oneshot(self, interval: str, balance: float = 1_000.0, *, live_confirmed: bool = False) -> OneShotResult:
        started_at_ns = perf_counter_ns()
        cycle_metrics = OneShotCycleMetrics(interval=interval)
        try:
            result = self._oneshot_impl(
                interval=interval,
                balance=balance,
                live_confirmed=live_confirmed,
                cycle_metrics=cycle_metrics,
            )
        except Exception as exc:
            emit_cycle_error(cycle_metrics, error=exc, duration_ms=_duration_ms(started_at_ns))
            raise

        emit_cycle_result(
            cycle_metrics,
            action=result.action,
            duration_ms=_duration_ms(started_at_ns),
            execution_status=result.execution_status,
            execution_message=result.execution_message,
            order_id=result.order_id,
            submission_id=result.submission_id,
        )
        return result

    def _oneshot_impl(
        self,
        interval: str,
        balance: float,
        *,
        live_confirmed: bool,
        cycle_metrics: OneShotCycleMetrics,
    ) -> OneShotResult:
        now = datetime.now(UTC)
        executor = self.executor
        recorder = self.recorder
        uses_paper_execution = executor is None or isinstance(executor, PaperExecutor)
        if self.config.trading_mode == "live" and uses_paper_execution:
            cycle_metrics.reasons = ["live_executor_not_configured"]
            return OneShotResult(
                interval=interval,
                market_id=None,
                action="skip",
                reasons=["live_executor_not_configured"],
                execution_status="blocked_live_order",
                execution_message="live_executor_not_configured",
            )
        if uses_paper_execution:
            recorder = recorder or (executor.recorder if isinstance(executor, PaperExecutor) else None)
            recorder = recorder or PaperTradeRecorder(self.config.paper_trades_path)
            self.recorder = recorder
            if executor is None:
                executor = PaperExecutor(recorder)
                self.executor = executor
            if not self._loaded_historical_settlements:
                for settlement in recorder.settled_trades():
                    self.risk_manager.record_closed_trade(pnl=settlement["pnl"], closed_at=settlement["closed_at"])
                self._loaded_historical_settlements = True
        latest_tick = self.binance.latest_price()
        if uses_paper_execution and recorder is not None:
            # In paper mode Binance remains the fast market-data source for both
            # signal inputs and the surrogate expiry price used to simulate how
            # Chainlink would resolve against the recorded reference price.
            for settlement in recorder.settle_due(
                current_btc_price=latest_tick.price,
                now=now,
                settlement_price_at=lambda expires_at: self.binance.price_at(expires_at).price,
            ):
                self.risk_manager.record_closed_trade(pnl=settlement["pnl"], closed_at=settlement["closed_at"])

        market = self._select_market(interval, prefer_allowlisted_live_market=not uses_paper_execution)
        if market is None:
            cycle_metrics.reasons = ["no_active_market"]
            return OneShotResult(interval=interval, market_id=None, action="skip", reasons=["no_active_market"])

        cycle_metrics.market_id = market.market_id
        candles = self.binance.klines(interval=interval, limit=3)
        recent_ticks = [PriceTick(price=c.close, volume=0.0) for c in candles[:-1]] + [latest_tick]
        realized_volatility_bps = _realized_volatility_bps(candles)
        blocked, filter_reasons = evaluate_no_trade_filters(
            market=market,
            min_volatility_bps=self.config.min_volatility_bps,
            realized_volatility_bps=realized_volatility_bps,
            config=self.config,
        )
        if not uses_paper_execution:
            filter_reasons = [
                reason
                for reason in filter_reasons
                if reason not in {"extreme_market_price", "insufficient_time_remaining"}
            ]
            blocked = bool(filter_reasons)
        if blocked:
            cycle_metrics.reasons = list(filter_reasons)
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
        cycle_metrics.signal_name = decision.signal_name
        cycle_metrics.confidence = decision.confidence
        cycle_metrics.side = decision.side
        cycle_metrics.reasons = list(decision.reasons)
        if not decision.should_trade:
            return OneShotResult(
                interval=interval,
                market_id=market.market_id,
                action="skip",
                reasons=decision.reasons,
                signal_name=decision.signal_name,
                confidence=decision.confidence,
            )

        allowed, risk_reasons = self.risk_manager.allow_trade(balance=balance, now=now)
        if not allowed:
            cycle_metrics.reasons = list(risk_reasons)
            return OneShotResult(interval=interval, market_id=market.market_id, action="skip", reasons=risk_reasons)

        stake = self.risk_manager.position_size(
            balance=balance,
            decision=decision,
            live_mode=not uses_paper_execution,
        )
        cycle_metrics.stake = stake
        if decision.side not in {"UP", "DOWN"}:
            execution_status = "blocked_live_order" if not uses_paper_execution else None
            cycle_metrics.reasons = ["decision_side_invalid"]
            return OneShotResult(
                interval=interval,
                market_id=market.market_id,
                action="skip",
                reasons=["decision_side_invalid"],
                signal_name=decision.signal_name,
                confidence=decision.confidence,
                side=decision.side,
                stake=stake,
                execution_status=execution_status,
                execution_message="decision_side_invalid" if execution_status is not None else None,
            )
        if market.reference_price is None:
            execution_status = "blocked_live_order" if not uses_paper_execution else None
            cycle_metrics.reasons = ["reference_price_unavailable"]
            return OneShotResult(
                interval=interval,
                market_id=market.market_id,
                action="skip",
                reasons=["reference_price_unavailable"],
                signal_name=decision.signal_name,
                confidence=decision.confidence,
                side=decision.side,
                stake=stake,
                execution_status=execution_status,
                execution_message="reference_price_unavailable" if execution_status is not None else None,
            )
        entry_price = market.up.price if decision.side == "UP" else market.down.price
        token_id = None
        if not uses_paper_execution:
            token_id = market.token_id_up if decision.side == "UP" else market.token_id_down
        execution_request = ExecutionRequest(
            market_id=market.market_id,
            token_id=token_id,
            side=decision.side or "NONE",
            price=entry_price,
            size_usd=stake,
            order_type="market",
            metadata={
                "timestamp": now.isoformat(),
                "interval": interval,
                "expires_at": _market_expires_at(market, now),
                "reference_price": market.reference_price,
                "signal": {
                    "should_trade": decision.should_trade,
                    "side": decision.side,
                    "signal_name": decision.signal_name,
                    "confidence": decision.confidence,
                    "reasons": list(decision.reasons),
                },
                "tick_size": getattr(market, "tick_size", None),
                "neg_risk": getattr(market, "neg_risk", None),
                "notes": ["paper_trade"] if uses_paper_execution else ["live_trade"],
            },
        )
        if not uses_paper_execution:
            live_guard_reasons = evaluate_live_order_guards(
                config=self.config,
                market=market,
                request=execution_request,
                live_confirmed=live_confirmed,
            )
            if live_guard_reasons:
                cycle_metrics.reasons = list(live_guard_reasons)
                return OneShotResult(
                    interval=interval,
                    market_id=market.market_id,
                    action="skip",
                    reasons=live_guard_reasons,
                    signal_name=decision.signal_name,
                    confidence=decision.confidence,
                    side=decision.side,
                    stake=stake,
                    execution_status="blocked_live_order",
                    execution_message=",".join(live_guard_reasons),
                )
        execution_result = executor.execute(execution_request)
        return OneShotResult(
            interval=interval,
            market_id=market.market_id,
            action=execution_result.action,
            reasons=decision.reasons,
            signal_name=decision.signal_name,
            confidence=decision.confidence,
            side=decision.side,
            stake=stake,
            execution_status=execution_result.status,
            execution_message=execution_result.message,
            order_id=execution_result.order_id,
            submission_id=execution_result.submission_id,
        )

    def _select_market(self, interval: str, *, prefer_allowlisted_live_market: bool = False) -> MarketSnapshot | None:
        markets = self.polymarket.active_markets(interval)
        if not markets:
            return None
        eligible = [m for m in markets if m.active and not m.closed]
        pool = eligible or markets
        if prefer_allowlisted_live_market and self.config.live_allow_market_ids:
            allowlisted = [m for m in pool if m.market_id in self.config.live_allow_market_ids]
            if allowlisted:
                pool = allowlisted
        return max(pool, key=lambda item: item.liquidity)


def _duration_ms(started_at_ns: int) -> float:
    return (perf_counter_ns() - started_at_ns) / 1_000_000


def _realized_volatility_bps(candles: list) -> float:
    if len(candles) < 2:
        return 0.0
    start = candles[0].close
    end = candles[-1].close
    return abs((end - start) / start) * 10_000


def _market_expires_at(market: MarketSnapshot, now: datetime) -> str:
    end_date = getattr(market, "end_date", None)
    if isinstance(end_date, str):
        try:
            expires_at = datetime.fromisoformat(end_date)
        except ValueError:
            expires_at = None
        if expires_at is not None and expires_at.tzinfo is not None and expires_at.utcoffset() is not None:
            return expires_at.isoformat()
    return (now + timedelta(seconds=market.seconds_to_expiry)).isoformat()


from pm_bot.models import PriceTick  # noqa: E402
