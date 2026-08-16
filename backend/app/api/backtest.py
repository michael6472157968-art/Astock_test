"""策略回测 API — 15 策略统一执行 + 结果缓存 + 历史记录。"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.core.cache import cache_get, cache_set
from app.core.database import async_session
from app.core.security import require_tier
from app.models.orm.models import BacktestResult
from app.models.schemas.common import APIResponse
from app.services.backtest_engine import cache_key as bt_cache_key
from app.services.factor_meta import list_factors_grouped
from app.services.factor_engine import diagnose as run_diagnose, match as run_match

logger = logging.getLogger("backtest_api")
router = APIRouter(prefix="/api/v1/backtest", tags=["策略回测"])

_MIN_DAYS = 20
_MAX_DB_BACKFILL = 120
_CACHE_TTL = 3600 * 6  # 6 hours — expires after market close


class MatchRequest(BaseModel):
    stock_code: str = Field(..., min_length=1)


class RunRequest(BaseModel):
    stock_code: str = Field(..., min_length=1)
    factor_id: str = Field(default="f1")
    strategy: str = Field(default="")  # 向后兼容，已废弃(原技术指标策略)


@router.post("/run")
async def run_backtest(req: RunRequest, user: dict = Depends(require_tier(2))):
    """因子诊断：计算该股在选定因子上的全市场分位，给出结论。"""
    uid = user["user_id"]
    ts_code = req.stock_code
    fid = req.factor_id or "f1"

    result = await run_diagnose(ts_code, fid)
    if "error" in result:
        return APIResponse(code=400, message=result["error"], data=None, timestamp=int(time.time()))

    await _save_history(uid, fid, ts_code, result)
    return APIResponse(data=result, timestamp=int(time.time()))


@router.post("/match")
async def match_factors(req: MatchRequest, user: dict = Depends(require_tier(2))):
    """因子自动匹配：推荐该股当前最突出的适配因子。"""
    result = await run_match(req.stock_code)
    return APIResponse(data=result, timestamp=int(time.time()))


async def _save_history(uid: int, strategy: str, code: str, result: dict) -> None:
    try:
        async with async_session() as sess:
            rec = BacktestResult(
                user_id=uid,
                strategy_name=strategy,
                strategy_params=json.dumps({"stock_code": code}),
                result_json=json.dumps(result, ensure_ascii=False),
            )
            sess.add(rec)
            await sess.commit()
    except Exception:
        logger.warning("Failed to save backtest history", exc_info=True)


@router.get("/strategies")
async def list_strategies():
    """返回因子库元信息（按维度分组），供 Stockwin 因子库展示。"""
    grouped = list_factors_grouped()
    total = sum(len(v) for v in grouped.values())
    return APIResponse(data={"categories": grouped, "total": total}, timestamp=int(time.time()))


async def _load_daily_data(ts_code: str) -> list[dict]:
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT trade_date, open, high, low, close, volume, pct_chg FROM stock_daily WHERE ts_code=:c"),
            {"c": ts_code},
        )
        rows = r.mappings().all()

    seen: set[str] = set()
    daily = []
    for row in rows:
        dt = row["trade_date"]
        if dt not in seen:
            seen.add(dt)
            daily.append({
                "trade_date": dt,
                "open": float(row["open"] or 0),
                "high": float(row["high"] or 0),
                "low": float(row["low"] or 0),
                "close": float(row["close"] or 0),
                "volume": float(row["volume"] or 0),
                "pct_chg": float(row["pct_chg"] or 0),
            })

    daily.sort(key=lambda x: x["trade_date"])

    need_days = max(0, _MAX_DB_BACKFILL - len(daily))
    if need_days <= 0:
        return daily

    end_date = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    start_date = (date.today() - timedelta(days=need_days * 2)).strftime("%Y%m%d")

    try:
        from app.services.tushare_client import get_daily_data
        rows_raw = await get_daily_data(ts_code, start_date, end_date)
    except Exception:
        return daily

    if not rows_raw:
        return daily

    new_rows = []
    for rd in rows_raw:
        dt = str(rd.get("trade_date", ""))
        if dt not in seen:
            seen.add(dt)
            daily.append({
                "trade_date": dt,
                "open": float(rd.get("open", 0) or 0),
                "high": float(rd.get("high", 0) or 0),
                "low": float(rd.get("low", 0) or 0),
                "close": float(rd.get("close", 0) or 0),
                "volume": float(rd.get("vol", 0) or 0),
                "pct_chg": float(rd.get("pct_chg", 0) or 0),
            })
            new_rows.append(rd)

    if new_rows:
        async with async_session() as sess:
            for rd in new_rows:
                await sess.execute(text("""
                    INSERT OR IGNORE INTO stock_daily
                        (ts_code, trade_date, open, high, low, close, pre_close,
                         change, pct_chg, volume, amount)
                    VALUES (:ts_code, :trade_date, :open, :high, :low, :close,
                            :pre_close, :change, :pct_chg, :volume, :amount)
                """), {
                    "ts_code": rd.get("ts_code", ts_code),
                    "trade_date": str(rd.get("trade_date", "")),
                    "open": float(rd.get("open", 0) or 0),
                    "high": float(rd.get("high", 0) or 0),
                    "low": float(rd.get("low", 0) or 0),
                    "close": float(rd.get("close", 0) or 0),
                    "pre_close": float(rd.get("pre_close", 0) or 0),
                    "change": float(rd.get("change", 0) or 0),
                    "pct_chg": float(rd.get("pct_chg", 0) or 0),
                    "volume": float(rd.get("vol", 0) or 0),
                    "amount": float(rd.get("amount", 0) or 0),
                })
            await sess.commit()

    daily.sort(key=lambda x: x["trade_date"])
    return daily


@router.get("/history")
async def history(user: dict = Depends(require_tier(2))):
    async with async_session() as sess:
        r = await sess.execute(
            select(BacktestResult)
            .where(BacktestResult.user_id == user["user_id"])
            .order_by(BacktestResult.created_at.desc())
            .limit(20)
        )
        recs = r.scalars().all()
        items = []
        for rec in recs:
            items.append({
                "id": rec.id,
                "strategy": rec.strategy_name,
                "result": json.loads(rec.result_json),
                "created_at": rec.created_at.isoformat() if rec.created_at else "",
            })
    return APIResponse(data={"total": len(items), "items": items}, timestamp=int(time.time()))


# ════════════════════════ 五眼共识 API ════════════════════════

class ConsensusRequest(BaseModel):
    stock_code: str = Field(..., min_length=1)


@router.post("/consensus")
async def run_consensus(req: ConsensusRequest):
    ts_code = req.stock_code

    ck = bt_cache_key(ts_code, "consensus_v2")
    cached = await cache_get(ck)
    if cached is not None:
        return APIResponse(data=cached, timestamp=int(time.time()))

    daily = await _load_daily_data(ts_code)
    if len(daily) < _MIN_DAYS:
        return APIResponse(
            code=400,
            message=f"历史数据不足(当前{len(daily)}天，至少需{_MIN_DAYS}天)",
            data=None,
            timestamp=int(time.time()),
        )

    from app.services.multi_eye import consensus as run_consensus_engine
    result = run_consensus_engine(daily)

    eyes_dict = {}
    for name, e in result.eyes.items():
        eyes_dict[name] = {
            "eye": e.eye, "lens": e.lens,
            "trend": e.trend, "trend_detail": e.trend_detail,
            "position": e.position, "position_detail": e.position_detail,
            "signal": e.signal, "signal_detail": e.signal_detail,
            "confidence": e.confidence,
            "horizon": e.horizon,
        }

    data = {
        "stock_code": ts_code,
        "latest_date": daily[-1]["trade_date"] if daily else "",
        "data_days": len(daily),
        "eyes": eyes_dict,
        "trend": result.trend,
        "position": result.position,
        "signal": result.signal,
        "summary": result.summary,
        "plain_summary": result.plain_summary,
        "retreat_alert": result.retreat_alert,
    }

    await cache_set(ck, data, ttl=_CACHE_TTL)
    return APIResponse(data=data, timestamp=int(time.time()))


# ═══════════════════════════════════════════════════════
# 权重校准 API
# ═══════════════════════════════════════════════════════

class CalibrateRequest(BaseModel):
    sample_size: int = Field(default=300, ge=50, le=500)
    forward_days: int = Field(default=0, ge=0, le=40)  # 0 = 按 horizon 自动分配
    save_weights: bool = Field(default=False)


class GridSearchRequest(BaseModel):
    sample_size: int = Field(default=150, ge=50, le=300)


@router.post("/calibrate")
async def run_calibrate(req: CalibrateRequest, user: dict = Depends(require_tier(3))):
    """运行五眼权重校准 — 管理员权限。"""
    from app.services.calibration import calibrate as run_cal, save_weights

    result = await run_cal(
        sample_size=req.sample_size,
        forward_days=req.forward_days,
    )

    if "error" in result:
        return APIResponse(code=400, message=result["error"], data=None, timestamp=int(time.time()))

    if req.save_weights:
        ok = await save_weights(result)
        result["weights_saved"] = ok

    return APIResponse(data=result, timestamp=int(time.time()))


@router.post("/calibrate/search")
async def run_grid_search(req: GridSearchRequest, user: dict = Depends(require_tier(3))):
    """网格搜索最优共识参数 — 管理员权限。"""
    from app.services.calibration import grid_search as run_gs

    result = await run_gs(sample_size=req.sample_size)

    if "error" in result:
        return APIResponse(code=400, message=result["error"], data=None, timestamp=int(time.time()))

    return APIResponse(data=result, timestamp=int(time.time()))
