"""自选股异动预警 API——支持自选股管理（本地JSON+DB双写）、预警配置与通知。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.database import async_session
from app.core.settings import get_settings
from app.models.orm.models import AlertNotification, UserAlertConfig, UserFavorite
from app.models.schemas.common import APIResponse
from app.services import user_data
from sqlalchemy import select

router = APIRouter(prefix="/api/v1/alerts", tags=["预警"])
_settings = get_settings()


def _get_user(request: Request) -> tuple[int | None, int]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, 0
    try:
        from app.core.security import decode_token
        payload = decode_token(auth[7:])
        return int(payload["sub"]), payload.get("tier", 0)
    except Exception:
        return None, 0


# ── 自选股 ──

@router.get("/favorites")
async def list_favorites(request: Request):
    uid, tier = _get_user(request)
    if not uid:
        return APIResponse(data={"total": 0, "items": []}, timestamp=int(time.time()))

    stocks = user_data.get_favorites(uid)
    items = [{"id": s.get("added_at", ""), "stock_code": s["stock_code"], "stock_name": s.get("stock_name", ""), "added_at": s.get("added_at", "")} for s in stocks]
    return APIResponse(data={"total": len(items), "items": items}, timestamp=int(time.time()))


@router.get("/favorites-quotes")
async def favorites_quotes(codes: str = "", request: Request = None):
    """批量获取自选股最新行情数据"""
    uid, tier = _get_user(request)
    if not uid or not codes:
        return APIResponse(data={"quotes": {}}, timestamp=int(time.time()))

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    quotes = {}
    async with async_session() as session:
        from sqlalchemy import text as _text
        for code in code_list:
            r = await session.execute(
                _text("SELECT s.name, d.close, d.pct_chg FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code WHERE d.ts_code = :code ORDER BY d.trade_date DESC LIMIT 1"),
                {"code": code}
            )
            row = r.fetchone()
            if row:
                quotes[code] = {"name": row[0], "close": round(float(row[1]), 2) if row[1] else None, "pct_chg": round(float(row[2]), 2) if row[2] else 0}

    return APIResponse(data={"quotes": quotes}, timestamp=int(time.time()))


class AddFavRequest(BaseModel):
    stock_code: str = Field(..., min_length=1)


@router.post("/favorites")
async def add_favorite(req: AddFavRequest, request: Request):
    uid, tier = _get_user(request)
    if not uid:
        raise HTTPException(401, "请先登录")

    added = user_data.add_favorite(uid, req.stock_code, req.stock_name if hasattr(req, "stock_name") else "")
    if not added:
        return APIResponse(data={"message": "已在自选列表中"}, timestamp=int(time.time()))

    return APIResponse(data={"message": "已添加"}, timestamp=int(time.time()))


@router.delete("/favorites/{fav_id}")
async def remove_favorite(fav_id: str, request: Request):
    """fav_id 在这里是 stock_code（去重键），非自增ID"""
    uid, tier = _get_user(request)
    if not uid:
        raise HTTPException(401, "请先登录")

    removed = user_data.remove_favorite(uid, fav_id)
    if not removed:
        raise HTTPException(404, "自选记录不存在")
    return APIResponse(data={"message": "已删除"}, timestamp=int(time.time()))


# ── 预警配置 ──

@router.get("/configs")
async def list_configs(request: Request):
    uid, tier = _get_user(request)
    if not uid:
        return APIResponse(data={"total": 0, "items": []}, timestamp=int(time.time()))

    async with async_session() as session:
        r = await session.execute(
            select(UserAlertConfig).where(UserAlertConfig.user_id == uid)
        )
        cfgs = r.scalars().all()
        items = [{"id": c.id, "stock_code": c.ts_code, "alert_types": c.alert_types, "is_active": c.is_active} for c in cfgs]
        return APIResponse(data={"total": len(items), "items": items}, timestamp=int(time.time()))


class AlertConfigRequest(BaseModel):
    stock_code: str
    alert_types: str = "[]"  # JSON数组字符串
    is_active: int = 1


@router.post("/configs")
async def create_config(req: AlertConfigRequest, request: Request):
    uid, tier = _get_user(request)
    if not uid:
        raise HTTPException(401, "请先登录")

    async with async_session() as session:
        cfg = UserAlertConfig(user_id=uid, ts_code=req.stock_code, alert_types=req.alert_types, is_active=req.is_active)
        session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
        return APIResponse(data={"id": cfg.id, "message": "已创建"}, timestamp=int(time.time()))


@router.delete("/configs/{cfg_id}")
async def delete_config(cfg_id: int, request: Request):
    uid, tier = _get_user(request)
    if not uid:
        raise HTTPException(401, "请先登录")

    async with async_session() as session:
        r = await session.execute(
            select(UserAlertConfig).where(UserAlertConfig.id == cfg_id, UserAlertConfig.user_id == uid)
        )
        cfg = r.scalar_one_or_none()
        if not cfg:
            raise HTTPException(404, "配置不存在")
        await session.delete(cfg)
        await session.commit()
        return APIResponse(data={"message": "已删除"}, timestamp=int(time.time()))


# ── 通知 ──

@router.get("/notifications")
async def list_notifications(request: Request):
    uid, tier = _get_user(request)
    if not uid:
        return APIResponse(data={"total": 0, "items": []}, timestamp=int(time.time()))

    async with async_session() as session:
        r = await session.execute(
            select(AlertNotification).where(AlertNotification.user_id == uid).order_by(AlertNotification.created_at.desc()).limit(50)
        )
        notifs = r.scalars().all()
        items = [{"id": n.id, "ts_code": n.ts_code, "stock_name": n.stock_name, "alert_type": n.alert_type, "content": n.content, "is_read": n.is_read, "created_at": n.created_at.isoformat() if n.created_at else ""} for n in notifs]
        return APIResponse(data={"total": len(items), "items": items}, timestamp=int(time.time()))
