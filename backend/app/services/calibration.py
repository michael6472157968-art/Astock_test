"""共识权重校准引擎 — 批量运行五眼 + 前向命中率统计 + 网格搜索。

calibrate(): 随机采样N只股票，逐日跑5眼，统计前向N日命中率 → 新权重
grid_search(): 在置信度乘数、趋势阈值、冲突消解参数上做网格搜索
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("calibration")

EYES = ["candle", "indicator", "chan", "wave", "gann"]
TREND_LABELS = ["up", "down", "neutral"]
SIGNAL_LABELS = ["buy", "sell", "caution", "none"]

# horizon → forward_days 映射 (精扫 5/10/12/15/20/25/30/40d 确认拐点)
HORIZON_FORWARD = {"short": 5, "mid15": 15, "mid": 20, "long": 30, "xlong": 40}
# 每只眼的 horizon 声明
EYE_HORIZON = {
    "candle": "short",    # 形态1-5d兑现
    "indicator": "mid15", # 12→15d 跳涨3.6pp，之后平坦 → 甜点15d
    "chan": "long",       # 30d拐点，每+5d约+1.5pp线性爬坡
    "wave": "xlong",      # 40d sell 46.4%，浪型需要完整走完
    "gann": "long",       # 25→30d 跳1.9pp，30→40d仅+0.5pp → 拐点30d
}


# ═══════════════════════════════════════════════════════
# 校准主函数
# ═══════════════════════════════════════════════════════

async def calibrate(
    stock_codes: list[str] | None = None,
    sample_size: int = 300,
    forward_days: int = 0,  # 0 = 按 horizon 自动分配
    trend_threshold: float = 0.03,
    signal_threshold: float = 0.03,
) -> dict:
    """批量运行五眼前向验证，计算各眼命中率。

    当 forward_days=0 时，按各眼 horizon 分层验证:
      - candle (short)  → 5日前向
      - indicator (mid) → 10日前向
      - gann (mid)      → 10日前向
      - chan (long)     → 15日前向
      - wave (long)     → 15日前向
    """
    t0 = time.monotonic()
    use_per_eye_forward = (forward_days == 0)

    if stock_codes is None:
        stock_codes = await _sample_stocks(sample_size)
    else:
        stock_codes = stock_codes[:sample_size]

    if not stock_codes:
        return {"error": "无可用的股票样本"}

    from app.services.multi_eye import candle_eye, indicator_eye, chan_eye, wave_eye, gann_eye

    eye_funcs = {
        "candle": candle_eye, "indicator": indicator_eye,
        "chan": chan_eye, "wave": wave_eye, "gann": gann_eye,
    }

    max_fwd = max(HORIZON_FORWARD.values()) if use_per_eye_forward else forward_days

    trend_counts: dict[str, dict[str, dict[str, int]]] = {
        e: {p: {a: 0 for a in ["up", "down", "neutral"]} for p in ["up", "down", "neutral"]}
        for e in EYES
    }
    signal_counts: dict[str, dict[str, dict[str, int]]] = {
        e: {p: {a: 0 for a in ["hit", "miss"]} for p in ["buy", "sell", "caution", "none"]}
        for e in EYES
    }
    total_dates = 0
    usable_stocks = 0

    for code in stock_codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < max_fwd + 30:
            continue
        usable_stocks += 1

        for t in range(len(daily) - max_fwd):
            window = daily[:t + 1]
            if len(window) < 30:
                continue

            # 逐眼预测
            for ename, efunc in eye_funcs.items():
                try:
                    v = efunc(window)
                except Exception:
                    continue

                fwd = HORIZON_FORWARD[EYE_HORIZON[ename]] if use_per_eye_forward else forward_days
                if t + fwd >= len(daily):
                    continue
                future_ret = (_safe_close(daily, t + fwd) - _safe_close(daily, t)) / _safe_close(daily, t)

                # 标签标准化：收益 / (单日ATR × sqrt(fwd))
                # 价格漂移随窗口平方根增长，除以 sqrt(fwd) 让不同窗口的阈值可比
                atr_pct = _atr_pct(daily, t)
                if atr_pct > 0:
                    ret_norm = future_ret / (atr_pct * math.sqrt(fwd))
                else:
                    ret_norm = future_ret / max(trend_threshold, 1e-6)  # 兜底

                # 标准化后阈值：|ret_norm| > 0.5 表示弱方向性移动（约0.5个标准差）
                # 0.5 让不同窗口阈值可比，又不至于太严导致各眼命中率无法区分
                norm_threshold = 0.5
                if ret_norm > norm_threshold:
                    actual_trend = "up"
                elif ret_norm < -norm_threshold:
                    actual_trend = "down"
                else:
                    actual_trend = "neutral"

                if ret_norm > norm_threshold:
                    actual_signal = "buy"
                elif ret_norm < -norm_threshold:
                    actual_signal = "sell"
                else:
                    actual_signal = "none"

                trend_counts[ename][v.trend][actual_trend] += 1

                hit = (
                    (v.signal == "buy" and actual_signal == "buy") or
                    (v.signal == "sell" and actual_signal == "sell")
                )
                signal_counts[ename][v.signal]["hit" if hit else "miss"] += 1

            total_dates += 1

    # 计算命中率
    trend_hit_rates: dict[str, dict[str, float]] = {}
    signal_hit_rates: dict[str, dict[str, float]] = {}

    for ename in EYES:
        thr: dict[str, float] = {}
        for pred in ["up", "down", "neutral"]:
            total = sum(trend_counts[ename][pred].values())
            correct = trend_counts[ename][pred].get(pred, 0)
            thr[pred] = round(correct / total, 3) if total > 0 else 0.0
        trend_hit_rates[ename] = thr

        shr: dict[str, float] = {}
        for pred in ["buy", "sell", "caution", "none"]:
            total = sum(signal_counts[ename][pred].values())
            hits = signal_counts[ename][pred].get("hit", 0)
            shr[pred] = round(hits / total, 3) if total > 0 else 0.0
        signal_hit_rates[ename] = shr

    # 4. 组装新权重 (直接可用于 eye_weights.json)
    new_trend_weight: dict[str, dict[str, float]] = {}
    new_signal_weight: dict[str, dict[str, float]] = {}
    for ename in EYES:
        new_trend_weight[ename] = {
            "up": trend_hit_rates[ename].get("up", 0.5),
            "down": trend_hit_rates[ename].get("down", 0.5),
        }
        new_signal_weight[ename] = {
            "buy": signal_hit_rates[ename].get("buy", 0.5),
            "sell": signal_hit_rates[ename].get("sell", 0.5),
        }

    elapsed = round(time.monotonic() - t0, 1)

    result = {
        "sample_size": usable_stocks,
        "total_dates": total_dates,
        "forward_days": forward_days if not use_per_eye_forward else "per_eye",
        "eye_forward_days": {e: HORIZON_FORWARD[EYE_HORIZON[e]] for e in EYES} if use_per_eye_forward else {},
        "trend_threshold": trend_threshold,
        "trend_hit_rates": trend_hit_rates,
        "signal_hit_rates": signal_hit_rates,
        "new_weights": {
            "trend_weight": new_trend_weight,
            "signal_weight": new_signal_weight,
        },
        "elapsed_seconds": elapsed,
    }

    return result


# ═══════════════════════════════════════════════════════
# 网格搜索
# ═══════════════════════════════════════════════════════

async def grid_search(
    stock_codes: list[str] | None = None,
    sample_size: int = 150,
    forward_days: int = 5,
) -> dict:
    """网格搜索共识器最优参数组合。

    搜索维度:
    - confidence_multiplier: 置信度缩放系数
    - trend_share_threshold: 趋势判定所需分数占比
    - conflict_buy: 冲突消解的 buy 眼数阈值
    - conflict_sell: 冲突消解的 sell 眼数阈值
    """
    t0 = time.monotonic()

    if stock_codes is None:
        stock_codes = await _sample_stocks(sample_size)
    else:
        stock_codes = stock_codes[:sample_size]

    if not stock_codes:
        return {"error": "无可用的股票样本"}

    from app.services.multi_eye import candle_eye, indicator_eye, chan_eye, wave_eye, gann_eye

    eye_funcs = {
        "candle": candle_eye, "indicator": indicator_eye,
        "chan": chan_eye, "wave": wave_eye, "gann": gann_eye,
    }

    # 准备样本数据 (预测×实际对)
    samples: list[dict] = []
    for code in stock_codes:
        daily = await _load_daily_data_fast(code)
        if len(daily) < forward_days + 30:
            continue
        for t in range(len(daily) - forward_days):
            window = daily[:t + 1]
            if len(window) < 30:
                continue
            future_ret = (_safe_close(daily, t + forward_days) - _safe_close(daily, t)) / _safe_close(daily, t)
            atr_pct = _atr_pct(daily, t)
            if atr_pct > 0 and forward_days > 0:
                ret_norm = future_ret / (atr_pct * math.sqrt(forward_days))
            else:
                ret_norm = future_ret / 0.03  # 兜底

            eye_verdicts: dict[str, dict] = {}
            for ename, efunc in eye_funcs.items():
                try:
                    v = efunc(window)
                    eye_verdicts[ename] = {
                        "trend": v.trend, "signal": v.signal,
                        "confidence": v.confidence,
                    }
                except Exception:
                    continue
            if len(eye_verdicts) < 3:
                continue

            samples.append({
                "eyes": eye_verdicts,
                "future_ret": future_ret,
                "ret_norm": ret_norm,
            })

    if not samples:
        return {"error": "无有效样本"}

    # 加载校准后的单眼权重
    tw, sw = _load_weights_file()

    # 搜索空间
    conf_mults = [0.8, 0.9, 1.0, 1.1, 1.2]
    share_thresholds = [0.50, 0.55, 0.60, 0.65]
    conflicts = [(2, 2), (2, 1), (3, 2), (3, 1)]

    all_results = []
    best_score = -1.0
    best_params = {}

    for cm in conf_mults:
        for st in share_thresholds:
            for cb, cs in conflicts:
                trend_correct = 0
                trend_total = 0
                signal_correct = 0
                signal_total = 0

                for s in samples:
                    # Trend 投票
                    trend_scores = {"up": 0.0, "down": 0.0, "neutral": 0.0}
                    for ename, ev in s["eyes"].items():
                        eye_tw = tw.get(ename, {"up": 0.5, "down": 0.5})
                        w = ev["confidence"] * cm * eye_tw.get(ev["trend"], 0.3)
                        trend_scores[ev["trend"]] += w

                    total = sum(trend_scores.values())
                    if total > 0:
                        up_share = trend_scores["up"] / total
                        down_share = trend_scores["down"] / total
                    else:
                        up_share = down_share = 0

                    if up_share > st:
                        cons_trend = "up"
                    elif down_share > st:
                        cons_trend = "down"
                    else:
                        cons_trend = "neutral"

                    # Signal 投票
                    buy_eyes = sell_eyes = 0
                    signal_scores = {"buy": 0.0, "sell": 0.0, "caution": 0.0, "none": 0.0}
                    for ename, ev in s["eyes"].items():
                        eye_sw = sw.get(ename, {"buy": 0.5, "sell": 0.5})
                        sig = ev["signal"]
                        w = ev["confidence"] * cm * eye_sw.get(sig, 0.2) if sig != "none" else 0.3
                        if sig != "none":
                            signal_scores[sig] += w
                            if sig == "buy":
                                buy_eyes += 1
                            elif sig == "sell":
                                sell_eyes += 1

                    if buy_eyes >= cb and sell_eyes >= cs:
                        cons_signal = "caution"
                    else:
                        cons_signal = max(signal_scores, key=signal_scores.get)

                    # 评估趋势
                    actual_trend = "up" if s["ret_norm"] > 0.5 else ("down" if s["ret_norm"] < -0.5 else "neutral")
                    if cons_trend != "neutral":
                        trend_total += 1
                        if cons_trend == actual_trend:
                            trend_correct += 1

                    # 评估信号
                    actual_sig = "buy" if s["ret_norm"] > 0.5 else ("sell" if s["ret_norm"] < -0.5 else "none")
                    if cons_signal in ("buy", "sell"):
                        signal_total += 1
                        if cons_signal == actual_sig:
                            signal_correct += 1

                trend_acc = trend_correct / trend_total if trend_total > 0 else 0
                signal_acc = signal_correct / signal_total if signal_total > 0 else 0
                composite = 0.3 * trend_acc + 0.7 * signal_acc

                entry = {
                    "params": {
                        "confidence_multiplier": cm,
                        "trend_share_threshold": st,
                        "conflict_buy": cb,
                        "conflict_sell": cs,
                    },
                    "trend_accuracy": round(trend_acc, 3),
                    "signal_accuracy": round(signal_acc, 3),
                    "composite_score": round(composite, 3),
                }
                all_results.append(entry)

                if composite > best_score:
                    best_score = composite
                    best_params = entry["params"]

    elapsed = round(time.monotonic() - t0, 1)

    return {
        "sample_size": len(samples),
        "best_params": best_params,
        "best_score": round(best_score, 3),
        "total_combinations": len(all_results),
        "all_results": sorted(all_results, key=lambda x: x["composite_score"], reverse=True),
        "elapsed_seconds": elapsed,
    }


# ═══════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════

async def _sample_stocks(n: int) -> list[str]:
    """从 stocks 表随机采样 — 排除上市 < 1 年的新股和 ST 股。"""
    year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

    async with async_session() as sess:
        r = await sess.execute(
            text(
                "SELECT ts_code FROM stocks "
                "WHERE list_date < :min_date AND name NOT LIKE '%ST%' "
                "ORDER BY RANDOM() LIMIT :n"
            ),
            {"min_date": year_ago, "n": min(n * 2, 1000)},  # 多取一些 + 兜底
        )
        rows = r.fetchall()

    # 打乱取前 N
    codes = [row[0] for row in rows]
    random.shuffle(codes)
    return codes[:n]


async def _load_daily_data_fast(ts_code: str) -> list[dict]:
    """仅从 DB 加载日线 (不触发 Tushare live fallback)，适合批量校准。"""
    async with async_session() as sess:
        r = await sess.execute(
            text(
                "SELECT trade_date, open, high, low, close, volume, pct_chg "
                "FROM stock_daily WHERE ts_code=:c ORDER BY trade_date ASC"
            ),
            {"c": ts_code},
        )
        rows = r.mappings().all()

    daily = []
    for row in rows:
        daily.append({
            "trade_date": str(row["trade_date"]),
            "open": float(row["open"] or 0),
            "high": float(row["high"] or 0),
            "low": float(row["low"] or 0),
            "close": float(row["close"] or 0),
            "volume": float(row["volume"] or 0),
            "pct_chg": float(row["pct_chg"] or 0),
        })
    return daily


def _safe_close(daily: list[dict], idx: int) -> float:
    if idx < 0 or idx >= len(daily):
        return 0.0
    return float(daily[idx].get("close", 0) or 0)


def _atr_pct(daily: list[dict], idx: int, period: int = 14) -> float:
    """截至 idx 的 ATR(period) / close，返回单日平均波幅的百分比。

    只用 idx 及之前的数据，无前视偏差。ATR 用简单平均（标签标准化足够）。
    """
    if idx < period:
        return 0.0
    trs: list[float] = []
    for i in range(idx - period + 1, idx + 1):
        h = float(daily[i].get("high", 0) or 0)
        l = float(daily[i].get("low", 0) or 0)
        pc = float(daily[i - 1].get("close", 0) or 0) if i > 0 else h
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if not trs:
        return 0.0
    atr = sum(trs) / len(trs)
    close = float(daily[idx].get("close", 0) or 0)
    if close <= 0 or atr <= 0:
        return 0.0
    return atr / close


def _load_weights_file() -> tuple[dict, dict]:
    """加载 eye_weights.json 中的单眼权重。"""
    weights_path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'data', 'eye_weights.json'
    )
    try:
        with open(weights_path, encoding='utf-8') as f:
            data = json.load(f)
        return data.get("trend_weight", {}), data.get("signal_weight", {})
    except Exception:
        return {}, {}


async def save_weights(weights: dict) -> bool:
    """将校准结果写入 eye_weights.json。"""
    weights_path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'data', 'eye_weights.json'
    )
    try:
        existing = {}
        if os.path.exists(weights_path):
            with open(weights_path, encoding='utf-8') as f:
                existing = json.load(f)

        existing["version"] = existing.get("version", 1) + 1
        existing["calibrated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        existing["sample_size"] = weights.get("sample_size", 0)
        existing["forward_days"] = weights.get("forward_days", 5)
        existing["trend_threshold"] = weights.get("trend_threshold", 0.03)
        nw = weights.get("new_weights", {})
        existing["trend_weight"] = nw.get("trend_weight", existing.get("trend_weight", {}))
        existing["signal_weight"] = nw.get("signal_weight", existing.get("signal_weight", {}))

        with open(weights_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        # 清除 multi_eye 的 mtime 缓存，下次调用自动重载
        try:
            from app.services import multi_eye
            multi_eye._WEIGHTS_CACHE = None
            multi_eye._WEIGHTS_CACHE_MTIME = 0
        except Exception:
            pass

        return True
    except Exception as e:
        logger.error("保存权重文件失败: %s", e)
        return False
