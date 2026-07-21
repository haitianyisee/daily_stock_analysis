# -*- coding: utf-8 -*-
"""Robust wrapper for July 2026 tail-strategy backtest."""
from __future__ import annotations

import os
import random
import time
from typing import Any

import requests

import july_tail_backtest as bt


PUSH2_HOSTS = [
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://33.push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
]


def robust_get_json(url: str, params: dict[str, Any], retries: int = 10) -> dict[str, Any]:
    hosts = [url]
    if "push2.eastmoney.com" in url and "push2his" not in url:
        suffix = url.split("push2.eastmoney.com", 1)[1]
        hosts = [h + suffix for h in PUSH2_HOSTS]

    err: Exception | None = None
    for i in range(retries):
        target = hosts[i % len(hosts)]
        try:
            p = dict(params)
            p["_"] = int(time.time() * 1000) + random.randint(0, 999)
            r = bt.session().get(target, params=p, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            if data is None:
                raise RuntimeError("empty json")
            return data
        except Exception as e:
            err = e
            time.sleep(min(8.0, 0.7 * (2 ** min(i, 4))))
    raise RuntimeError(f"request failed after {retries} attempts: {url}: {err}")


def robust_fetch_universe() -> list[dict[str, Any]]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    fields = "f12,f14,f2,f3,f6,f8,f15,f16,f17,f18"

    # First try a single large page to avoid fragile deep pagination.
    p = {
        "pn": 1,
        "pz": 6000,
        "po": 1,
        "np": 1,
        "ut": bt.UT,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": fields,
    }
    rows: list[dict[str, Any]] = []
    try:
        js = robust_get_json(url, p, retries=12)
        rows = ((js.get("data") or {}).get("diff") or [])
    except Exception:
        rows = []

    # Fallback: query market groups separately with small pages.
    if len(rows) < 1000:
        rows = []
        groups = ["m:0+t:6,m:0+t:80", "m:1+t:2,m:1+t:23"]
        for fs in groups:
            page = 1
            while page <= 40:
                q = dict(p)
                q.update({"pn": page, "pz": 100, "fs": fs})
                try:
                    js = robust_get_json(url, q, retries=8)
                except Exception:
                    # Skip one bad page only after repeated host rotation.
                    page += 1
                    continue
                data = js.get("data") or {}
                batch = data.get("diff") or []
                rows.extend(batch)
                total = int(data.get("total") or 0)
                if not batch or page * 100 >= total:
                    break
                page += 1
                time.sleep(0.15)

    dedup: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = str(r.get("f12", "")).zfill(6)
        name = str(r.get("f14", ""))
        if not bt.is_main(code):
            continue
        upper = name.upper()
        if any(x in upper for x in ("ST", "退", "ETF", "基金", "指数")):
            continue
        dedup[code] = {"code": code, "name": name}

    out = list(dedup.values())
    if len(out) < 1000:
        raise RuntimeError(f"主板股票池过小，仅{len(out)}只")
    return out


if __name__ == "__main__":
    bt.get_json = robust_get_json
    bt.fetch_universe = robust_fetch_universe
    os.environ.setdefault("BACKTEST_WORKERS", "6")
    bt.main()
