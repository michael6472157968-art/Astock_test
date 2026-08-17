"""2 档分层选股池回测验证。

短线池(持有20日,月度): F1反转 + F2量价背离 + F8成长，20日口径IC加权
长线池(持有60日,季度): F9低换手 + F3低波动 + F4价值BP + F5价值SP + F6股息 + F7现金流 + F11筹码，60日口径IC加权

对比：当前统一池(F2+F8+F7)在 20/60 日持有下的表现。
纯多头：每日选 top15 等权持有 N 天 vs 全市场等权基准。
用法: cd backend && PYTHONIOENCODING=utf-8 python factor_pool_tiered_backtest.py
"""
import pandas as pd
import numpy as np

# ── 数据加载 + 因子计算（同 horizon_scan_all）──
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

df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

df["ret1"] = df.groupby("ts_code")["close"].pct_change()
df["vol_chg"] = df.groupby("ts_code")["vol"].pct_change()
f2 = df.groupby("ts_code")[["ret1", "vol_chg"]].apply(
    lambda x: x["ret1"].rolling(20, min_periods=10).corr(x["vol_chg"])
).reset_index(level=0, drop=True)
df["f2"] = f2

df["f1"] = df.groupby("ts_code")["close"].pct_change(42)
df["f3"] = df.groupby("ts_code")["ret1"].transform(lambda s: s.rolling(21, min_periods=15).std())
df["f4"] = 1.0 / df["pb"]
df["f5"] = 1.0 / df["ps_ttm"]
df["f6"] = df["dv_ttm"]
df["f7"] = df["cfps_yoy"]
df["f8"] = df["dt_netprofit_yoy"]
df["f9"] = df["turnover_rate"]
df["f11"] = (df["cost_95pct"] - df["cost_5pct"]) / df["weight_avg"].replace(0, np.nan)

LOW = ["f1", "f2", "f3", "f9"]
HIGH = ["f4", "f5", "f6", "f7", "f8", "f11"]
for f in LOW:
    df[f + "_s"] = 1 - df.groupby("trade_date")[f].rank(pct=True)
for f in HIGH:
    df[f + "_s"] = df.groupby("trade_date")[f].rank(pct=True)

# ── 池定义（IC 加权，用各自持有期口径的 IC）──
SHORT_POOL = {"f1_s": 0.398, "f2_s": 0.310, "f8_s": 0.292}   # 20日口径
LONG_POOL = {"f9_s": 0.242, "f3_s": 0.218, "f4_s": 0.170, "f5_s": 0.133,
             "f6_s": 0.126, "f7_s": 0.048, "f11_s": 0.062}    # 60日口径(防御)
LONG_ATTACK = {"f1_s": 0.381, "f2_s": 0.323, "f8_s": 0.295}   # 60日口径(进攻F1F2F8)
LONG_ATTACK_F7 = {"f1_s": 0.329, "f2_s": 0.279, "f8_s": 0.254, "f7_s": 0.138}  # 60日(进攻+现金流)
BASELINE = {"f2_s": 1 / 3, "f8_s": 1 / 3, "f7_s": 1 / 3}      # 当前统一池等权

def make_score(df, w):
    return df.assign(score=sum(df[k] * v for k, v in w.items()))


def backtest_long_only(df, hold_days, top_n=15):
    df = df.copy()
    df["fwd_ret"] = df.groupby("ts_code")["close"].shift(-hold_days) / df["close"] - 1
    df["year"] = df["trade_date"].dt.year
    m = df.dropna(subset=["score", "fwd_ret"])
    if len(m) < 100:
        return (0, 0, 0, 0, 0, 0)
    dates = sorted(m["trade_date"].unique())
    mkt = m.groupby("trade_date")["fwd_ret"].mean()
    ppy = 252 / hold_days
    rows = []
    for offset in range(hold_days):
        for rd in dates[offset::hold_days]:
            day = m[m["trade_date"] == rd]
            if len(day) < top_n:
                continue
            top = day.sort_values("score", ascending=False).head(top_n)
            rows.append((day["year"].iloc[0], top["fwd_ret"].mean(), mkt.get(rd, np.nan)))
    if not rows:
        return (0, 0, 0, 0, 0, 0)
    r = pd.DataFrame(rows, columns=["year", "top", "mkt"])
    top_ann = r["top"].mean() * ppy
    mkt_ann = r["mkt"].mean() * ppy
    excess = (r["top"] - r["mkt"]).mean() * ppy
    es = (r["top"] - r["mkt"]).std()
    sharpe = excess / es * np.sqrt(ppy) if es > 0 else 0
    pos = int((r.groupby("year")["top"].mean() > r.groupby("year")["mkt"].mean()).sum())
    ny = int(r["year"].nunique())
    return top_ann, mkt_ann, excess, sharpe, pos, ny


print("=== 2 档分层池 vs 当前统一池 (纯多头 top15 等权) ===\n")
print(f"{'池/方案':<28}{'持有':<6}{'组合年化':<10}{'基准年化':<10}{'超额年化':<10}{'超额夏普':<9}{'正超额年份':<8}")

cases = [
    ("短线池(F1+F2+F8)@20日", SHORT_POOL, 20),
    ("长线池防御(7慢变量)@60日", LONG_POOL, 60),
    ("长线池进攻(F1+F2+F8)@60日", LONG_ATTACK, 60),
    ("长线池(F1+F2+F8+F7)@60日", LONG_ATTACK_F7, 60),
    ("统一池(F2+F8+F7)@60日", BASELINE, 60),
]
for name, w, hold in cases:
    d = make_score(df, w)
    top_ann, mkt_ann, excess, sharpe, pos, ny = backtest_long_only(d, hold, top_n=15)
    print(f"{name:<28}{hold:<6}{top_ann*100:+8.2f}%  {mkt_ann*100:+8.2f}%  {excess*100:+8.2f}%  {sharpe:<9.2f}{f'{pos}/{ny}':<8}")

print("\n注：短线池持有20日(月度调仓)，长线池持有60日(季度调仓)，各自用匹配的持有期回测。")
