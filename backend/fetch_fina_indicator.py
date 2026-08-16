"""拉取全市场 fina_indicator 财务指标 → fina_indicator 表（断点续传）。

F7(现金流 cfps_yoy/ocf_yoy/ocfps) 和 F8(成长 dt_netprofit_yoy/roe_yoy) 的数据源。
fina_indicator 是 2000 积分接口，按 ts_code 逐股拉（每股约 40 期季度数据）。
断点续传：fina_indicator 表已有该股数据即跳过。

用法: cd backend && PYTHONIOENCODING=utf-8 python fetch_fina_indicator.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.tushare_client import get_pro

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stock_analyzer.db")

_FIELDS = ["cfps_yoy", "ocf_yoy", "ocfps", "ocf_to_debt", "dt_netprofit_yoy", "roe_yoy", "basic_eps_yoy",
           "roe", "roa", "grossprofit_margin", "netprofit_margin", "or_yoy", "netprofit_yoy", "debt_to_assets"]


def _v(row, key):
    val = row.get(key)
    return float(val) if val is not None and str(val) != "nan" else None


def main():
    pro = get_pro()
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT ts_code FROM stocks ORDER BY ts_code")
    codes = [r[0] for r in c.fetchall()]
    c.execute("SELECT DISTINCT ts_code FROM fina_indicator")
    done = {r[0] for r in c.fetchall()}
    todo = [x for x in codes if x not in done]
    print(f"待拉 {len(todo)} 股（已拉 {len(done)}）")

    for i, code in enumerate(todo):
        try:
            f = pro.fina_indicator(ts_code=code)
            if f is not None and not f.empty:
                for _, row in f.iterrows():
                    try:
                        c.execute("""
                            INSERT OR REPLACE INTO fina_indicator
                                (ts_code, end_date, ann_date, cfps_yoy, ocf_yoy, ocfps, ocf_to_debt,
                                 dt_netprofit_yoy, roe_yoy, basic_eps_yoy, roe, roa, grossprofit_margin,
                                 netprofit_margin, or_yoy, netprofit_yoy, debt_to_assets)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            code,
                            str(row.get("end_date", "")),
                            str(row.get("ann_date", "")) if row.get("ann_date") is not None else None,
                            _v(row, "cfps_yoy"), _v(row, "ocf_yoy"), _v(row, "ocfps"), _v(row, "ocf_to_debt"),
                            _v(row, "dt_netprofit_yoy"), _v(row, "roe_yoy"), _v(row, "basic_eps_yoy"),
                            _v(row, "roe"), _v(row, "roa"), _v(row, "grossprofit_margin"),
                            _v(row, "netprofit_margin"), _v(row, "or_yoy"), _v(row, "netprofit_yoy"),
                            _v(row, "debt_to_assets"),
                        ))
                    except Exception:
                        continue
                conn.commit()
        except Exception as e:
            print(f"  {code} 失败: {e}")
        if (i + 1) % 50 == 0:
            print(f"  已拉 {i + 1}/{len(todo)}")
            time.sleep(2)

    conn.close()
    print("完成")


if __name__ == "__main__":
    main()
