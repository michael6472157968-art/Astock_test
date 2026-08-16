"""拉取 10 年解禁 + 增减持事件 → data/share_float.pkl + data/stk_holdertrade.pkl。

- share_float: 按 float_date(解禁日) 范围拉，字段 float_ratio(解禁比例%)/float_share
- stk_holdertrade: 按 ann_date(公告日) 范围拉，字段 in_de(IN增持/DE减持)/change_ratio

按月循环 2016-2026（120 个月 × 2 接口）。
用法: cd backend && PYTHONIOENCODING=utf-8 python fetch_events.py
"""
from __future__ import annotations

import os
import time

import pandas as pd
import tushare as ts

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

pro = ts.pro_api()


def main():
    months = pd.period_range("2016-01", "2026-08", freq="M")
    sf_out: list[pd.DataFrame] = []
    ht_out: list[pd.DataFrame] = []
    t0 = time.time()

    for p in months:
        start = p.strftime("%Y%m01")
        end = p.end_time.strftime("%Y%m%d")
        for attempt in range(3):
            try:
                sf = pro.share_float(start_date=start, end_date=end)
                if sf is not None and len(sf):
                    sf_out.append(sf)
                break
            except Exception:
                time.sleep(2)
        for attempt in range(3):
            try:
                ht = pro.stk_holdertrade(start_date=start, end_date=end)
                if ht is not None and len(ht):
                    ht_out.append(ht)
                break
            except Exception:
                time.sleep(2)
        if p.month % 12 == 0:
            print(f"{p} 完成, 已用时 {time.time()-t0:.0f}s", flush=True)
        time.sleep(0.2)

    if sf_out:
        sf = pd.concat(sf_out, ignore_index=True).drop_duplicates(["ts_code", "float_date", "holder_name", "float_ratio"])
        sf.to_pickle(os.path.join(DATA_DIR, "share_float.pkl"))
    if ht_out:
        ht = pd.concat(ht_out, ignore_index=True).drop_duplicates(["ts_code", "ann_date", "holder_name", "change_ratio"])
        ht.to_pickle(os.path.join(DATA_DIR, "stk_holdertrade.pkl"))

    print(f"\n完成: 解禁 {sum(len(x) for x in sf_out)} 行, 增减持 {sum(len(x) for x in ht_out)} 行, 用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
