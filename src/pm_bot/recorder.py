from __future__ import annotations

import json
from pathlib import Path

from pm_bot.models import PaperTradeRecord


class PaperTradeRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, trade: PaperTradeRecord) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trade.to_dict(), allow_nan=False) + "\n")
