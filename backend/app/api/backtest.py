"""策略回测 API — POST 同步返回结果 + GET 历史记录。"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.database import async_session
from app.core.security import require_tier
from app.models.orm.models import BacktestResult
from app.models.schemas.common import APIResponse
from app.services.backtest_engine import run as run_engine

router = APIRouter(prefix="/api/v1/backtest", tags=["策略回测"])


class RunRequest(BaseModel):
    stock_code: str = Field(..., min_length=1)
    strategy: str = Field(default="ma_cross")


@router.post("/run")
async def run_backtest(req: RunRequest, user: dict = Depends(require_tier(2))):
    uid = user["user_id"]

    async with async_session() as sess:
        from sqlalchemy import text
        r = await sess.execute(
            text("SELECT trade_date,close,volume FROM stock_daily WHERE ts_code=:c ORDER BY trade_date ASC"),
            {"c": req.stock_code},
        )
        rows = r.fetchall()

    if len(rows) < 60:
        return APIResponse(code=400, message="历史数据不足(至少需60个交易日)", data=None, timestamp=int(time.time()))

    daily = [{"trade_date": r[0], "close": r[1], "volume": r[2]} for r in rows]
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
