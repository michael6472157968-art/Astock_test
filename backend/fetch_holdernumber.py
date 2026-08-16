"""拉取 1000 股 × 10 年股东户数历史 → data/stk_holdernumber.pkl。

股东户数因子（筹码集中度）：户数下降=筹码集中=主力吸筹=看涨，A股经典 alpha，
与量价/基本面正交。数据不定期公布（季度为主），做 point-in-time IC 检验需要多年历史。

用法: cd backend && PYTHONIOENCODING=utf-8 python fetch_holdernumber.py
"""
from __future__ import annotations

import os
import time

import pandas as pd
import tushare as ts

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
OUT_PKL = os.path.join(DATA_DIR, "stk_holdernumber.pkl")

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
                df = pro.stk_holdernumber(ts_code=code, start_date="20160101", end_date="20260815")
                if df is not None and len(df):
                    out.append(df)
                ok = True
                break
            except Exception as e:
                msg = str(e)
                if "每分钟" in msg or "频次" in msg or "limit" in msg.lower():
                    time.sleep(60)  # 撞限流，等1分钟
                else:
                    time.sleep(2)
        if not ok:
            fail += 1
        if (i + 1) % 100 == 0:
            el = time.time() - t0
            print(f"{i+1}/{len(codes)} 完成, 已用时 {el:.0f}s, 失败 {fail}", flush=True)

    if out:
        res = pd.concat(out, ignore_index=True)
        res = res.drop_duplicates(["ts_code", "ann_date", "end_date"], keep="last")
        res.to_pickle(OUT_PKL)
        print(f"\n完成: {len(res)} 行, {res['ts_code'].nunique()} 股, 失败 {fail} 股, 用时 {time.time()-t0:.0f}s")
    else:
        print("无数据")


if __name__ == "__main__":
    main()
