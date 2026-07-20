# -*- coding: utf-8 -*-
"""2026年7月A股14:55尾盘策略：分片回测与结果合并。"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import math
import os
import random
import statistics
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests

UT = "fa5fd1943c7b386f172d6893dbfba10b"
START_FETCH = "20260620"
START_TEST = "20260701"
END_TEST = "20260720"
CAPITAL = 10000.0
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
STAMP_RATE = 0.0005
TRANSFER_RATE = 0.00001
_tls = threading.local()


def session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        })
        _tls.session = s
    return s


def get_json(url: str, params: dict[str, Any], retries: int = 6) -> dict[str, Any]:
    last: Exception | None = None
    for i in range(retries):
        try:
            time.sleep(random.uniform(0.03, 0.15))
            r = session().get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise RuntimeError("non-dict response")
            return data
        except Exception as exc:
            last = exc
            time.sleep(min(0.5 * (2 ** i), 6.0) + random.uniform(0, 0.4))
    raise RuntimeError(f"request failed: {url}: {last}")


def secid(code: str) -> str:
    return ("1." if code.startswith(("600", "601", "603", "605", "688", "689")) else "0.") + code


def is_main(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "000", "001", "002"))


def valid_name(name: str) -> bool:
    upper = name.upper()
    return not any(x in upper for x in ("ST", "退", "ETF", "基金", "指数"))


def lot(price: float) -> int:
    return int(CAPITAL // (price * 100) * 100) if price > 0 else 0


def fetch_universe_eastmoney() -> list[dict[str, str]]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    fields = "f12,f14"
    rows_all: list[dict[str, Any]] = []
    for fs in ("m:1+t:2", "m:0+t:6"):
        params = {
            "pn": 1, "pz": 5000, "po": 1, "np": 1, "ut": UT,
            "fltt": 2, "invt": 2, "fid": "f3", "fs": fs, "fields": fields,
        }
        js = get_json(url, params, retries=8)
        diff = (js.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        rows_all.extend(diff)

    found: dict[str, str] = {}
    for row in rows_all:
        code = str(row.get("f12", "")).zfill(6)
        name = str(row.get("f14", ""))
        if is_main(code) and valid_name(name):
            found[code] = name
    return [{"code": c, "name": found[c]} for c in sorted(found)]


def fetch_universe_baostock() -> list[dict[str, str]]:
    try:
        import baostock as bs
    except Exception as exc:
        raise RuntimeError(f"baostock unavailable: {exc}") from exc

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_msg}")
    try:
        rs = bs.query_stock_basic()
        found: dict[str, str] = {}
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if len(row) < 6:
                continue
            raw_code, name, _ipo, _out, sec_type, status = row[:6]
            code = raw_code.split(".")[-1]
            if sec_type == "1" and status == "1" and is_main(code) and valid_name(name):
                found[code] = name
        if rs.error_code != "0":
            raise RuntimeError(rs.error_msg)
        return [{"code": c, "name": found[c]} for c in sorted(found)]
    finally:
        bs.logout()


def command_universe(out_file: Path) -> None:
    errors: list[str] = []
    universe: list[dict[str, str]] = []
    try:
        universe = fetch_universe_eastmoney()
    except Exception as exc:
        errors.append(f"eastmoney: {exc}")
    if len(universe) < 1000:
        try:
            universe = fetch_universe_baostock()
        except Exception as exc:
            errors.append(f"baostock: {exc}")
    if len(universe) < 1000:
        raise RuntimeError(f"股票池数量异常: {len(universe)}; errors={errors}")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps({"universe": universe, "errors": errors}, ensure_ascii=False), encoding="utf-8")
    print(f"universe={len(universe)}")


def fetch_5m(code: str) -> list[dict[str, Any]]:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid(code), "klt": 5, "fqt": 0,
        "beg": START_FETCH, "end": END_TEST,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": UT,
    }
    js = get_json(url, params, retries=4)
    lines = ((js.get("data") or {}).get("klines") or [])
    bars: list[dict[str, Any]] = []
    for line in lines:
        a = line.split(",")
        if len(a) < 11:
            continue
        try:
            bars.append({
                "time": a[0], "date": a[0][:10], "hm": a[0][11:16],
                "open": float(a[1]), "close": float(a[2]), "high": float(a[3]), "low": float(a[4]),
                "volume": float(a[5]), "amount": float(a[6]), "pct": float(a[8]), "turnover": float(a[10]),
            })
        except ValueError:
            continue
    return bars


def fetch_trade_dates() -> list[str]:
    for code in ("000001", "600000", "600519"):
        try:
            bars = fetch_5m(code)
            dates = sorted({b["date"] for b in bars if "2026-07-01" <= b["date"] <= "2026-07-20"})
            if len(dates) >= 2:
                return dates
        except Exception:
            continue
    raise RuntimeError("交易日读取失败")


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def analyze_stock(item: dict[str, str], trade_dates: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    code, name = item["code"], item["name"]
    try:
        bars = fetch_5m(code)
    except Exception as exc:
        return [], str(exc)
    if not bars:
        return [], "empty"

    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bar in bars:
        by_date[bar["date"]].append(bar)
    for day in by_date:
        by_date[day].sort(key=lambda x: x["time"])

    rows: list[dict[str, Any]] = []
    for i, day in enumerate(trade_dates[:-1]):
        if i == 0:
            continue
        prev_day, next_day = trade_dates[i - 1], trade_dates[i + 1]
        if day not in by_date or prev_day not in by_date or next_day not in by_date:
            continue
        today, prev, nxt = by_date[day], by_date[prev_day], by_date[next_day]
        minute = {b["hm"]: b for b in today}
        if not all(t in minute for t in ("14:45", "14:50", "14:55")):
            continue
        b45, b50, b55 = minute["14:45"], minute["14:50"], minute["14:55"]
        if min(b45["volume"], b50["volume"], b55["volume"]) <= 0:
            continue
        upto = [b for b in today if b["hm"] <= "14:55"]
        price = b55["close"]
        prev_close = prev[-1]["close"]
        if prev_close <= 0:
            continue
        pct = (price / prev_close - 1) * 100
        amount = sum(b["amount"] for b in upto)
        volume = sum(b["volume"] for b in upto)
        vwap = amount / (volume * 100) if volume > 0 else 0
        day_high = max(b["high"] for b in upto)
        day_low = min(b["low"] for b in upto)

        if not (3 <= price <= 100 and 2 <= pct <= 8.8 and amount >= 2e8 and vwap > 0 and price >= vwap):
            continue
        if day_high <= day_low:
            continue
        shares = lot(price)
        if shares < 100:
            continue

        tail_ret = (price / b45["open"] - 1) * 100 if b45["open"] > 0 else -99
        if tail_ret < -0.5:
            continue
        vwap_gap = (price / vwap - 1) * 100
        range_pos = (price - day_low) / (day_high - day_low)
        accel = (b50["amount"] + b55["amount"]) / max(2 * b45["amount"], 1.0)
        cap_util = shares * price / CAPITAL

        score = (
            clamp(25 - abs(pct - 5.5) * 4.2, 0, 25)
            + clamp(math.log1p(amount / 2e8) * 11, 0, 20)
            + clamp(5 + vwap_gap * 8, 0, 15)
            + clamp(9 + tail_ret * 14, 0, 20)
            + clamp((accel - 0.5) * 8, 0, 10)
            + clamp((range_pos - 0.45) * 18, 0, 10)
            + cap_util * 10
        )

        next_open = nxt[0]["open"]
        if next_open <= 0:
            continue
        gross_ret = next_open / price - 1
        buy_value = shares * price
        sell_value = shares * next_open
        buy_fee = max(MIN_COMMISSION, buy_value * COMMISSION_RATE) + buy_value * TRANSFER_RATE
        sell_fee = max(MIN_COMMISSION, sell_value * COMMISSION_RATE) + sell_value * (STAMP_RATE + TRANSFER_RATE)
        net_pnl = sell_value - buy_value - buy_fee - sell_fee

        rows.append({
            "date": day, "next_date": next_day, "code": code, "name": name,
            "buy_1455": round(price, 4), "next_open": round(next_open, 4), "shares": shares,
            "used_cash": round(buy_value, 2), "cash_left": round(CAPITAL - buy_value, 2),
            "pct_1455": round(pct, 3), "amount_1455": round(amount, 2), "vwap": round(vwap, 4),
            "vwap_gap_pct": round(vwap_gap, 3), "tail_ret_pct": round(tail_ret, 3),
            "range_pos": round(range_pos, 4), "accel": round(accel, 3), "score": round(score, 3),
            "gross_return_pct": round(gross_ret * 100, 3), "hit_1pct": gross_ret >= 0.01,
            "net_pnl": round(net_pnl, 2), "net_return_pct": round(net_pnl / CAPITAL * 100, 3),
        })
    return rows, None


def command_shard(universe_file: Path, index: int, count: int, out_file: Path, workers: int) -> None:
    payload = json.loads(universe_file.read_text(encoding="utf-8"))
    universe = payload["universe"]
    subset = [item for pos, item in enumerate(universe) if pos % count == index]
    trade_dates = fetch_trade_dates()
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(analyze_stock, item, trade_dates): item for item in subset}
        for done, future in enumerate(cf.as_completed(future_map), 1):
            item = future_map[future]
            try:
                rows, error = future.result()
                candidates.extend(rows)
                if error:
                    errors.append({"code": item["code"], "error": error})
            except Exception as exc:
                errors.append({"code": item["code"], "error": str(exc)})
            if done % 50 == 0:
                print(f"shard={index} processed={done}/{len(subset)}", flush=True)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps({
        "shard_index": index, "shard_count": count, "subset_size": len(subset),
        "trade_dates": trade_dates, "candidates": candidates, "errors": errors,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"shard={index} subset={len(subset)} candidates={len(candidates)} errors={len(errors)}")


def write_report(picks: list[dict[str, Any]], coverage: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "july_2026_tail_backtest.csv"
    fields = list(picks[0].keys()) if picks else ["date"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(picks)

    n = len(picks)
    hits = sum(bool(x["hit_1pct"]) for x in picks)
    positives = sum(x["net_pnl"] > 0 for x in picks)
    total_net = sum(x["net_pnl"] for x in picks)
    avg_gross = statistics.mean(x["gross_return_pct"] for x in picks) if picks else 0.0
    avg_net = statistics.mean(x["net_return_pct"] for x in picks) if picks else 0.0
    cumulative = peak = 0.0
    max_dd = 0.0
    for row in picks:
        cumulative += row["net_pnl"]
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)

    lines = [
        "# 2026年7月14:55尾盘策略回测",
        "",
        f"- 数据源：东方财富5分钟K线",
        f"- 分片完成度：{coverage['found_shards']}/{coverage['expected_shards']}",
        f"- 股票处理数：{coverage['processed_stocks']}",
        f"- 接口失败数：{coverage['error_count']}",
        f"- 已结算交易：**{n}笔**",
        f"- 次日开盘高于买点1%：**{hits}/{n} = {(hits / n * 100 if n else 0):.1f}%**",
        f"- 扣费后盈利交易：**{positives}/{n} = {(positives / n * 100 if n else 0):.1f}%**",
        f"- 平均隔夜毛收益：**{avg_gross:.3f}%**",
        f"- 平均单笔净收益：**{avg_net:.3f}%**",
        f"- 固定1万元累计净盈亏：**{total_net:.2f}元**",
        f"- 顺序累计最大回撤：**{max_dd:.2f}元**",
        "",
        "## 逐日结果",
        "",
        "|买入日|股票|代码|14:55买价|次日开盘|毛收益|≥1%|股数|净盈亏|评分|",
        "|---|---|---:|---:|---:|---:|:---:|---:|---:|---:|",
    ]
    for row in picks:
        lines.append(
            f"|{row['date']}|{row['name']}|{row['code']}|{row['buy_1455']:.2f}|{row['next_open']:.2f}|"
            f"{row['gross_return_pct']:.2f}%|{'是' if row['hit_1pct'] else '否'}|{row['shares']}|{row['net_pnl']:.2f}|{row['score']:.1f}|"
        )
    lines += [
        "", "## 口径限制", "",
        "1. 使用14:55对应5分钟K线收盘价，无法还原逐笔滑点和排队成交。",
        "2. 股票池为当前存续主板证券，存在生存者偏差。",
        "3. 未加入历史时点新闻和板块资讯快照，仅验证量价评分模型。",
        "4. 费用按佣金万三且最低5元、卖出印花税万五、过户费十万分之一计算。",
    ]
    if coverage["found_shards"] != coverage["expected_shards"]:
        lines.insert(3, "- **警告：分片不完整，本结果不可作为正式胜率。**")

    (out_dir / "july_2026_tail_backtest.md").write_text("\n".join(lines), encoding="utf-8")
    meta = {
        **coverage, "trades": n, "hits": hits,
        "hit_rate": hits / n if n else 0.0,
        "positive_rate": positives / n if n else 0.0,
        "avg_gross_return_pct": avg_gross, "avg_net_return_pct": avg_net,
        "total_net_pnl": round(total_net, 2), "max_drawdown_yuan": round(max_dd, 2),
    }
    (out_dir / "july_2026_tail_backtest_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(lines))


def command_merge(input_dir: Path, expected_shards: int, out_dir: Path) -> None:
    files = sorted(input_dir.rglob("shard_*.json"))
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    processed = 0
    errors = 0
    for file in files:
        payload = json.loads(file.read_text(encoding="utf-8"))
        processed += int(payload.get("subset_size", 0))
        errors += len(payload.get("errors", []))
        for row in payload.get("candidates", []):
            by_date[row["date"]].append(row)
    picks = [max(by_date[day], key=lambda x: x["score"]) for day in sorted(by_date) if by_date[day]]
    coverage = {
        "expected_shards": expected_shards, "found_shards": len(files),
        "processed_stocks": processed, "error_count": errors,
    }
    write_report(picks, coverage, out_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_uni = sub.add_parser("universe")
    p_uni.add_argument("--out", required=True, type=Path)

    p_shard = sub.add_parser("shard")
    p_shard.add_argument("--universe", required=True, type=Path)
    p_shard.add_argument("--index", required=True, type=int)
    p_shard.add_argument("--count", required=True, type=int)
    p_shard.add_argument("--out", required=True, type=Path)
    p_shard.add_argument("--workers", type=int, default=int(os.getenv("BACKTEST_WORKERS", "4")))

    p_merge = sub.add_parser("merge")
    p_merge.add_argument("--input", required=True, type=Path)
    p_merge.add_argument("--expected-shards", required=True, type=int)
    p_merge.add_argument("--out", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "universe":
        command_universe(args.out)
    elif args.command == "shard":
        command_shard(args.universe, args.index, args.count, args.out, args.workers)
    else:
        command_merge(args.input, args.expected_shards, args.out)


if __name__ == "__main__":
    main()
