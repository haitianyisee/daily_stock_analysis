#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import elp1030_5y_backtest_v2 as core


def concat_files(root: Path, name: str) -> pd.DataFrame:
    frames = []
    for p in sorted(root.glob(f"elp1030-part-*/{name}")):
        try:
            df = pd.read_csv(p)
            if len(df) or len(df.columns):
                frames.append(df)
        except Exception as e:
            print(f"WARN cannot read {p}: {e}", flush=True)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="part_artifacts")
    ap.add_argument("--output", default="elp1030_results")
    args = ap.parse_args()

    root = Path(args.input)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    cand = concat_files(root, "daily_touch_candidates.csv")
    detail = concat_files(root, "event_reconstruction_all.csv")
    selected = concat_files(root, "selected_trades.csv")
    failures = concat_files(root, "daily_scan_failures.csv")
    universe = concat_files(root, "universe.csv")

    dup = 0
    if not selected.empty and {"date", "code"}.issubset(selected.columns):
        dup = int(selected.duplicated(["date", "code"]).sum())
        selected = selected.drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)

    for df, name in [
        (cand, "daily_touch_candidates.csv"),
        (detail, "event_reconstruction_all.csv"),
        (selected, "selected_trades.csv"),
        (failures, "daily_scan_failures.csv"),
        (universe, "universe.csv"),
    ]:
        df.to_csv(out / name, index=False, encoding="utf-8-sig")

    if selected.empty:
        raise RuntimeError("v4 parts produced zero selected trades")

    summary = core.summarize(selected, out)

    if len(detail) and "date" in detail and "data_ok" in detail:
        detail["year"] = detail["date"].astype(str).str[:4]
        detail.groupby("year")["data_ok"].agg(["count", "sum", "mean"]).reset_index().to_csv(
            out / "reconstruction_coverage_by_year.csv", index=False, encoding="utf-8-sig"
        )

    part_summaries = []
    for p in sorted(root.glob("elp1030-part-*/part_summary.json")):
        try:
            part_summaries.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            part_summaries.append({"file": str(p), "error": repr(e)})

    meta = {
        "engine": "ELP1030_Core_5Y_v4_period_stock_shard",
        "generated_utc": datetime.utcnow().isoformat(),
        "strategy_logic": "unchanged from v2; only execution partitioning changed",
        "parts_received": len(part_summaries),
        "universe_rows_raw_across_parts": int(len(universe)),
        "daily_touch_candidates": int(len(cand)),
        "reconstructed_rows": int(len(detail)),
        "selected_trades": int(len(selected)),
        "selected_dates": int(selected["date"].nunique()),
        "duplicate_date_code_removed": dup,
        "scan_failure_rows": int(len(failures)),
        "summary": summary,
        "part_summaries": part_summaries,
    }

    with open(out / "run_summary.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=str)

    print("===FINAL_RUN_SUMMARY===", flush=True)
    print(json.dumps(meta, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
