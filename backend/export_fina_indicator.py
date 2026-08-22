"""导出本地 fina_indicator 表 → gzip SQL 文件，用于导入 Fly 线上库。

本地库已全量拉取(5543股/24万行)，线上 Fly 的 fina_indicator 表为空。
直接把本地数据导成 INSERT OR REPLACE 批量 SQL，gzip 压缩后 sftp 上传 Fly 再 executescript 导入，
比在 Fly 上重新对 Tushare 拉 ~5000 只(烧财务接口额度+1小时+)快得多。

用法: cd backend && python export_fina_indicator.py
输出: data/fina_indicator_export.sql.gz
"""
from __future__ import annotations

import gzip
import math
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stock_analyzer.db")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fina_indicator_export.sql.gz")

COLS = ["ts_code", "end_date", "ann_date", "cfps_yoy", "ocf_yoy", "ocfps", "ocf_to_debt",
        "dt_netprofit_yoy", "roe_yoy", "basic_eps_yoy", "roe", "roa", "grossprofit_margin",
        "netprofit_margin", "or_yoy", "netprofit_yoy", "debt_to_assets"]
TEXT_COLS = {"ts_code", "end_date", "ann_date"}


def _val(col: str, v):
    """把值转成 SQL 字面量：NaN / None / 'nan' → NULL，文本加引号转义，数值直接 repr。"""
    if v is None:
        return "NULL"
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return "NULL"
    if col in TEXT_COLS:
        s = str(v)
        if s in ("nan", "NaN", "None", ""):
            return "NULL"
        return "'" + s.replace("'", "''") + "'"
    return repr(float(v))


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(f"SELECT {','.join(COLS)} FROM fina_indicator ORDER BY ts_code, end_date")
    rows = cur.fetchall()
    conn.close()

    n = len(rows)
    print(f"导出 {n} 行 → {OUT}")

    batch = 500
    header = "INSERT OR REPLACE INTO fina_indicator (" + ",".join(COLS) + ") VALUES\n"
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        f.write("BEGIN;\n")
        for i in range(0, n, batch):
            chunk = rows[i:i + batch]
            vals = ",\n".join("(" + ",".join(_val(c, v) for c, v in zip(COLS, r)) + ")" for r in chunk)
            f.write(header + vals + ";\n")
            if (i // batch) % 20 == 0:
                print(f"  ...{i}/{n}")
        f.write("COMMIT;\n")
    print(f"完成，输出 {OUT}")


if __name__ == "__main__":
    main()
