#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ELP-1030 v4 worker: one date period x one stock shard.

Strategy logic is unchanged from v2. The only change is execution partitioning.
Calendar is extended around each target period so the first target trading day has D-1
and the last target trading day has D+1 for strict T+1 reconstruction.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import baostock as bs
import pandas as pd

import elp1030_5y_backtest_v2 as core


def shift_date8(d8: str, days: int) -> str:
    d = datetime.strptime(d8, "%Y%m%d") + timedelta(days=days)
    return d.strftime("%Y%m%d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--shard-index", type=int, required=True)
    ap.add_argument("--shard-total", type=int, required=True)
    args = ap.parse_args()

    if not (0 <= args.shard_index < args.shard_total):
        raise ValueError("invalid shard index")

    out = Path(f"elp1030_part_{args.label}_{args.shard_index}")
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "engine": "ELP1030_Core_5Y_v4_period_stock_shard",
        "target_start": args.start,
        "target_end": args.end,
        "period_label": args.label,
        "shard_index": args.shard_index,
        "shard_total": args.shard_total,
        "generated_utc": datetime.utcnow().isoformat(),
        "strategy_logic": "unchanged from elp1030_5y_backtest_v2.py",
    }

    lg = bs.login()
    meta["login_code"], meta["login_msg"] = lg.error_code, lg.error_msg
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {lg.error_code} {lg.error_msg}")

    try:
        cal_start = shift_date8(args.start, -20)
        cal_end = shift_date8(args.end, 20)
        cal = core.trade_calendar(cal_start, cal_end)
        target_cal = [d for d in cal if args.start <= d <= args.end]
        meta["target_trade_dates"] = len(target_cal)
        if not target_cal:
            raise RuntimeError("target period contains no trading days")

        univ = core.stock_universe(args.start, args.end, False).reset_index(drop=True)
        shard = univ.iloc[args.shard_index::args.shard_total].copy().reset_index(drop=True)
        meta["universe_total"] = len(univ)
        meta["universe_shard"] = len(shard)
        shard.to_csv(out / "universe.csv", index=False, encoding="utf-8-sig")

        # discover_candidates internally adds a D-1 buffer for first-board status and emits
        # candidates only inside [start,end].
        cand = core.discover_candidates(shard, args.start, args.end, out)
        meta["daily_touch_candidates"] = len(cand)

        prev_map = {cal[i]: cal[i - 1] if i else None for i in range(len(cal))}
        next_map = {cal[i]: cal[i + 1] if i + 1 < len(cal) else None for i in range(len(cal))}
        if not cand.empty:
            cand = cand[
                cand["date"].map(prev_map).notna()
                & cand["date"].map(next_map).notna()
                & cand["date"].between(args.start, args.end)
            ].copy()

        recs = []
        for i, (_, row) in enumerate(cand.iterrows(), 1):
            d = row["date"]
            recs.append(core.reconstruct(row, prev_map[d], next_map[d]))
            if i % 25 == 0 or i == len(cand):
                print(
                    f"period {args.label} shard {args.shard_index}: "
                    f"5m reconstruction {i}/{len(cand)}",
                    flush=True,
                )

        detail = pd.DataFrame(recs)
        detail.to_csv(out / "event_reconstruction_all.csv", index=False, encoding="utf-8-sig")

        if detail.empty:
            selected = pd.DataFrame()
            meta["reconstruction_ok"] = 0
            meta["reconstruction_failures"] = 0
        else:
            ok = detail[detail["data_ok"] == True].copy()
            meta["reconstruction_ok"] = len(ok)
            meta["reconstruction_failures"] = int(len(detail) - len(ok))
            selected = ok[
                (ok["first_hit_le_1020"] == True)
                & (ok["at_limit_1025"] == True)
                & (ok["break_runs_5m_to_1025"] <= 1)
                & (ok["amount_1025"] >= 100_000_000.0)
            ].copy()

        selected.to_csv(out / "selected_trades.csv", index=False, encoding="utf-8-sig")
        meta["selected_trades"] = len(selected)
        meta["selected_dates"] = int(selected["date"].nunique()) if len(selected) else 0

        with open(out / "part_summary.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
        print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
    finally:
        bs.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
