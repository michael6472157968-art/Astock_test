"""拉取 1000 股 × 8年筹码分布 → data/cyq_perf.pkl。

筹码分布字段：winner_rate(获利盘%)/weight_avg(平均成本)/cost_5pct~95pct(筹码分位数)/his_high/his_low。
因子方向（待IC验证）：
- 获利盘比例 winner_rate 高 → 抛压大 → 看跌（负IC，且与反转同源）
- 筹码集中度 = (cost_95pct-cost_5pct)/weight_avg 窄 → 筹码集中 → 看涨

数据从 2018 年开始，per-stock 拉取。
用法: cd backend && PYTHONIOENCODING=utf-8 python fetch_cyqperf.py
"""
from __future__ import annotations

import os
import time

import pandas as pd
import tushare as ts

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
OUT_PKL = os.path.join(DATA_DIR, "cyq_perf.pkl")

pro = ts.pro_api()


def main():
    codes = pd.read_pickle(LONG_PKL)["ts_code"].unique().tolist()
    out: list[pd.DataFrame] = []
    fail = 0
    t0 = time.time()

    for i, code in enumerate(codes):
        ok = False
        for attempt in range(3):
            try:
                df = pro.cyq_perf(ts_code=code, start_date="20180101", end_date="20260815")
                if df is not None and len(df):
                    out.append(df)
                ok = True
                break
            except Exception as e:
                msg = str(e)
                if "每分钟" in msg or "频次" in msg or "limit" in msg.lower():
                    time.sleep(60)
                else:
                    time.sleep(2)
        if not ok:
            fail += 1
        if (i + 1) % 100 == 0:
            print(f"{i+1}/{len(codes)} 完成, 已用时 {time.time()-t0:.0f}s, 失败 {fail}", flush=True)

    if out:
        res = pd.concat(out, ignore_index=True)
        res = res.drop_duplicates(["ts_code", "trade_date"], keep="last")
        res.to_pickle(OUT_PKL)
        print(f"\n完成: {len(res)} 行, {res['ts_code'].nunique()} 股, 失败 {fail} 股, 用时 {time.time()-t0:.0f}s")
    else:
        print("无数据")


if __name__ == "__main__":
    main()
