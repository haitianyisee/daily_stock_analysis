#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ELP-1030 core five-year backtest.

Frozen research rules for this run:
- Shanghai/Shenzhen main-board ordinary A shares only
- exclude ST names and ChiNext/STAR/BSE by code
- upper-limit price <= CNY 30
- prior trading day did NOT close limit-up (first-board definition)
- first limit hit <= 10:20
- 10:25 five-minute bar closes at the official upper limit
- detected break episodes through 10:25 <= 1 (5m approximation)
- cumulative traded amount through 10:25 >= CNY 100m
- D0 opening return in [-2%, +7%]
- buy at upper-limit price at 10:25, assume 100% fill
- strict T+1; report D1 open/09:35/09:45/10:00/10:30/11:30/15:00

Data:
- Eastmoney historical limit-up + broken-board pools for event discovery
- BaoStock unadjusted 5-minute bars for D-1/D0/D1 reconstruction

Important: the break count is necessarily a 5-minute observable approximation; intrabar
open/reseal cycles can be missed. This is explicitly recorded in outputs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

try:
    import baostock as bs
except Exception:
    bs = None

UT = "7eea3edcaed734bea9cbfc24409ed989"
ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
ZB_URL = "https://push2ex.eastmoney.com/getTopicZBPool"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/ztb/detail",
}
TARGET_TIMES = ["09:30", "09:35", "09:45", "10:00", "10:30", "11:30", "15:00"]


def round_cent(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def upper_limit_from_prev(prev_close: float) -> float:
    return round_cent(Decimal(str(prev_close)) * Decimal("1.10"))


def lower_limit_from_close(close: float) -> float:
    return round_cent(Decimal(str(close)) * Decimal("0.90"))


def hhmmss_num(v: Any) -> Optional[int]:
    if v in (None, "", "-", 0, "0"):
        return None
    s = re.sub(r"\D", "", str(v))
    if not s:
        return None
    s = s.zfill(6)[-6:]
    try:
        return int(s)
    except Exception:
        return None


def is_main_board(code: str) -> bool:
    code = str(code).zfill(6)
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def bs_code(code: str) -> str:
    return ("sh." if code.startswith("6") else "sz.") + code


def parse_json_or_jsonp(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    a, b = text.find("("), text.rfind(")")
    if a >= 0 and b > a:
        return json.loads(text[a + 1 : b])
    raise ValueError(f"unrecognized payload prefix: {text[:80]!r}")


def fetch_pool(date: str, kind: str, session: requests.Session, retries: int = 5) -> List[Dict[str, Any]]:
    url = ZT_URL if kind == "U" else ZB_URL
    params = {
        "ut": UT,
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 5000,
        "sort": "fbt:asc",
        "date": date,
        "_": int(time.time() * 1000),
    }
    # JSONP is historically more reliable for ZB endpoint.
    if kind == "Z":
        params["cb"] = "callbackdata9664180"
    err = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=20)
            if r.status_code != 200 or not r.text.strip():
                raise RuntimeError(f"HTTP {r.status_code}, empty={not bool(r.text.strip())}")
            payload = parse_json_or_jsonp(r.text)
            body = payload.get("data")
            if body is None:
                return []
            pool = body.get("pool", body.get("diff", [])) or []
            return pool
        except Exception as e:
            err = e
            time.sleep(min(0.5 * (2**attempt), 5.0))
    raise RuntimeError(f"pool fetch failed {date} {kind}: {err}")


def get_trade_dates(start_date: str, end_date: str) -> List[str]:
    if bs is None:
        raise RuntimeError("baostock import failed")
    rs = bs.query_trade_dates(start_date=f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}",
                              end_date=f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}")
    out = []
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        if len(row) >= 2 and row[1] == "1":
            out.append(row[0].replace("-", ""))
    if not out:
        raise RuntimeError(f"trade calendar empty: {rs.error_code} {rs.error_msg}")
    return out


def pool_to_event(item: Dict[str, Any], date: str, status: str) -> Dict[str, Any]:
    code = str(item.get("c", "")).zfill(6)
    p_raw = item.get("p")
    try:
        pool_price = float(p_raw) / 1000.0
    except Exception:
        pool_price = np.nan
    return {
        "date": date,
        "status": status,
        "code": code,
        "name": str(item.get("n", "")),
        "first_time": hhmmss_num(item.get("fbt")),
        "last_time": hhmmss_num(item.get("lbt")),
        "pool_price": pool_price,
        "pool_change_pct": item.get("zdp"),
        "pool_amount": item.get("amount"),
        "pool_turnover": item.get("hs"),
        "pool_open_times_full_day": item.get("zbc"),
        "pool_continuous": item.get("lbc"),
        "industry": str(item.get("hybk", "")),
    }


def discover_events(trade_dates: List[str], out_dir: Path, probe: bool = False) -> Tuple[pd.DataFrame, Dict[str, set]]:
    sess = requests.Session()
    rows: List[Dict[str, Any]] = []
    zt_by_date: Dict[str, set] = {}
    failures = []
    dates = trade_dates[:3] if probe else trade_dates
    for i, d in enumerate(dates, 1):
        try:
            u = fetch_pool(d, "U", sess)
            z = fetch_pool(d, "Z", sess)
        except Exception as e:
            failures.append({"date": d, "error": repr(e)})
            u, z = [], []
        zt_by_date[d] = {str(x.get("c", "")).zfill(6) for x in u}
        rows.extend(pool_to_event(x, d, "U") for x in u)
        rows.extend(pool_to_event(x, d, "Z") for x in z)
        if i % 25 == 0 or i == len(dates):
            print(f"event discovery {i}/{len(dates)} rows={len(rows)} failures={len(failures)}", flush=True)
        time.sleep(0.04)
    pd.DataFrame(failures).to_csv(out_dir / "event_fetch_failures.csv", index=False)
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No Eastmoney limit events discovered; cannot backtest")
    df.to_csv(out_dir / "event_pool_raw.csv", index=False, encoding="utf-8-sig")
    return df, zt_by_date


def filter_pre_minute(events: pd.DataFrame, trade_dates: List[str], zt_by_date: Dict[str, set]) -> pd.DataFrame:
    prev_map = {trade_dates[i]: trade_dates[i - 1] if i > 0 else None for i in range(len(trade_dates))}
    x = events.copy()
    x = x[x["code"].map(is_main_board)]
    x = x[~x["name"].str.upper().str.contains("ST", na=False)]
    x = x[x["first_time"].notna() & (x["first_time"] <= 102000)]
    # Avoid obvious new/no-limit securities: a genuine normal main-board limit event should be ~10%.
    x["pool_change_pct_num"] = pd.to_numeric(x["pool_change_pct"], errors="coerce")
    x = x[x["pool_change_pct_num"].between(9.4, 10.7) | (x["status"] == "Z")]

    def prior_closed_limit(row: pd.Series) -> bool:
        p = prev_map.get(row["date"])
        return bool(p and row["code"] in zt_by_date.get(p, set()))

    x["prior_day_closed_limit"] = x.apply(prior_closed_limit, axis=1)
    x = x[~x["prior_day_closed_limit"]]
    x = x.drop_duplicates(["date", "code"], keep="first").sort_values(["date", "first_time", "code"])
    return x


def query_bs_bars(code: str, prev_d: str, d0: str, d1: str) -> pd.DataFrame:
    fields = "date,time,code,open,high,low,close,volume,amount,adjustflag"
    rs = bs.query_history_k_data_plus(
        bs_code(code), fields,
        start_date=f"{prev_d[:4]}-{prev_d[4:6]}-{prev_d[6:]}",
        end_date=f"{d1[:4]}-{d1[4:6]}-{d1[6:]}",
        frequency="5", adjustflag="3"
    )
    data = []
    while rs.error_code == "0" and rs.next():
        data.append(rs.get_row_data())
    if rs.error_code != "0":
        raise RuntimeError(f"BaoStock minute query error {rs.error_code}: {rs.error_msg}")
    if not data:
        return pd.DataFrame(columns=fields.split(","))
    df = pd.DataFrame(data, columns=rs.fields)
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date8"] = df["date"].str.replace("-", "", regex=False)
    df["hhmm"] = df["time"].astype(str).str.slice(8, 12)
    return df


def bar_close_at(day: pd.DataFrame, hhmm: str) -> float:
    v = day.loc[day["hhmm"] == hhmm.replace(":", ""), "close"]
    return float(v.iloc[-1]) if len(v) else np.nan


def analyze_event(row: pd.Series, prev_d: str, d1: str) -> Dict[str, Any]:
    out = row.to_dict()
    out.update({"prev_date": prev_d, "next_date": d1, "data_ok": False, "error": ""})
    try:
        bars = query_bs_bars(row["code"], prev_d, row["date"], d1)
        if bars.empty:
            raise RuntimeError("empty 5m bars")
        pday = bars[bars["date8"] == prev_d]
        d0 = bars[bars["date8"] == row["date"]]
        nd = bars[bars["date8"] == d1]
        if pday.empty or d0.empty or nd.empty:
            raise RuntimeError(f"missing day bars prev={len(pday)} d0={len(d0)} d1={len(nd)}")

        prev_close = float(pday.iloc[-1]["close"])
        limit_price = upper_limit_from_prev(prev_close)
        d0_open = float(d0.iloc[0]["open"])
        open_ret = d0_open / prev_close - 1.0
        d0_1025 = d0[d0["hhmm"] <= "1025"].copy()
        if d0_1025.empty:
            raise RuntimeError("no bars through 10:25")
        close_1025 = bar_close_at(d0, "10:25")
        at_limit_1025 = bool(np.isfinite(close_1025) and abs(close_1025 - limit_price) <= 0.0051)
        amount_1025 = float(d0_1025["amount"].fillna(0).sum())

        # Observable 5m break episodes after first close/high reaches the upper limit.
        # This can undercount sub-5-minute breaks and is reported as an approximation.
        states = []
        touched = False
        for _, b in d0_1025.iterrows():
            if float(b["high"]) >= limit_price - 0.0051:
                touched = True
            if touched:
                states.append(abs(float(b["close"]) - limit_price) <= 0.0051)
        breaks = 0
        if states:
            for a, b in zip(states[:-1], states[1:]):
                if a and not b:
                    breaks += 1

        d0_close = float(d0.iloc[-1]["close"])
        d1_lower = lower_limit_from_close(d0_close)
        first_nd = nd.iloc[0]
        d1_open = float(first_nd["open"])

        out.update({
            "prev_close": prev_close,
            "limit_price": limit_price,
            "d0_open": d0_open,
            "open_ret": open_ret,
            "close_1025": close_1025,
            "at_limit_1025": at_limit_1025,
            "amount_1025": amount_1025,
            "breaks_5m_to_1025": breaks,
            "d0_close": d0_close,
            "d1_lower_limit": d1_lower,
            "d1_open": d1_open,
        })

        # Fixed-time raw returns and conservative locked-limit-down flag.
        target_prices = {"09:30": d1_open}
        for t in TARGET_TIMES[1:]:
            target_prices[t] = bar_close_at(nd, t)
        for t, px in target_prices.items():
            key = t.replace(":", "")
            out[f"d1_{key}_price"] = px
            out[f"ret_{key}"] = (px / limit_price - 1.0) if np.isfinite(px) else np.nan
            if t == "09:30":
                # At exact open, treat an open at lower-limit as potentially locked.
                locked = abs(d1_open - d1_lower) <= 0.0051 and abs(float(first_nd["high"]) - d1_lower) <= 0.0051
            else:
                hhmm = t.replace(":", "")
                prior = nd[nd["hhmm"] <= hhmm]
                locked = (not prior.empty) and bool(
                    np.all(np.abs(prior["high"].to_numpy(dtype=float) - d1_lower) <= 0.0051)
                    and np.all(np.abs(prior["low"].to_numpy(dtype=float) - d1_lower) <= 0.0051)
                )
            out[f"locked_{key}"] = locked
        out["data_ok"] = True
    except Exception as e:
        out["error"] = repr(e)
    return out


def bootstrap_ci(values: np.ndarray, n_boot: int = 3000, seed: int = 20260828) -> Tuple[float, float]:
    v = values[np.isfinite(values)]
    if len(v) < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(v, size=len(v), replace=True).mean()
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def max_drawdown(daily_returns: pd.Series) -> float:
    if daily_returns.empty:
        return np.nan
    equity = (1.0 + daily_returns.fillna(0)).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def summarize(selected: pd.DataFrame, out_dir: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "n_events": int(len(selected)),
        "n_dates": int(selected["date"].nunique()) if len(selected) else 0,
        "method_note": "D0 10:25/break count reconstructed from unadjusted BaoStock 5-minute bars; break count may miss intrabar open/reseal cycles.",
    }
    metric_rows = []
    yearly_rows = []
    daily_frames = {}
    for t in TARGET_TIMES:
        k = t.replace(":", "")
        col = f"ret_{k}"
        vals = pd.to_numeric(selected[col], errors="coerce") if col in selected else pd.Series(dtype=float)
        valid = vals.dropna()
        ci = bootstrap_ci(valid.to_numpy()) if len(valid) else (np.nan, np.nan)
        locked = selected.get(f"locked_{k}", pd.Series(False, index=selected.index)).fillna(False).astype(bool)
        metric_rows.append({
            "exit": t,
            "n": int(valid.size),
            "mean": float(valid.mean()) if len(valid) else np.nan,
            "median": float(valid.median()) if len(valid) else np.nan,
            "win_rate": float((valid > 0).mean()) if len(valid) else np.nan,
            "p05": float(valid.quantile(0.05)) if len(valid) else np.nan,
            "p25": float(valid.quantile(0.25)) if len(valid) else np.nan,
            "p75": float(valid.quantile(0.75)) if len(valid) else np.nan,
            "p95": float(valid.quantile(0.95)) if len(valid) else np.nan,
            "ci95_low": ci[0], "ci95_high": ci[1],
            "locked_limit_down_rate": float(locked.mean()) if len(locked) else np.nan,
        })
        tmp = selected[["date", col]].copy().dropna()
        if not tmp.empty:
            dr = tmp.groupby("date")[col].mean().sort_index()
            daily_frames[t] = dr
            metric_rows[-1]["date_equal_weight_mean"] = float(dr.mean())
            metric_rows[-1]["date_equal_weight_win_rate"] = float((dr > 0).mean())
            metric_rows[-1]["compound_total"] = float((1 + dr).prod() - 1)
            metric_rows[-1]["max_drawdown"] = max_drawdown(dr)
        else:
            metric_rows[-1].update({"date_equal_weight_mean": np.nan, "date_equal_weight_win_rate": np.nan,
                                    "compound_total": np.nan, "max_drawdown": np.nan})

        if len(selected):
            work = selected[["date", col]].copy()
            work["year"] = work["date"].str[:4]
            for yr, g in work.groupby("year"):
                vv = pd.to_numeric(g[col], errors="coerce").dropna()
                yearly_rows.append({"year": yr, "exit": t, "n": int(len(vv)),
                                    "mean": float(vv.mean()) if len(vv) else np.nan,
                                    "median": float(vv.median()) if len(vv) else np.nan,
                                    "win_rate": float((vv > 0).mean()) if len(vv) else np.nan})

    metrics = pd.DataFrame(metric_rows)
    yearly = pd.DataFrame(yearly_rows)
    metrics.to_csv(out_dir / "metrics.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(out_dir / "yearly.csv", index=False, encoding="utf-8-sig")
    if daily_frames:
        pd.DataFrame(daily_frames).to_csv(out_dir / "daily_portfolio_returns.csv", encoding="utf-8-sig")
    result["metrics"] = metric_rows
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20210809")
    ap.add_argument("--end", default="20260807")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--max-events", type=int, default=0)
    args = ap.parse_args()

    out_dir = Path("elp1030_results")
    out_dir.mkdir(exist_ok=True)
    run_meta = {"start": args.start, "end": args.end, "probe": args.probe, "generated_utc": datetime.utcnow().isoformat()}

    if bs is None:
        raise RuntimeError("baostock package unavailable")
    lg = bs.login()
    run_meta["baostock_login_code"] = lg.error_code
    run_meta["baostock_login_msg"] = lg.error_msg
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {lg.error_code} {lg.error_msg}")

    try:
        trade_dates = get_trade_dates(args.start, args.end)
        if args.probe:
            # Use a known ordinary trading-day window for endpoint/data sanity.
            trade_dates = [d for d in trade_dates if d >= "20240108" and d <= "20240112"] or trade_dates[:5]
        run_meta["trade_dates"] = len(trade_dates)
        run_meta["first_trade_date"] = trade_dates[0]
        run_meta["last_trade_date"] = trade_dates[-1]

        events, zt_by_date = discover_events(trade_dates, out_dir, probe=False)
        pre = filter_pre_minute(events, trade_dates, zt_by_date)
        pre.to_csv(out_dir / "candidates_pre_minute.csv", index=False, encoding="utf-8-sig")
        run_meta["events_raw"] = len(events)
        run_meta["candidates_pre_minute"] = len(pre)

        prev_map = {trade_dates[i]: trade_dates[i - 1] if i > 0 else None for i in range(len(trade_dates))}
        next_map = {trade_dates[i]: trade_dates[i + 1] if i + 1 < len(trade_dates) else None for i in range(len(trade_dates))}
        pre = pre[pre["date"].map(prev_map).notna() & pre["date"].map(next_map).notna()].copy()
        if args.max_events > 0:
            pre = pre.head(args.max_events)

        analyzed = []
        for i, (_, row) in enumerate(pre.iterrows(), 1):
            d = row["date"]
            analyzed.append(analyze_event(row, prev_map[d], next_map[d]))
            if i % 50 == 0 or i == len(pre):
                print(f"minute reconstruction {i}/{len(pre)}", flush=True)
        detail = pd.DataFrame(analyzed)
        detail.to_csv(out_dir / "event_reconstruction_all.csv", index=False, encoding="utf-8-sig")

        if detail.empty:
            raise RuntimeError("No reconstructed events")
        ok = detail[detail["data_ok"] == True].copy()
        run_meta["reconstructed_ok"] = len(ok)
        run_meta["reconstruction_failures"] = int(len(detail) - len(ok))

        selected = ok[
            (ok["limit_price"] <= 30.0)
            & (ok["at_limit_1025"] == True)
            & (ok["breaks_5m_to_1025"] <= 1)
            & (ok["amount_1025"] >= 100_000_000.0)
            & (ok["open_ret"] >= -0.02)
            & (ok["open_ret"] <= 0.07)
        ].copy()
        selected.to_csv(out_dir / "selected_trades.csv", index=False, encoding="utf-8-sig")
        run_meta["selected_trades"] = len(selected)
        run_meta["selected_dates"] = int(selected["date"].nunique()) if len(selected) else 0
        summary = summarize(selected, out_dir)
        run_meta["summary"] = summary

        # Coverage/failure diagnostics by year.
        if len(detail):
            detail["year"] = detail["date"].str[:4]
            cov = detail.groupby("year")["data_ok"].agg(["count", "sum", "mean"]).reset_index()
            cov.to_csv(out_dir / "reconstruction_coverage_by_year.csv", index=False, encoding="utf-8-sig")

        with open(out_dir / "run_summary.json", "w", encoding="utf-8") as f:
            json.dump(run_meta, f, ensure_ascii=False, indent=2, default=str)
        print(json.dumps(run_meta, ensure_ascii=False, indent=2, default=str), flush=True)
    finally:
        bs.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
