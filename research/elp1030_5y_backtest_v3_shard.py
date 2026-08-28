#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parallel shard worker for ELP-1030 v2. Strategy logic is imported unchanged."""
from __future__ import annotations
import argparse, json
from datetime import datetime
from pathlib import Path
import pandas as pd
import baostock as bs
import elp1030_5y_backtest_v2 as core


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='20210809')
    ap.add_argument('--end', default='20260807')
    ap.add_argument('--shard-index', type=int, required=True)
    ap.add_argument('--shard-total', type=int, required=True)
    args = ap.parse_args()
    if not (0 <= args.shard_index < args.shard_total):
        raise ValueError('invalid shard index')

    out = Path(f'elp1030_shard_{args.shard_index}')
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        'engine': 'ELP1030_Core_5Y_v3_parallel', 'start': args.start, 'end': args.end,
        'shard_index': args.shard_index, 'shard_total': args.shard_total,
        'generated_utc': datetime.utcnow().isoformat(),
        'strategy_logic': 'unchanged from elp1030_5y_backtest_v2.py'
    }

    lg = bs.login()
    meta['login_code'], meta['login_msg'] = lg.error_code, lg.error_msg
    if lg.error_code != '0':
        raise RuntimeError(f'BaoStock login failed: {lg.error_code} {lg.error_msg}')
    try:
        cal = core.trade_calendar(args.start, args.end)
        univ = core.stock_universe(args.start, args.end, False).reset_index(drop=True)
        shard = univ.iloc[args.shard_index::args.shard_total].copy().reset_index(drop=True)
        meta['universe_total'] = len(univ)
        meta['universe_shard'] = len(shard)
        shard.to_csv(out / 'universe.csv', index=False, encoding='utf-8-sig')

        cand = core.discover_candidates(shard, args.start, args.end, out)
        meta['daily_touch_candidates'] = len(cand)
        prev_map = {cal[i]: cal[i-1] if i else None for i in range(len(cal))}
        next_map = {cal[i]: cal[i+1] if i+1 < len(cal) else None for i in range(len(cal))}
        if not cand.empty:
            cand = cand[cand['date'].map(prev_map).notna() & cand['date'].map(next_map).notna()].copy()

        recs = []
        for i, (_, row) in enumerate(cand.iterrows(), 1):
            recs.append(core.reconstruct(row, prev_map[row['date']], next_map[row['date']]))
            if i % 50 == 0 or i == len(cand):
                print(f'shard {args.shard_index}: 5m reconstruction {i}/{len(cand)}', flush=True)
        detail = pd.DataFrame(recs)
        detail.to_csv(out / 'event_reconstruction_all.csv', index=False, encoding='utf-8-sig')

        if detail.empty:
            selected = pd.DataFrame()
            meta['reconstruction_ok'] = 0
            meta['reconstruction_failures'] = 0
        else:
            ok = detail[detail['data_ok'] == True].copy()
            meta['reconstruction_ok'] = len(ok)
            meta['reconstruction_failures'] = int(len(detail) - len(ok))
            selected = ok[
                (ok['first_hit_le_1020'] == True)
                & (ok['at_limit_1025'] == True)
                & (ok['break_runs_5m_to_1025'] <= 1)
                & (ok['amount_1025'] >= 100_000_000.0)
            ].copy()
        selected.to_csv(out / 'selected_trades.csv', index=False, encoding='utf-8-sig')
        meta['selected_trades'] = len(selected)
        meta['selected_dates'] = int(selected['date'].nunique()) if len(selected) else 0
        with open(out / 'shard_summary.json', 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
        print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
    finally:
        bs.logout()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
