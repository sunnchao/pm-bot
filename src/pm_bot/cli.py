from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from pm_bot.clients import load_fixture_clients
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

    discover = subparsers.add_parser("discover", help="Inspect active BTC-related Polymarket markets")
    discover.add_argument("--keyword", action="append")
    discover.add_argument("--limit", type=int, default=20)

    return parser


def main() -> int:
    parser = build_parser()

    try:
        args = parser.parse_args()
        _validate_args(args)
        service = _build_service(getattr(args, "fixture", None))

        if args.command == "discover":
            keywords = args.keyword or ["btc", "bitcoin"]
            print(_json_dumps(service.discover(keywords=keywords, limit=args.limit)))
            return 0

        if args.command == "oneshot":
            result = service.oneshot(interval=args.interval, balance=args.balance)
            print(_json_dumps(asdict(result)))
            return 0

        iterations = 0
        while True:
            result = service.oneshot(interval=args.interval, balance=args.balance)
            print(_json_dumps(asdict(result)))
            iterations += 1
            if args.iterations and iterations >= args.iterations:
                return 0
            time.sleep(args.sleep_seconds)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(_json_dumps({"action": "error", "error": str(exc)}))
        return 1


def _validate_args(args: argparse.Namespace) -> None:
    if getattr(args, "command", None) == "discover" and args.limit < 0:
        raise ValueError("limit must be >= 0")
    if hasattr(args, "balance") and args.balance < 0:
        raise ValueError("balance must be >= 0")
    if getattr(args, "command", None) == "loop":
        if args.sleep_seconds < 0:
            raise ValueError("sleep_seconds must be >= 0")
        if args.iterations < 0:
            raise ValueError("iterations must be >= 0")


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, allow_nan=False)


def _build_service(fixture: str | None) -> TradingService:
    if not fixture:
        return TradingService()
    binance, polymarket, chainlink = load_fixture_clients(Path(fixture))
    return TradingService(binance=binance, polymarket=polymarket, chainlink=chainlink)


if __name__ == "__main__":
    raise SystemExit(main())
