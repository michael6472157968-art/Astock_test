"""拉取 10 年日线数据 → parquet，用于跨牛熊因子回测。

DB 的 stock_daily 只有 ~2 年。本脚本用 Tushare daily 接口（120积分免费）
拉采样股票 2016-2026 十年日线，落 backend/data/long_daily.parquet。

采样：上市 > 8 年（list_date < 2018）的非 ST 股票，随机 N 只。

用法: cd backend && PYTHONIOENCODING=utf-8 python fetch_long_daily.py [sample_size]
"""
from __future__ import annotations

import os
import random
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.tushare_client import get_pro

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long_daily.pkl")


def main(sample_size: int):
    pro = get_pro()

    # 全市场股票列表 + 过滤
    basic = pro.stock_basic(list_status="L", fields="ts_code,name,list_date")
    basic["list_date"] = basic["list_date"].astype(str)
    basic = basic[basic["list_date"] < "20180101"]           # 上市 > 8 年
    basic = basic[~basic["name"].str.contains("ST", na=False)]
    codes = basic["ts_code"].tolist()
    random.shuffle(codes)
    codes = codes[:sample_size]

    print(f"采样 {len(codes)} 只股票，拉取 2016-2026 十年日线...")

    frames = []
    for i, code in enumerate(codes):
        try:
            df = pro.daily(ts_code=code, start_date="20160101", end_date="20260814")
            if df is not None and not df.empty:
                frames.append(df[["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]])
        except Exception as e:
            print(f"  {code} 失败: {e}")
        if (i + 1) % 100 == 0:
            print(f"  已拉 {i + 1}/{len(codes)}")
            time.sleep(1)  # 温和限流

    if not frames:
        print("无数据，退出")
        return

    all_df = pd.concat(frames, ignore_index=True)
    all_df.to_pickle(OUT)
    n_stocks = all_df["ts_code"].nunique()
    n_days = all_df["trade_date"].nunique()
    print(f"\n完成: {len(all_df)} 行, {n_stocks} 股, {n_days} 交易日 → {OUT}")
    print(f"时间跨度: {all_df['trade_date'].min()} ~ {all_df['trade_date'].max()}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    main(n)
