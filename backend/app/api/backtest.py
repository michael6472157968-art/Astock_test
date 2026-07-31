"""策略回测 API。"""

from __future__ import annotations

import time

from fastapi import APIRouter

from app.models.schemas.common import APIResponse

router = APIRouter(prefix="/api/v1/backtest", tags=["策略回测"])


@router.post("/run")
async def run_backtest():
    return APIResponse(data={"task_id": "", "status": "pending"}, timestamp=int(time.time()))


@router.get("/history")
async def backtest_history():
    return APIResponse(data={"total": 0, "items": []}, timestamp=int(time.time()))
