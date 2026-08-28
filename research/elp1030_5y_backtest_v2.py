#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ELP-1030 Core 5Y backtest v2 — no historical limit-pool dependency.

Signal is reconstructed only from point-in-time unadjusted BaoStock daily + 5-minute bars.
Daily bars discover EVERY normal main-board day whose intraday high touched the computed 10% upper
limit, including stocks that later broke the board. 5m bars then enforce the 10:25 cutoff.

Frozen core rules:
  * Shanghai/Shenzhen main board only: 600/601/603/605, 000/001/002/003
  * historical D0 isST == 0; normal trading
  * ordinary 10% limit regime only; no-limit/abnormal days rejected
  * upper-limit price <= CNY 30
  * first board: prior traded day did not close at its applicable upper limit
  * first 5m bar touching upper limit ends no later than 10:20
  * 10:25 5m close equals upper-limit price
  * observable 5m break episodes through 10:25 <= 1
  * cumulative amount through 10:25 >= CNY 100m
  * D0 open return in [-2%, +7%]
  * buy D0 at upper-limit price, research fill=100%; no D0 sale
  * T+1 mark-to-market exits: open, 09:35, 09:45, 10:00, 10:30, 11:30, 15:00

Precision note: first-hit/break logic is 5-minute observable, not tick precision. A sub-5-minute
open/reseal cycle can be missed. We report this explicitly rather than pretending tick accuracy.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import baostock as bs
import numpy as np
import pandas as pd

TARGET_TIMES = ["09:30", "09:35", "09:45", "10:00", "10:30", "11:30", "15:00"]
MAIN_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
PROBE_CODES = [
    "sz.002162", "sh.600716", "sz.003042", "sz.000532", "sz.002667",
    "sh.601086", "sh.600844", "sz.002326", "sh.600480", "sh.603798",
]


def cent(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_price(preclose: float, ratio: float = 0.10) -> float:
    return cent(Decimal(str(preclose)) * (Decimal("1") + Decimal(str(ratio))))


def is_main(code: str) -> bool:
    raw = code.split(".")[-1]
    return raw.startswith(MAIN_PREFIXES)


def rows_to_df(rs) -> pd.DataFrame:
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if rs.error_code != "0":
        raise RuntimeError(f"BaoStock error {rs.error_code}: {rs.error_msg}")
    return pd.DataFrame(rows, columns=rs.fields)


def trade_calendar(start8: str, end8: str) -> List[str]:
    rs = bs.query_trade_dates(
        start_date=f"{start8[:4]}-{start8[4:6]}-{start8[6:]}",
        end_date=f"{end8[:4]}-{end8[4:6]}-{end8[6:]}",
    )
    df = rows_to_df(rs)
    if df.empty:
        raise RuntimeError("empty trade calendar")
    return df.loc[df["is_trading_day"] == "1", "calendar_date"].str.replace("-", "", regex=False).tolist()


def stock_universe(start8: str, end8: str, probe: bool) -> pd.DataFrame:
    if probe:
        return pd.DataFrame({"code": PROBE_CODES, "code_name": [""] * len(PROBE_CODES),
                             "ipoDate": [""] * len(PROBE_CODES), "outDate": [""] * len(PROBE_CODES),
                             "type": ["1"] * len(PROBE_CODES), "status": ["1"] * len(PROBE_CODES)})
    df = rows_to_df(bs.query_stock_basic())
    if df.empty:
        raise RuntimeError("query_stock_basic returned no securities")
    df = df[(df["type"] == "1") & df["code"].map(is_main)].copy()
    # Keep currently delisted names when their life overlaps the research interval (avoids survivorship bias).
    s = pd.to_datetime(f"{start8[:4]}-{start8[4:6]}-{start8[6:]}")
    e = pd.to_datetime(f"{end8[:4]}-{end8[4:6]}-{end8[6:]}")
    ipo = pd.to_datetime(df["ipoDate"], errors="coerce")
    out = pd.to_datetime(df["outDate"], errors="coerce")
    df = df[(ipo.isna() | (ipo <= e)) & (out.isna() | (out >= s))].copy()
    return df


def daily_bars(code: str, start8: str, end8: str) -> pd.DataFrame:
    fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
    rs = bs.query_history_k_data_plus(
        code, fields,
        start_date=f"{start8[:4]}-{start8[4:6]}-{start8[6:]}",
        end_date=f"{end8[:4]}-{end8[4:6]}-{end8[6:]}",
        frequency="d", adjustflag="3",
    )
    df = rows_to_df(rs)
    if df.empty:
        return df
    for c in ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date8"] = df["date"].str.replace("-", "", regex=False)
    return df


def prior_closed_limit(prev_row: pd.Series) -> bool:
    try:
        if str(prev_row.get("tradestatus")) != "1":
            return False
        pc = float(prev_row["preclose"])
        cl = float(prev_row["close"])
        # If previous day was ST, its applicable historical main-board limit is 5%; otherwise 10%.
        ratio = 0.05 if str(prev_row.get("isST")) == "1" else 0.10
        lp = limit_price(pc, ratio)
        return abs(cl - lp) <= 0.0051
    except Exception:
        return False


def discover_candidates(universe: pd.DataFrame, start8: str, end8: str, out_dir: Path) -> pd.DataFrame:
    # Include calendar buffer so first-board status for the first research day is observable.
    start_dt = pd.to_datetime(f"{start8[:4]}-{start8[4:6]}-{start8[6:]}") - pd.Timedelta(days=20)
    scan_start = start_dt.strftime("%Y%m%d")
    found: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    n = len(universe)
    for i, r in universe.reset_index(drop=True).iterrows():
        code = r["code"]
        try:
            df = daily_bars(code, scan_start, end8)
            if len(df) < 2:
                continue
            for j in range(1, len(df)):
                cur = df.iloc[j]
                d = cur["date8"]
                if d < start8 or d > end8:
                    continue
                if str(cur["tradestatus"]) != "1" or str(cur["isST"]) != "0":
                    continue
                vals = [cur.get(x) for x in ["open", "high", "close", "preclose", "amount"]]
                if not all(pd.notna(x) and float(x) > 0 for x in vals[:4]):
                    continue
                pc = float(cur["preclose"])
                lp = limit_price(pc, 0.10)
                if lp > 30.0:
                    continue
                hi = float(cur["high"])
                # Normal ordinary-limit day must not trade above computed upper limit.
                # This rejects IPO/no-limit days and many ex-right/reference-price anomalies.
                if hi < lp - 0.0051 or hi > lp + 0.0051:
                    continue
                op = float(cur["open"])
                open_ret = op / pc - 1.0
                if open_ret < -0.02 or open_ret > 0.07:
                    continue
                # Necessary (non-leaky in final logic) query-reduction prefilter: if full-day amount <100m,
                # 10:25 cumulative amount cannot reach 100m.
                amt_day = float(cur["amount"]) if pd.notna(cur["amount"]) else 0.0
                if amt_day < 100_000_000.0:
                    continue
                prev = df.iloc[j - 1]
                if prior_closed_limit(prev):
                    continue
                found.append({
                    "date": d, "code": code, "code_name_basic": r.get("code_name", ""),
                    "preclose": pc, "limit_price": lp, "d0_open_daily": op,
                    "d0_high_daily": hi, "d0_close_daily": float(cur["close"]),
                    "d0_amount_daily": amt_day, "d0_turn_daily": cur.get("turn"),
                    "d0_pct_daily": cur.get("pctChg"), "d0_isST": cur.get("isST"),
                    "prev_date_stock": prev["date8"], "prev_close": float(prev["close"]),
                    "prev_preclose": float(prev["preclose"]) if pd.notna(prev["preclose"]) else np.nan,
                    "prev_isST": prev.get("isST"),
                })
        except Exception as e:
            failures.append({"code": code, "error": repr(e)})
        if (i + 1) % 100 == 0 or i + 1 == n:
            print(f"daily scan {i+1}/{n}; candidates={len(found)} failures={len(failures)}", flush=True)
    pd.DataFrame(failures).to_csv(out_dir / "daily_scan_failures.csv", index=False)
    out = pd.DataFrame(found)
    out.to_csv(out_dir / "daily_touch_candidates.csv", index=False, encoding="utf-8-sig")
    return out


def minute_bars(code: str, prev8: str, d08: str, next8: str) -> pd.DataFrame:
    fields = "date,time,code,open,high,low,close,volume,amount,adjustflag"
    rs = bs.query_history_k_data_plus(
        code, fields,
        start_date=f"{prev8[:4]}-{prev8[4:6]}-{prev8[6:]}",
        end_date=f"{next8[:4]}-{next8[4:6]}-{next8[6:]}",
        frequency="5", adjustflag="3",
    )
    df = rows_to_df(rs)
    if df.empty:
        return df
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date8"] = df["date"].str.replace("-", "", regex=False)
    # BaoStock time is YYYYMMDDHHMMSSmmm; bar-end HHMM sits at positions 8:12.
    df["hhmm"] = df["time"].astype(str).str.slice(8, 12)
    return df


def px_close(day: pd.DataFrame, hhmm: str) -> float:
    v = day.loc[day["hhmm"] == hhmm.replace(":", ""), "close"]
    return float(v.iloc[-1]) if len(v) else np.nan


def reconstruct(row: pd.Series, market_prev: str, market_next: str) -> Dict[str, Any]:
    out = row.to_dict()
    out.update({"market_prev": market_prev, "market_next": market_next, "data_ok": False, "error": ""})
    try:
        bars = minute_bars(row["code"], market_prev, row["date"], market_next)
        d0 = bars[bars["date8"] == row["date"]].copy()
        d1 = bars[bars["date8"] == market_next].copy()
        if d0.empty or d1.empty:
            raise RuntimeError(f"missing 5m bars d0={len(d0)} d1={len(d1)}")
        lp = float(row["limit_price"])
        d0_am = d0[d0["hhmm"] <= "1025"].copy()
        if d0_am.empty:
            raise RuntimeError("no D0 bars through 10:25")
        # Guard against a no-limit/reference-price anomaly at minute level too.
        if float(d0["high"].max()) > lp + 0.0051:
            raise RuntimeError("minute price traded above computed 10% limit")

        touch = d0_am[d0_am["high"] >= lp - 0.0051]
        first_hit = str(touch.iloc[0]["hhmm"]) if not touch.empty else ""
        first_hit_ok = bool(first_hit and first_hit <= "1020")
        c1025 = px_close(d0, "10:25")
        at_1025 = bool(np.isfinite(c1025) and abs(c1025 - lp) <= 0.0051)
        amount_1025 = float(d0_am["amount"].fillna(0).sum())

        # Break evidence after the first touch: count contiguous runs of bars whose LOW drops below
        # the upper-limit price. The first touch bar itself is excluded because it necessarily started
        # from below the limit in many legitimate seals. This is a 5m observable approximation.
        break_runs = 0
        if not touch.empty:
            first_idx = touch.index[0]
            after = d0_am.loc[d0_am.index > first_idx].copy()
            flags = (after["low"] < lp - 0.0051).tolist()
            prev_flag = False
            for flag in flags:
                if flag and not prev_flag:
                    break_runs += 1
                prev_flag = bool(flag)

        d1_open = float(d1.iloc[0]["open"])
        d0_close = float(row["d0_close_daily"])
        d1_lower = limit_price(d0_close, -0.10)
        out.update({
            "first_hit_5m_bar": first_hit,
            "first_hit_le_1020": first_hit_ok,
            "close_1025": c1025,
            "at_limit_1025": at_1025,
            "amount_1025": amount_1025,
            "break_runs_5m_to_1025": break_runs,
            "d1_lower_limit": d1_lower,
        })
        prices = {"09:30": d1_open}
        for t in TARGET_TIMES[1:]:
            prices[t] = px_close(d1, t)
        for t, px in prices.items():
            k = t.replace(":", "")
            out[f"d1_{k}_price"] = px
            out[f"ret_{k}"] = px / lp - 1.0 if np.isfinite(px) else np.nan
            if t == "09:30":
                b = d1.iloc[0]
                locked = abs(float(b["open"]) - d1_lower) <= 0.0051 and abs(float(b["high"]) - d1_lower) <= 0.0051
            else:
                prior = d1[d1["hhmm"] <= t.replace(":", "")]
                locked = bool(len(prior) and
                              np.all(np.abs(prior["high"].to_numpy(float) - d1_lower) <= 0.0051) and
                              np.all(np.abs(prior["low"].to_numpy(float) - d1_lower) <= 0.0051))
            out[f"locked_{k}"] = locked
        out["data_ok"] = True
    except Exception as e:
        out["error"] = repr(e)
    return out


def bootstrap_ci(v: np.ndarray, n: int = 3000, seed: int = 20260828) -> Tuple[float, float]:
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    m = np.empty(n)
    for i in range(n):
        m[i] = rng.choice(v, len(v), replace=True).mean()
    return float(np.quantile(m, 0.025)), float(np.quantile(m, 0.975))


def max_dd(ret: pd.Series) -> float:
    if ret.empty:
        return np.nan
    eq = (1 + ret.fillna(0)).cumprod()
    return float((eq / eq.cummax() - 1).min())


def summarize(sel: pd.DataFrame, out_dir: Path) -> Dict[str, Any]:
    metrics, years = [], []
    daily_cols: Dict[str, pd.Series] = {}
    for t in TARGET_TIMES:
        k = t.replace(":", "")
        col = f"ret_{k}"
        v = pd.to_numeric(sel[col], errors="coerce").dropna() if col in sel else pd.Series(dtype=float)
        lo, hi = bootstrap_ci(v.to_numpy()) if len(v) else (np.nan, np.nan)
        locked = sel.get(f"locked_{k}", pd.Series(False, index=sel.index)).fillna(False).astype(bool)
        rec = {
            "exit": t, "n": int(len(v)),
            "mean": float(v.mean()) if len(v) else np.nan,
            "median": float(v.median()) if len(v) else np.nan,
            "win_rate": float((v > 0).mean()) if len(v) else np.nan,
            "p05": float(v.quantile(.05)) if len(v) else np.nan,
            "p25": float(v.quantile(.25)) if len(v) else np.nan,
            "p75": float(v.quantile(.75)) if len(v) else np.nan,
            "p95": float(v.quantile(.95)) if len(v) else np.nan,
            "ci95_low": lo, "ci95_high": hi,
            "locked_limit_down_rate": float(locked.mean()) if len(locked) else np.nan,
        }
        tmp = sel[["date", col]].dropna() if col in sel else pd.DataFrame()
        if len(tmp):
            dr = tmp.groupby("date")[col].mean().sort_index()
            daily_cols[t] = dr
            rec.update({"date_equal_weight_mean": float(dr.mean()),
                        "date_equal_weight_win_rate": float((dr > 0).mean()),
                        "compound_total": float((1 + dr).prod() - 1),
                        "max_drawdown": max_dd(dr)})
        metrics.append(rec)
        if len(sel):
            w = sel[["date", col]].copy()
            w["year"] = w["date"].str[:4]
            for yr, g in w.groupby("year"):
                vv = pd.to_numeric(g[col], errors="coerce").dropna()
                years.append({"year": yr, "exit": t, "n": int(len(vv)),
                              "mean": float(vv.mean()) if len(vv) else np.nan,
                              "median": float(vv.median()) if len(vv) else np.nan,
                              "win_rate": float((vv > 0).mean()) if len(vv) else np.nan})
    pd.DataFrame(metrics).to_csv(out_dir / "metrics.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(years).to_csv(out_dir / "yearly.csv", index=False, encoding="utf-8-sig")
    if daily_cols:
        pd.DataFrame(daily_cols).to_csv(out_dir / "daily_portfolio_returns.csv", encoding="utf-8-sig")
    return {"n_events": int(len(sel)), "n_dates": int(sel["date"].nunique()) if len(sel) else 0, "metrics": metrics}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20210809")
    ap.add_argument("--end", default="20260807")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--max-events", type=int, default=0)
    args = ap.parse_args()
    if args.probe:
        args.start, args.end = "20240108", "20240112"

    out_dir = Path("elp1030_results")
    out_dir.mkdir(exist_ok=True)
    meta: Dict[str, Any] = {
        "engine": "ELP1030_Core_5Y_v2",
        "start": args.start, "end": args.end, "probe": args.probe,
        "generated_utc": datetime.utcnow().isoformat(),
        "data": "BaoStock unadjusted daily + 5-minute",
        "precision_note": "First-hit and board-break counts are 5-minute observable approximations, not tick precision.",
    }

    lg = bs.login()
    meta["login_code"], meta["login_msg"] = lg.error_code, lg.error_msg
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {lg.error_code} {lg.error_msg}")
    try:
        cal = trade_calendar(args.start, args.end)
        meta["trade_dates"] = len(cal)
        meta["first_trade_date"], meta["last_trade_date"] = cal[0], cal[-1]
        univ = stock_universe(args.start, args.end, args.probe)
        meta["universe_size"] = len(univ)
        univ.to_csv(out_dir / "universe.csv", index=False, encoding="utf-8-sig")
        cand = discover_candidates(univ, args.start, args.end, out_dir)
        meta["daily_touch_candidates"] = len(cand)
        if cand.empty:
            raise RuntimeError("daily scan found zero upper-limit touches")

        prev_map = {cal[i]: cal[i-1] if i else None for i in range(len(cal))}
        next_map = {cal[i]: cal[i+1] if i+1 < len(cal) else None for i in range(len(cal))}
        cand = cand[cand["date"].map(prev_map).notna() & cand["date"].map(next_map).notna()].copy()
        if args.max_events > 0:
            cand = cand.head(args.max_events)
        recs = []
        for i, (_, row) in enumerate(cand.iterrows(), 1):
            recs.append(reconstruct(row, prev_map[row["date"]], next_map[row["date"]]))
            if i % 50 == 0 or i == len(cand):
                print(f"5m reconstruction {i}/{len(cand)}", flush=True)
        detail = pd.DataFrame(recs)
        detail.to_csv(out_dir / "event_reconstruction_all.csv", index=False, encoding="utf-8-sig")
        ok = detail[detail["data_ok"] == True].copy()
        meta["reconstruction_ok"] = len(ok)
        meta["reconstruction_failures"] = int(len(detail) - len(ok))
        sel = ok[
            (ok["first_hit_le_1020"] == True)
            & (ok["at_limit_1025"] == True)
            & (ok["break_runs_5m_to_1025"] <= 1)
            & (ok["amount_1025"] >= 100_000_000.0)
        ].copy()
        sel.to_csv(out_dir / "selected_trades.csv", index=False, encoding="utf-8-sig")
        meta["selected_trades"] = len(sel)
        meta["selected_dates"] = int(sel["date"].nunique()) if len(sel) else 0
        meta["summary"] = summarize(sel, out_dir)
        if len(detail):
            detail["year"] = detail["date"].str[:4]
            detail.groupby("year")["data_ok"].agg(["count", "sum", "mean"]).reset_index().to_csv(
                out_dir / "reconstruction_coverage_by_year.csv", index=False, encoding="utf-8-sig")
        with open(out_dir / "run_summary.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
        print("===RUN_SUMMARY===", flush=True)
        print(json.dumps(meta, ensure_ascii=False, indent=2, default=str), flush=True)
    finally:
        bs.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
