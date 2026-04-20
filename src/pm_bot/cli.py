from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from pm_bot.clients import load_fixture_clients
from pm_bot.config import AppConfig
from pm_bot.execution import LivePolymarketExecutor
from pm_bot.live_recorder import LiveOrderRecorder
from pm_bot.live_service import LiveTradingService
from pm_bot.service import TradingService


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="pm-bot")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    oneshot = subparsers.add_parser("oneshot", help="Run one decision cycle")
    oneshot.add_argument("--interval", choices=["5m", "15m"], required=True)
    oneshot.add_argument("--balance", type=float, default=1_000.0)
    oneshot.add_argument("--fixture", help="Use a local JSON fixture instead of live market data")

    loop = subparsers.add_parser("loop", help="Repeat oneshot on a fixed sleep interval")
    loop.add_argument("--interval", choices=["5m", "15m"], required=True)
    loop.add_argument("--balance", type=float, default=1_000.0)
    loop.add_argument("--fixture", help="Use a local JSON fixture instead of live market data")
    loop.add_argument("--sleep-seconds", type=int, default=60)
    loop.add_argument("--iterations", type=int, default=0, help="0 means run forever")

    live = subparsers.add_parser("live", help="Run one live trading decision cycle")
    live.add_argument("--interval", choices=["5m", "15m"], required=True)
    live.add_argument("--balance", type=float, default=1_000.0)
    live.add_argument("--confirm-live", action="store_true")

    live_loop = subparsers.add_parser("live-loop", help="Repeat live decision cycles on a fixed sleep interval")
    live_loop.add_argument("--interval", choices=["5m", "15m"], required=True)
    live_loop.add_argument("--balance", type=float, default=1_000.0)
    live_loop.add_argument("--confirm-live", action="store_true")
    live_loop.add_argument("--sleep-seconds", type=int, default=60)
    live_loop.add_argument("--iterations", type=int, default=0, help="0 means run forever")

    discover = subparsers.add_parser("discover", help="Inspect active BTC-related Polymarket markets")
    discover.add_argument("--keyword", action="append")
    discover.add_argument("--limit", type=int, default=20)

    return parser


def main() -> int:
    parser = build_parser()

    try:
        args = parser.parse_args()
        _validate_args(args)
        service = _build_service(args)

        if args.command == "discover":
            keywords = args.keyword or ["btc", "bitcoin"]
            print(_json_dumps(service.discover(keywords=keywords, limit=args.limit)))
            return 0

        if args.command in {"oneshot", "live"}:
            result = service.oneshot(
                interval=args.interval,
                balance=args.balance,
                live_confirmed=_live_confirmed(args),
            )
            print(_json_dumps(asdict(result)))
            return 0

        iterations = 0
        while True:
            result = service.oneshot(
                interval=args.interval,
                balance=args.balance,
                live_confirmed=_live_confirmed(args),
            )
            print(_json_dumps(asdict(result)))
            iterations += 1
            if args.iterations and iterations >= args.iterations:
                return 0
            time.sleep(args.sleep_seconds)
    except Exception as exc:
        print(_json_dumps({"action": "error", "error": str(exc)}))
        return 1


def _validate_args(args: argparse.Namespace) -> None:
    command = getattr(args, "command", None)
    if command == "discover" and args.limit < 0:
        raise ValueError("limit must be >= 0")
    if hasattr(args, "balance") and args.balance < 0:
        raise ValueError("balance must be >= 0")
    if command in {"loop", "live-loop"}:
        if args.sleep_seconds < 0:
            raise ValueError("sleep_seconds must be >= 0")
        if args.iterations < 0:
            raise ValueError("iterations must be >= 0")
    if _is_live_command(command) and not getattr(args, "confirm_live", False):
        raise ValueError("--confirm-live is required for live trading")


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, allow_nan=False)


def _build_service(args: argparse.Namespace) -> TradingService:
    if _is_live_command(getattr(args, "command", None)):
        return _build_live_service()
    return _build_paper_service(getattr(args, "fixture", None))


def _build_paper_service(fixture: str | None) -> TradingService:
    config = AppConfig.paper_from_env()
    if not fixture:
        return TradingService(config=config)
    binance, polymarket, chainlink = load_fixture_clients(Path(fixture))
    return TradingService(config=config, binance=binance, polymarket=polymarket, chainlink=chainlink)


def _build_live_service() -> LiveTradingService:
    config = AppConfig.from_env()
    _validate_live_config(config)
    live_recorder = LiveOrderRecorder(config.live_orders_path)
    executor = LivePolymarketExecutor(config=config, recorder=live_recorder)
    return LiveTradingService(
        config=config,
        executor=executor,
        live_client=executor,
        live_recorder=live_recorder,
    )


def _validate_live_config(config: AppConfig) -> None:
    if config.trading_mode != "live":
        raise ValueError("TRADING_MODE must be 'live' for live commands")
    if _is_blank(config.wallet_private_key) or config.signature_type is None or _is_blank(config.funder_address):
        raise ValueError("live wallet config incomplete")


def _live_confirmed(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "confirm_live", False))


def _is_live_command(command: str | None) -> bool:
    return command in {"live", "live-loop"}


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


if __name__ == "__main__":
    raise SystemExit(main())
