"""全量因子持有期（horizon）扫描——11 个有效因子 × 7 个持有期的 IC 衰减矩阵。

目的：按各因子最优持有期给因子分层，据此设计分层选股池。
因子（方向已统一）：
  F1 反转 return_42d (low)      F2 量价背离 20日close×vol (low)   F3 低波动 vol_21d (low)
  F4 价值BP 1/pb (low)          F5 价值SP 1/ps_ttm (low)          F6 价值DP dv_ttm (high)
  F7 现金流 cfps_yoy (high)     F8 成长 dt_netprofit_yoy (high)   F9 低换手 turnover (low)
  F10 股东户数变化 (low)         F11 筹码宽度 concentration (high)
用法: cd backend && PYTHONIOENCODING=utf-8 python horizon_scan_all.py
"""
import pandas as pd
import numpy as np

HOLD_DAYS = [5, 10, 20, 30, 40, 60, 120]

# ── 加载数据 ──
df = pd.read_pickle("data/long_daily.pkl")
df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str))
df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
for c in ["close", "vol"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

db = pd.read_pickle("data/daily_basic.pkl")
db["trade_date"] = pd.to_datetime(db["trade_date"].astype(str))
for c in ["pb", "ps_ttm", "dv_ttm", "turnover_rate"]:
    db[c] = pd.to_numeric(db[c], errors="coerce")
df = df.merge(db[["ts_code", "trade_date", "pb", "ps_ttm", "dv_ttm", "turnover_rate"]],
              on=["ts_code", "trade_date"], how="left")

cyq = pd.read_pickle("data/cyq_perf.pkl")
cyq["trade_date"] = pd.to_datetime(cyq["trade_date"].astype(str))
for c in ["cost_5pct", "cost_95pct", "weight_avg"]:
    cyq[c] = pd.to_numeric(cyq[c], errors="coerce")
df = df.merge(cyq[["ts_code", "trade_date", "cost_5pct", "cost_95pct", "weight_avg"]],
              on=["ts_code", "trade_date"], how="left")

fina = pd.read_pickle("data/fina_indicator.pkl")
fina["end_date"] = pd.to_datetime(fina["end_date"].astype(str))
for c in ["cfps_yoy", "dt_netprofit_yoy"]:
    fina[c] = pd.to_numeric(fina[c], errors="coerce")
fina = fina.dropna(subset=["end_date"]).sort_values("end_date")
df = pd.merge_asof(df.sort_values("trade_date"),
                   fina[["ts_code", "end_date", "cfps_yoy", "dt_netprofit_yoy"]],
                   left_on="trade_date", right_on="end_date", by="ts_code", direction="backward")

holder = pd.read_pickle("data/stk_holdernumber.pkl")
holder["end_date"] = pd.to_datetime(holder["end_date"].astype(str))
holder["holder_num"] = pd.to_numeric(holder["holder_num"], errors="coerce")
holder = holder.dropna(subset=["end_date", "holder_num"]).sort_values("end_date")
df = pd.merge_asof(df.sort_values("trade_date"),
                   holder[["ts_code", "end_date", "holder_num"]],
                   left_on="trade_date", right_on="end_date", by="ts_code", direction="backward")

# merge_asof 打乱了分组顺序，重新按 ts_code 分组排序（groupby apply 需分组连续）
df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

# ── 计算因子原始值 ──
df["ret1"] = df.groupby("ts_code")["close"].pct_change()
df["vol_chg"] = df.groupby("ts_code")["vol"].pct_change()

f2 = df.groupby("ts_code")[["ret1", "vol_chg"]].apply(
    lambda x: x["ret1"].rolling(20, min_periods=10).corr(x["vol_chg"])
).reset_index(level=0, drop=True)
df["f2"] = f2  # index 对齐（df 已按 ts_code 分组排序）

df["f1"] = df.groupby("ts_code")["close"].pct_change(42)
df["f3"] = df.groupby("ts_code")["ret1"].transform(lambda s: s.rolling(21, min_periods=15).std())
df["f4"] = 1.0 / df["pb"]
df["f5"] = 1.0 / df["ps_ttm"]
df["f6"] = df["dv_ttm"]
df["f7"] = df["cfps_yoy"]
df["f8"] = df["dt_netprofit_yoy"]
df["f9"] = df["turnover_rate"]
df["f10"] = df.groupby("ts_code")["holder_num"].pct_change()
df["f11"] = (df["cost_95pct"] - df["cost_5pct"]) / df["weight_avg"].replace(0, np.nan)

# 方向统一成「越高越好」的分位
# 注意 f4/f5 是 1/pb、1/ps_ttm（BP/SP，越高=越低估越好），是 high_good
LOW_GOOD = ["f1", "f2", "f3", "f9", "f10"]
HIGH_GOOD = ["f4", "f5", "f6", "f7", "f8", "f11"]
for f in LOW_GOOD:
    df[f + "_s"] = 1 - df.groupby("trade_date")[f].rank(pct=True)
for f in HIGH_GOOD:
    df[f + "_s"] = df.groupby("trade_date")[f].rank(pct=True)

FACTORS = [("F1反转", "f1_s"), ("F2量价背离", "f2_s"), ("F3低波动", "f3_s"),
           ("F4价值BP", "f4_s"), ("F5价值SP", "f5_s"), ("F6价值DP", "f6_s"),
           ("F7现金流", "f7_s"), ("F8成长", "f8_s"), ("F9低换手", "f9_s"),
           ("F10股东户数", "f10_s"), ("F11筹码宽度", "f11_s")]

# ── 扫描 IC ──
results = {fwd: {} for fwd in HOLD_DAYS}
for fwd in HOLD_DAYS:
    fwd_ret = df.groupby("ts_code")["close"].shift(-fwd) / df["close"] - 1
    fwd_rank = fwd_ret.groupby(df["trade_date"]).rank(pct=True)
    for name, col in FACTORS:
        tmp = pd.DataFrame({
            "td": df["trade_date"].values,
            "f": df[col].values,
            "r": fwd_rank.values,
        }).dropna()
        ic = tmp.groupby("td").apply(lambda g: g["f"].corr(g["r"]))
        ic = ic.dropna()
        if len(ic) < 30:
            results[fwd][name] = np.nan
            continue
        results[fwd][name] = ic.mean()

# ── 输出矩阵 ──
names = [n for n, _ in FACTORS]
print("=== 11 因子 IC 持有期矩阵 (1000股×10年) ===")
header = "持有期 | " + " | ".join(f"{n:>8}" for n in names)
print(header)
print("-" * len(header))
for fwd in HOLD_DAYS:
    cells = " | ".join(f"{results[fwd][n]:>8.4f}" if not np.isnan(results[fwd][n]) else f"{'N/A':>8}" for n in names)
    print(f"{fwd:<6} | {cells}")

# ── 每个因子的最优持有期 + 峰值 IC ──
print("\n=== 各因子最优持有期（IC 峰值） ===")
for name in names:
    best = max(HOLD_DAYS, key=lambda f: results[f][name] if not np.isnan(results[f][name]) else -1)
    peak = results[best][name]
    print(f"{name:<12} 峰值 {best:>4}日  IC={peak:.4f}")
