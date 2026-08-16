"""拉取财务因子数据（daily_basic + fina_indicator）→ pkl，用于横截面 IC 检验。

两条数据源，覆盖 Tushare 财务因子库三大类：
- daily_basic（日频，120积分）：pe/pe_ttm/pb/ps/ps_ttm/dv_ratio/dv_ttm/total_mv/circ_mv
  → 价值因子 EP=1/pe_ttm、BP=1/pb、SP=1/ps_ttm、DP=dv_ttm、市值
- fina_indicator（季度频，2000积分）：roe/roa/毛利率/净利率/负债率/增速/现金流质量等 108 列
  → 质量因子 + 成长因子（保留 ann_date 做 point-in-time 对齐）

样本：long_daily.pkl 的 1000 股。断点续传：已拉的 ts_code 跳过。
用法: cd backend && PYTHONIOENCODING=utf-8 python fetch_financial.py
"""
from __future__ import annotations

import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.tushare_client import get_pro

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LONG_PKL = os.path.join(DATA_DIR, "long_daily.pkl")
DB_PKL = os.path.join(DATA_DIR, "daily_basic.pkl")
FIN_PKL = os.path.join(DATA_DIR, "fina_indicator.pkl")

# daily_basic 价值因子字段
DB_FIELDS = "ts_code,trade_date,close,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_mv,circ_mv,turnover_rate"


def _load_codes() -> list[str]:
    base = pd.read_pickle(LONG_PKL)
    codes = sorted(base["ts_code"].unique())
    print(f"样本 {len(codes)} 股 (来自 long_daily.pkl)")
    return codes


def _done_codes(pkl: str) -> set[str]:
    if os.path.exists(pkl):
        try:
            return set(pd.read_pickle(pkl)["ts_code"].unique())
        except Exception:
            return set()
    return set()


def fetch_daily_basic(pro, codes: list[str]):
    done = _done_codes(DB_PKL)
    todo = [c for c in codes if c not in done]
    print(f"daily_basic: 已拉 {len(done)}, 待拉 {len(todo)}")

    frames = []
    if done:
        frames.append(pd.read_pickle(DB_PKL))

    for i, code in enumerate(todo):
        try:
            d = pro.daily_basic(ts_code=code, start_date="20160101", end_date="20260814", fields=DB_FIELDS)
            if d is not None and not d.empty:
                frames.append(d)
        except Exception as e:
            print(f"  daily_basic {code} 失败: {e}")
        if (i + 1) % 100 == 0:
            print(f"  daily_basic 已拉 {i + 1}/{len(todo)}")
            # 落盘断点（增量合并）
            pd.concat(frames, ignore_index=True).to_pickle(DB_PKL)
            time.sleep(1)

    out = pd.concat(frames, ignore_index=True).drop_duplicates(["ts_code", "trade_date"])
    out.to_pickle(DB_PKL)
    print(f"daily_basic 完成: {len(out)} 行, {out['ts_code'].nunique()} 股 → {DB_PKL}")


def fetch_fina_indicator(pro, codes: list[str]):
    done = _done_codes(FIN_PKL)
    todo = [c for c in codes if c not in done]
    print(f"fina_indicator: 已拉 {len(done)}, 待拉 {len(todo)}")

    frames = []
    if done:
        frames.append(pd.read_pickle(FIN_PKL))

    for i, code in enumerate(todo):
        try:
            f = pro.fina_indicator(ts_code=code, start_date="20150101", end_date="20260814")
            if f is not None and not f.empty:
                frames.append(f)
        except Exception as e:
            print(f"  fina_indicator {code} 失败: {e}")
        if (i + 1) % 50 == 0:
            print(f"  fina_indicator 已拉 {i + 1}/{len(todo)}")
            pd.concat(frames, ignore_index=True).to_pickle(FIN_PKL)
            time.sleep(2)  # 财务接口限流更严

    out = pd.concat(frames, ignore_index=True).drop_duplicates(["ts_code", "end_date"])
    out.to_pickle(FIN_PKL)
    print(f"fina_indicator 完成: {len(out)} 行, {out['ts_code'].nunique()} 股 → {FIN_PKL}")


def main():
    pro = get_pro()
    codes = _load_codes()
    fetch_daily_basic(pro, codes)
    fetch_fina_indicator(pro, codes)
    print("\n全部完成。")


if __name__ == "__main__":
    main()
