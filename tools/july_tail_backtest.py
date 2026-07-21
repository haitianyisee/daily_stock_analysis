# -*- coding: utf-8 -*-
"""2026年7月A股14:55尾盘策略回测（东方财富5分钟行情）。"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import datetime as dt
import json
import math
import os
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
OUT = Path("backtest_output")
OUT.mkdir(parents=True, exist_ok=True)

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


def get_json(url: str, params: dict[str, Any], retries: int = 4) -> dict[str, Any]:
    err = None
    for i in range(retries):
        try:
            r = session().get(url, params=params, timeout=12)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
            time.sleep(0.4 * (2 ** i))
    raise RuntimeError(f"request failed: {url}: {err}")


def secid(code: str) -> str:
    return ("1." if code.startswith(("600", "601", "603", "605", "688", "689")) else "0.") + code


def is_main(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "000", "001", "002"))


def lot(price: float) -> int:
    return int(CAPITAL // (price * 100) * 100) if price > 0 else 0


def fetch_universe() -> list[dict[str, Any]]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    fields = "f12,f14,f2,f3,f6,f8,f15,f16,f17,f18"
    all_rows: list[dict[str, Any]] = []
    page, pz = 1, 500
    while True:
        p = {
            "pn": page, "pz": pz, "po": 1, "np": 1, "ut": UT, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": fields,
        }
        js = get_json(url, p)
        data = js.get("data") or {}
        rows = data.get("diff") or []
        all_rows.extend(rows)
        total = int(data.get("total") or 0)
        if not rows or len(all_rows) >= total:
            break
        page += 1
        if page > 20:
            break
    out = []
    for r in all_rows:
        code = str(r.get("f12", "")).zfill(6)
        name = str(r.get("f14", ""))
        if not is_main(code):
            continue
        if any(x in name.upper() for x in ("ST", "退", "ETF", "基金", "指数")):
            continue
        out.append({"code": code, "name": name})
    return out


def fetch_5m(code: str) -> list[dict[str, Any]]:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    p = {
        "secid": secid(code), "klt": 5, "fqt": 0, "beg": START_FETCH, "end": END_TEST,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "ut": UT,
    }
    js = get_json(url, p)
    lines = ((js.get("data") or {}).get("klines") or [])
    bars = []
    for x in lines:
        a = x.split(",")
        if len(a) < 11:
            continue
        try:
            bars.append({
                "time": a[0], "date": a[0][:10], "hm": a[0][11:16],
                "open": float(a[1]), "close": float(a[2]), "high": float(a[3]), "low": float(a[4]),
                "volume": float(a[5]), "amount": float(a[6]), "pct": float(a[8]), "turnover": float(a[10]),
            })
        except ValueError:
            pass
    return bars


def fetch_trade_dates() -> list[str]:
    bars = fetch_5m("000001")
    dates = sorted({b["date"] for b in bars if START_TEST[:4] + "-" + START_TEST[4:6] + "-" + START_TEST[6:] <= b["date"] <= END_TEST[:4] + "-" + END_TEST[4:6] + "-" + END_TEST[6:]})
    return dates


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def analyze_stock(item: dict[str, str], trade_dates: list[str]) -> tuple[str, str, list[dict[str, Any]], str | None]:
    code, name = item["code"], item["name"]
    try:
        bars = fetch_5m(code)
    except Exception as e:
        return code, name, [], str(e)
    if not bars:
        return code, name, [], "empty"

    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in bars:
        by_date[b["date"]].append(b)
    for d in by_date:
        by_date[d].sort(key=lambda z: z["time"])

    result = []
    for i, d in enumerate(trade_dates[:-1]):
        if d not in by_date or i == 0:
            continue
        prev_d = trade_dates[i - 1]
        next_d = trade_dates[i + 1]
        if prev_d not in by_date or next_d not in by_date:
            continue
        today = by_date[d]
        prev = by_date[prev_d]
        nxt = by_date[next_d]
        m = {b["hm"]: b for b in today}
        if not all(t in m for t in ("14:45", "14:50", "14:55")):
            continue
        upto = [b for b in today if b["hm"] <= "14:55"]
        if not upto:
            continue
        b45, b50, b55 = m["14:45"], m["14:50"], m["14:55"]
        if min(b45["volume"], b50["volume"], b55["volume"]) <= 0:
            continue
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
        shares = lot(price)
        if shares < 100:
            continue

        tail_ret = (price / b45["open"] - 1) * 100 if b45["open"] > 0 else -99
        if tail_ret < -0.5:
            continue
        vwap_gap = (price / vwap - 1) * 100
        range_pos = (price - day_low) / (day_high - day_low) if day_high > day_low else 0.5
        recent_amt = b50["amount"] + b55["amount"]
        prior_tail = max(b45["amount"], 1.0)
        accel = recent_amt / (2 * prior_tail)
        cap_util = shares * price / CAPITAL

        pct_score = clamp(25 - abs(pct - 5.5) * 4.2, 0, 25)
        amt_score = clamp(math.log1p(amount / 2e8) * 11, 0, 20)
        vwap_score = clamp(5 + vwap_gap * 8, 0, 15)
        tail_score = clamp(9 + tail_ret * 14, 0, 20)
        accel_score = clamp((accel - 0.5) * 8, 0, 10)
        pos_score = clamp((range_pos - 0.45) * 18, 0, 10)
        capital_score = cap_util * 10
        score = pct_score + amt_score + vwap_score + tail_score + accel_score + pos_score + capital_score

        next_open = nxt[0]["open"]
        gross_ret = next_open / price - 1
        buy_value = shares * price
        sell_value = shares * next_open
        buy_fee = max(MIN_COMMISSION, buy_value * COMMISSION_RATE) + buy_value * TRANSFER_RATE
        sell_fee = max(MIN_COMMISSION, sell_value * COMMISSION_RATE) + sell_value * (STAMP_RATE + TRANSFER_RATE)
        net_pnl = sell_value - buy_value - buy_fee - sell_fee
        net_ret = net_pnl / CAPITAL
        result.append({
            "date": d, "next_date": next_d, "code": code, "name": name,
            "buy_1455": round(price, 4), "next_open": round(next_open, 4), "shares": shares,
            "used_cash": round(buy_value, 2), "cash_left": round(CAPITAL - buy_value, 2),
            "pct_1455": round(pct, 3), "amount_1455": round(amount, 2), "vwap": round(vwap, 4),
            "vwap_gap_pct": round(vwap_gap, 3), "tail_ret_pct": round(tail_ret, 3),
            "range_pos": round(range_pos, 4), "accel": round(accel, 3), "score": round(score, 3),
            "gross_return_pct": round(gross_ret * 100, 3), "hit_1pct": gross_ret >= 0.01,
            "net_pnl": round(net_pnl, 2), "net_return_pct": round(net_ret * 100, 3),
        })
    return code, name, result, None


def write_outputs(trade_dates: list[str], universe_n: int, candidates: dict[str, list[dict[str, Any]]], errors: list[tuple[str, str]]) -> None:
    picks = []
    for d in trade_dates[:-1]:
        rows = candidates.get(d, [])
        if rows:
            picks.append(max(rows, key=lambda x: x["score"]))

    csv_path = OUT / "july_2026_tail_backtest.csv"
    fields = list(picks[0].keys()) if picks else ["date"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(picks)

    n = len(picks)
    hits = sum(bool(x["hit_1pct"]) for x in picks)
    positives = sum(x["net_pnl"] > 0 for x in picks)
    total_net = sum(x["net_pnl"] for x in picks)
    avg_gross = statistics.mean(x["gross_return_pct"] for x in picks) if picks else 0
    avg_net = statistics.mean(x["net_return_pct"] for x in picks) if picks else 0
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in picks:
        cumulative += x["net_pnl"]
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)

    md = []
    md.append("# 2026年7月 14:55尾盘策略回测")
    md.append("")
    md.append(f"- 数据源：东方财富 `kline/get` 5分钟行情")
    md.append(f"- 期间：{trade_dates[0] if trade_dates else '-'} 至 {trade_dates[-2] if len(trade_dates)>1 else '-'}（仅已获得次日开盘的买入日）")
    md.append(f"- 股票池：当前沪深主板非ST个股，共 {universe_n} 只；成功读取后参与逐日截面筛选")
    md.append(f"- 每日：14:55买入唯一最高分个股，次交易日首根5分钟K线开盘价卖出")
    md.append(f"- 资金：固定10000元，100股整数倍，不复利")
    md.append("")
    md.append("## 汇总")
    md.append("")
    md.append(f"- 已结算交易：**{n}笔**")
    md.append(f"- 次日开盘高于买点1%命中：**{hits}/{n} = {(hits/n*100 if n else 0):.1f}%**")
    md.append(f"- 扣费后盈利交易：**{positives}/{n} = {(positives/n*100 if n else 0):.1f}%**")
    md.append(f"- 平均隔夜毛收益：**{avg_gross:.3f}%**")
    md.append(f"- 平均单笔净收益：**{avg_net:.3f}%**")
    md.append(f"- 固定1万元累计净盈亏：**{total_net:.2f}元**")
    md.append(f"- 顺序累计最大回撤：**{max_dd:.2f}元**")
    md.append("")
    md.append("## 逐日结果")
    md.append("")
    md.append("|买入日|股票|代码|14:55买价|次日开盘|毛收益|≥1%|股数|净盈亏|评分|")
    md.append("|---|---|---:|---:|---:|---:|:---:|---:|---:|---:|")
    for x in picks:
        md.append(f"|{x['date']}|{x['name']}|{x['code']}|{x['buy_1455']:.2f}|{x['next_open']:.2f}|{x['gross_return_pct']:.2f}%|{'是' if x['hit_1pct'] else '否'}|{x['shares']}|{x['net_pnl']:.2f}|{x['score']:.1f}|")
    md.append("")
    md.append("## 口径限制")
    md.append("")
    md.append("1. 使用5分钟K线的14:55收盘价，无法还原逐笔滑点和排队成交。")
    md.append("2. 股票池使用当前存续证券，存在轻微生存者偏差。")
    md.append("3. 回测仅使用可复现的分钟量价核心因子，未加入历史时点新闻/板块资讯快照。")
    md.append("4. 费用默认：佣金万三、单边最低5元，卖出印花税万五，过户费十万分之一；可在脚本顶部修改。")
    md.append(f"5. 接口读取失败股票数：{len(errors)}。")
    (OUT / "july_2026_tail_backtest.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / "july_2026_tail_backtest_meta.json").write_text(json.dumps({
        "trade_dates": trade_dates, "universe": universe_n, "picks": n, "hits": hits,
        "hit_rate": hits / n if n else 0, "positive_rate": positives / n if n else 0,
        "total_net_pnl": round(total_net, 2), "max_drawdown_yuan": round(max_dd, 2),
        "errors": errors[:100],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    trade_dates = fetch_trade_dates()
    if len(trade_dates) < 2:
        raise RuntimeError(f"交易日读取失败: {trade_dates}")
    universe = fetch_universe()
    print(f"trade_dates={trade_dates}")
    print(f"universe={len(universe)}")

    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    errors: list[tuple[str, str]] = []
    workers = int(os.getenv("BACKTEST_WORKERS", "16"))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(analyze_stock, item, trade_dates): item for item in universe}
        for idx, fut in enumerate(cf.as_completed(futs), 1):
            item = futs[fut]
            try:
                code, name, rows, err = fut.result()
                if err:
                    errors.append((code, err))
                for r in rows:
                    candidates[r["date"]].append(r)
            except Exception as e:
                errors.append((item["code"], str(e)))
            if idx % 200 == 0:
                print(f"processed {idx}/{len(universe)}")

    write_outputs(trade_dates, len(universe), candidates, errors)
    print((OUT / "july_2026_tail_backtest.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
