# -*- coding: utf-8 -*-
"""高容错入口：固定交易日、延长数据到2026-07-21，并保证分片异常也能产出JSON。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import july_tail_backtest_sharded as impl

TRADE_DATES = [
    "2026-07-01", "2026-07-02", "2026-07-03",
    "2026-07-06", "2026-07-07", "2026-07-08",
    "2026-07-09", "2026-07-10", "2026-07-13",
    "2026-07-14", "2026-07-15", "2026-07-16",
    "2026-07-17", "2026-07-20", "2026-07-21",
]

impl.END_TEST = "20260721"
impl.fetch_trade_dates = lambda: TRADE_DATES.copy()

_original_command_shard = impl.command_shard


def safe_command_shard(universe_file: Path, index: int, count: int, out_file: Path, workers: int) -> None:
    try:
        _original_command_shard(universe_file, index, count, out_file, workers)
    except Exception as exc:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(
            json.dumps(
                {
                    "shard_index": index,
                    "shard_count": count,
                    "subset_size": 0,
                    "trade_dates": TRADE_DATES,
                    "candidates": [],
                    "errors": [],
                    "fatal_error": str(exc),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"shard={index} fatal={exc}", flush=True)


impl.command_shard = safe_command_shard

if __name__ == "__main__":
    sys.exit(impl.main())
