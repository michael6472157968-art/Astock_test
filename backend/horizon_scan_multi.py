"""多因子持有期（horizon）对比扫描——回答「各因子最优持有期是否一致」。

扫当前选股池三因子 + 反转 F1 的 IC 随前向 N 日收益的衰减：
  F2 量价背离(20日 close×vol 相关, low_good)
  F7 现金流 cfps_yoy(季度频财务, high_good)
  F8 成长 dt_netprofit_yoy(季度频财务, high_good)
  F1 反转 return_42d(low_good)
用法: cd backend && PYTHONIOENCODING=utf-8 python horizon_scan_multi.py
"""
import pandas as pd
import numpy as np

df = pd.read_pickle("data/long_daily.pkl")
df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str))
df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
for c in ["close", "vol"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

# F2 量价背离：20日 close×volume 滚动相关
df["ret1"] = df.groupby("ts_code")["close"].pct_change()
df["vol_chg"] = df.groupby("ts_code")["vol"].pct_change()
f2 = df.groupby("ts_code")[["ret1", "vol_chg"]].apply(
    lambda x: x["ret1"].rolling(20, min_periods=10).corr(x["vol_chg"])
).reset_index(level=0, drop=True)
df["f2"] = f2.values

# F1 反转：return_42d
df["f1"] = df.groupby("ts_code")["close"].pct_change(42)

# F7/F8 财务：point-in-time 映射（pandas3 需全局 sort）
fina = pd.read_pickle("data/fina_indicator.pkl")
fina["end_date"] = pd.to_datetime(fina["end_date"].astype(str))
for c in ["cfps_yoy", "dt_netprofit_yoy"]:
    fina[c] = pd.to_numeric(fina[c], errors="coerce")
fina = fina.dropna(subset=["end_date"]).sort_values("end_date")
df = pd.merge_asof(
    df.sort_values("trade_date"),
    fina[["ts_code", "end_date", "cfps_yoy", "dt_netprofit_yoy"]],
    left_on="trade_date", right_on="end_date", by="ts_code", direction="backward",
)
df["f7"] = df["cfps_yoy"]
df["f8"] = df["dt_netprofit_yoy"]

# 方向统一（低好→1-rank，高好→rank）
df["f1_score"] = 1 - df.groupby("trade_date")["f1"].rank(pct=True)
df["f2_score"] = 1 - df.groupby("trade_date")["f2"].rank(pct=True)
df["f7_score"] = df.groupby("trade_date")["f7"].rank(pct=True)
df["f8_score"] = df.groupby("trade_date")["f8"].rank(pct=True)

FACTORS = [("F1 反转42d", "f1_score"), ("F2 量价背离", "f2_score"),
           ("F7 现金流", "f7_score"), ("F8 成长", "f8_score")]

print("=== 各因子 IC 随持有期衰减 (1000股×10年) ===")
print("持有期 | " + " | ".join(f"{n:>8}(t)" for n, _ in FACTORS))
for fwd in [5, 10, 20, 40, 60, 120]:
    fwd_ret = df.groupby("ts_code")["close"].shift(-fwd) / df["close"] - 1
    fwd_rank = fwd_ret.groupby(df["trade_date"]).rank(pct=True)
    cells = []
    for name, col in FACTORS:
        tmp = pd.DataFrame({
            "td": df["trade_date"].values,
            "f": df[col].values,
            "r": fwd_rank.values,
        }).dropna()
        ic = tmp.groupby("td").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        mean_ic = ic.mean()
        t = mean_ic / ic.std() * np.sqrt(len(ic)) if ic.std() > 0 else 0.0
        cells.append(f"{mean_ic:>7.4f}({t:>4.1f})")
    print(f"{fwd:<6} | " + " | ".join(cells))
