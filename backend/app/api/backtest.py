"""策略回测 API — POST 同步返回结果 + GET 历史记录。数据不足时自动从 Tushare 补拉。"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.core.database import async_session
from app.core.security import require_tier
from app.models.orm.models import BacktestResult, StockDaily
from app.models.schemas.common import APIResponse
from app.services.backtest_engine import run as run_engine

router = APIRouter(prefix="/api/v1/backtest", tags=["策略回测"])

_MIN_DAYS = 20
_MAX_DB_BACKFILL = 120  # 补拉半年日线，确保均线/RSI有足够历史


class RunRequest(BaseModel):
    stock_code: str = Field(..., min_length=1)
    strategy: str = Field(default="ma_cross")


@router.post("/run")
async def run_backtest(req: RunRequest, user: dict = Depends(require_tier(2))):
    uid = user["user_id"]

    daily = await _load_daily_data(req.stock_code)

    if len(daily) < _MIN_DAYS:
        return APIResponse(code=400, message=f"历史数据不足(当前{len(daily)}天，至少需{_MIN_DAYS}天)", data=None, timestamp=int(time.time()))

    result = run_engine(daily, strategy=req.strategy)

    async with async_session() as sess:
        rec = BacktestResult(
            user_id=uid,
            strategy_name=req.strategy,
            strategy_params=json.dumps({"stock_code": req.stock_code}),
            result_json=json.dumps(result, ensure_ascii=False),
        )
        sess.add(rec)
        await sess.commit()

    return APIResponse(data=result, timestamp=int(time.time()))


async def _load_daily_data(ts_code: str) -> list[dict]:
    """加载个股日线数据。DB 不足时从 Tushare 按日批量补拉并合并去重。"""
    async with async_session() as sess:
        r = await sess.execute(
            text("SELECT trade_date,close,volume FROM stock_daily WHERE ts_code=:c"),
            {"c": ts_code},
        )
        rows = r.mappings().all()

    seen: set[str] = set()
    daily = []
    for row in rows:
        dt = row["trade_date"]
        if dt not in seen:
            seen.add(dt)
            daily.append({"trade_date": dt, "close": row["close"], "volume": row["volume"]})

    # 按日期排序
    daily.sort(key=lambda x: x["trade_date"])

    need_days = max(0, _MAX_DB_BACKFILL - len(daily))
    if need_days <= 0:
        return daily

    # 从 Tushare 补拉历史数据
    end_date = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    start_date = (date.today() - timedelta(days=need_days * 2)).strftime("%Y%m%d")  # 多取一些，覆盖停牌日

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
            daily.append({"trade_date": dt, "close": float(rd.get("close", 0) or 0),
                          "volume": float(rd.get("vol", 0) or 0)})
            new_rows.append(rd)

    # 写入 DB
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

    # 重新排序
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
