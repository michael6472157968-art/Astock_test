"""F2 量价背离因子的持有期（horizon）扫描——IC 随前向 N 日收益的衰减。

回答「缩量涨的股票未来跑赢，未来是多久」：扫 fwd = 1/3/5/10/20/40/60/120 日的 mean IC + t 值。
实盘版 F2 = 单股近20日 close×volume Pearson 相关（负相关=缩量涨=好）。
用法: cd backend && PYTHONIOENCODING=utf-8 python horizon_scan_f2.py
"""
import pandas as pd
import numpy as np

df = pd.read_pickle("data/long_daily.pkl")
df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str))
df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
for c in ["close", "vol"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

df["ret1"] = df.groupby("ts_code")["close"].pct_change()
df["vol_chg"] = df.groupby("ts_code")["vol"].pct_change()

# F2 实盘版：20日 close×volume 滚动 Pearson 相关
f2 = df.groupby("ts_code")[["ret1", "vol_chg"]].apply(
    lambda x: x["ret1"].rolling(20, min_periods=10).corr(x["vol_chg"])
).reset_index(level=0, drop=True)
df["f2"] = f2.values
# 方向统一：corr 越低（越负=缩量涨）越好 → 1 - rank
df["f2_score"] = 1 - df.groupby("trade_date")["f2"].rank(pct=True)

print("=== F2 量价背离 IC 随持有期衰减 (1000股×10年) ===")
print(f"{'前向N日':<8}{'mean IC':<12}{'IC t值':<10}{'ICIR':<8}{'IC期数':<8}")
for fwd in [1, 3, 5, 10, 20, 40, 60, 120]:
    fwd_ret = df.groupby("ts_code")["close"].shift(-fwd) / df["close"] - 1
    fwd_rank = fwd_ret.groupby(df["trade_date"]).rank(pct=True)
    tmp = pd.DataFrame({
        "td": df["trade_date"].values,
        "f": df["f2_score"].values,
        "r": fwd_rank.values,
    }).dropna()
    ic = tmp.groupby("td").apply(lambda g: g["f"].corr(g["r"]))
    ic = ic.dropna()
    if len(ic) < 30:
        continue
    mean_ic = ic.mean()
    std_ic = ic.std()
    t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
    icir = mean_ic / std_ic if std_ic > 0 else 0.0
    print(f"{fwd:<8}{mean_ic:<12.4f}{t:<10.2f}{icir:<8.3f}{len(ic):<8}")
