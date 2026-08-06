"""自选股异动预警 API——支持自选股管理（本地JSON+DB双写）、预警配置与通知。"""

from __future__ import annotations

import math
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.database import async_session
from app.core.settings import get_settings
from app.middleware.auth_middleware import require_auth
from app.models.orm.models import AlertNotification, UserAlertConfig, UserFavorite, UserFavoriteGroup
from app.models.schemas.common import APIResponse
from app.services import user_data
from sqlalchemy import select

router = APIRouter(prefix="/api/v1/alerts", tags=["预警"])
_settings = get_settings()


# ── 自选股 ──

@router.get("/favorites")
async def list_favorites(request: Request, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        return APIResponse(data={"total": 0, "items": [], "quota": {"current": 0, "max": 0}}, timestamp=int(time.time()))

    stocks = await user_data.get_favorites(uid)
    items = [{"id": s.get("added_at", ""), "stock_code": s["stock_code"],
              "stock_name": s.get("stock_name", ""), "added_at": s.get("added_at", ""),
              "sort_order": s.get("sort_order", 0), "group_id": s.get("group_id")} for s in stocks]
    quota_max = user_data.get_quota(tier)
    return APIResponse(data={
        "total": len(items), "items": items,
        "quota": {"current": len(items), "max": quota_max},
    }, timestamp=int(time.time()))


@router.get("/favorites-quotes")
async def favorites_quotes(codes: str = "", user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid or not codes:
        return APIResponse(data={"quotes": {}}, timestamp=int(time.time()))

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    quotes = {}
    async with async_session() as session:
        from sqlalchemy import text as _text
        for code in code_list:
            r = await session.execute(
                _text("""SELECT s.name, d.close, d.pct_chg, d.open, d.high, d.low, d.volume
                         FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code
                         WHERE d.ts_code = :code ORDER BY d.trade_date DESC LIMIT 1"""),
                {"code": code}
            )
            row = r.fetchone()
            if not row:
                quotes[code] = {"name": code, "close": None, "pct_chg": 0,
                                "sparkline": [], "score": None, "signals": [], "risk": None}
                continue

            r2 = await session.execute(
                _text("""SELECT trade_date, open, close, high, low, volume
                         FROM stock_daily WHERE ts_code = :code
                         ORDER BY trade_date DESC LIMIT 20"""),
                {"code": code}
            )
            daily_rows = r2.fetchall()
            daily_rows.reverse()

            sparkline = [
                {"date": d[0], "open": round(float(d[1]), 2), "close": round(float(d[2]), 2),
                 "high": round(float(d[3]), 2), "low": round(float(d[4]), 2), "vol": int(d[5] or 0)}
                for d in daily_rows
            ]

            closes = [d["close"] for d in sparkline]
            tech = _compute_light_score(closes)

            quotes[code] = {
                "name": row[0], "close": round(float(row[1]), 2) if row[1] else None,
                "pct_chg": round(float(row[2]), 2) if row[2] else 0,
                "open": round(float(row[3]), 2) if row[3] else None,
                "high": round(float(row[4]), 2) if row[4] else None,
                "low": round(float(row[5]), 2) if row[5] else None,
                "volume": int(row[6] or 0),
                "sparkline": sparkline,
                "score": tech["score"],
                "signals": tech["signals"],
                "risk": tech["risk"],
            }

    await _attach_risk_data(quotes, code_list)

    return APIResponse(data={"quotes": quotes}, timestamp=int(time.time()))


# ── 轻量技术评分（纯Python，不依赖诊股引擎）──

def _sma(values, n):
    out = []
    for i in range(len(values)):
        if i < n - 1:
            out.append(None)
        else:
            out.append(sum(values[i - n + 1:i + 1]) / n)
    return out


def _compute_light_score(closes: list[float]) -> dict:
    """基于近20日收盘价计算技术评分、信号和风险等级。"""
    n = len(closes)
    if n < 10:
        return {"score": None, "signals": [], "risk": None}

    signals = []
    score = 50

    # 均线
    ma5 = closes[-1] - (sum(closes[-5:]) / 5) if n >= 5 else 0
    ma10 = closes[-1] - (sum(closes[-10:]) / 10) if n >= 10 else 0
    if ma5 > 0:
        score += 8
        if ma5 > ma10 > 0:
            signals.append("多头排列")
            score += 5
    else:
        score -= 8
        if ma5 < ma10 < 0:
            signals.append("空头排列")
            score -= 5

    # 涨跌趋势
    if n >= 5:
        chg5 = (closes[-1] / closes[-5] - 1) * 100 if closes[-5] else 0
        if chg5 > 5:
            signals.append(f"5日+{chg5:.1f}%")
            score += 5
        elif chg5 < -5:
            signals.append(f"5日{chg5:.1f}%")
            score -= 5

    if n >= 10:
        chg10 = (closes[-1] / closes[-10] - 1) * 100 if closes[-10] else 0
        if chg10 > 10:
            signals.append(f"10日+{chg10:.1f}%")
            score += 4
        elif chg10 < -10:
            signals.append(f"10日{chg10:.1f}%")
            score -= 4

    # 量价关系（最近5日量增价升）
    if n >= 5:
        vol_up = closes[-1] > closes[-5]
        price_up = closes[-1] > closes[-2] if n >= 2 else False
        if vol_up and price_up:
            signals.append("量价齐升")
            score += 3
        elif not vol_up and not price_up:
            signals.append("缩量回调")
            score -= 2

    # RSI 快速估算
    rsi = _quick_rsi(closes[-8:]) if n >= 8 else 50
    if rsi > 70:
        signals.append(f"RSI超买{rsi:.0f}")
        score -= 5
    elif rsi < 30:
        signals.append(f"RSI超卖{rsi:.0f}")
        score += 8
    elif rsi > 60:
        score += 3
    elif rsi < 40:
        score -= 3

    # 布林带位置
    boll_pos = _bollinger_position(closes)
    if boll_pos is not None:
        if boll_pos > 0.9:
            signals.append("布林上轨")
            score -= 4
        elif boll_pos < 0.1:
            signals.append("布林下轨")
            score += 6

    score = max(1, min(99, int(score)))

    if score >= 75:
        risk = "低风险"
    elif score >= 60:
        risk = "中低风险"
    elif score >= 40:
        risk = "中风险"
    elif score >= 25:
        risk = "中高风险"
    else:
        risk = "高风险"

    # 去重 + 裁剪信号
    return {"score": score, "signals": signals[:5], "risk": risk}


def _quick_rsi(closes: list[float]) -> float:
    gains, losses = 0, 0
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        if chg > 0:
            gains += chg
        else:
            losses += abs(chg)
    if gains + losses == 0:
        return 50
    avg_gain = gains / len(closes)
    avg_loss = losses / len(closes)
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def _bollinger_position(closes: list[float]) -> float | None:
    n = len(closes)
    if n < 20:
        return None
    ma20 = sum(closes[-20:]) / 20
    var = sum((c - ma20) ** 2 for c in closes[-20:]) / 20
    std = math.sqrt(var)
    upper = ma20 + 2 * std
    lower = ma20 - 2 * std
    if upper - lower == 0:
        return None
    return round((closes[-1] - lower) / (upper - lower), 3)


async def _attach_risk_data(quotes: dict, code_list: list[str]) -> None:
    """批量查询 risk_list_results 表，附加每个股票的最近风险记录。"""
    from sqlalchemy import text as _text

    for code in code_list:
        if code not in quotes:
            continue
        try:
            async with async_session() as session:
                r = await session.execute(
                    _text("""SELECT risk_category, risk_detail FROM risk_list_results
                             WHERE ts_code = :code ORDER BY calc_date DESC LIMIT 2"""),
                    {"code": code}
                )
                rows = r.fetchall()
                if rows:
                    cats = {row[0] for row in rows}
                    if "st_risk" in cats:
                        quotes[code]["risk"] = "高风险(ST)"
                        quotes[code]["risk_tags"] = ["ST退市风险"]
                    elif "surge_overheat" in cats:
                        quotes[code]["risk"] = quotes[code].get("risk", "中风险")
                        quotes[code]["risk_tags"] = quotes[code].get("risk_tags", []) + ["连板过热"]
                    elif "cliff_drop" in cats:
                        quotes[code]["risk"] = "中高风险"
                        quotes[code]["risk_tags"] = quotes[code].get("risk_tags", []) + ["断崖下跌"]
                    elif "high_turnover" in cats:
                        quotes[code]["risk_tags"] = quotes[code].get("risk_tags", []) + ["高换手异动"]
                    elif "volume_drain" in cats:
                        quotes[code]["risk_tags"] = quotes[code].get("risk_tags", []) + ["缩量阴跌"]
                    else:
                        quotes[code]["risk_tags"] = quotes[code].get("risk_tags", []) + [rows[0][0]]
        except Exception:
            pass


class AddFavRequest(BaseModel):
    stock_code: str = Field(..., min_length=1)


@router.post("/favorites")
async def add_favorite(req: AddFavRequest, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    ok, msg = await user_data.add_favorite(uid, req.stock_code, "", tier)
    if not ok:
        raise HTTPException(400, msg)

    return APIResponse(data={"message": msg}, timestamp=int(time.time()))


@router.delete("/favorites/{fav_id}")
async def remove_favorite(fav_id: str, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    removed = await user_data.remove_favorite(uid, fav_id)
    if not removed:
        raise HTTPException(404, "自选记录不存在")
    return APIResponse(data={"message": "已删除"}, timestamp=int(time.time()))


class ReorderRequest(BaseModel):
    ordered_codes: list[str] = Field(..., min_length=0)


@router.put("/favorites/reorder")
async def reorder_favorites(req: ReorderRequest, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    await user_data.reorder_favorites(uid, req.ordered_codes)
    return APIResponse(data={"message": "已更新排序"}, timestamp=int(time.time()))


# ── 分组管理 ──

class CreateGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=30)


class RenameGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=30)


class ReorderGroupsRequest(BaseModel):
    ordered_ids: list[int] = Field(..., min_length=0)


class MoveFavRequest(BaseModel):
    group_id: int | None = None


@router.get("/favorites/groups")
async def list_groups(user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    groups, ungrouped_count = await user_data.get_groups(uid)
    return APIResponse(data={
        "groups": groups,
        "ungrouped_count": ungrouped_count,
    }, timestamp=int(time.time()))


@router.post("/favorites/groups")
async def create_group(req: CreateGroupRequest, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    ok, msg, gid = await user_data.create_group(uid, req.name)
    if not ok:
        raise HTTPException(400, msg)
    return APIResponse(data={"id": gid, "message": msg}, timestamp=int(time.time()))


@router.put("/favorites/groups/{group_id}")
async def rename_group(group_id: int, req: RenameGroupRequest, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    ok = await user_data.rename_group(uid, group_id, req.name)
    if not ok:
        raise HTTPException(404, "分组不存在")
    return APIResponse(data={"message": "已重命名"}, timestamp=int(time.time()))


@router.delete("/favorites/groups/{group_id}")
async def delete_group(group_id: int, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    ok = await user_data.delete_group(uid, group_id)
    if not ok:
        raise HTTPException(404, "分组不存在")
    return APIResponse(data={"message": "已删除"}, timestamp=int(time.time()))


@router.put("/favorites/groups/reorder")
async def reorder_groups(req: ReorderGroupsRequest, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    await user_data.reorder_groups(uid, req.ordered_ids)
    return APIResponse(data={"message": "已更新分组排序"}, timestamp=int(time.time()))


@router.put("/favorites/{stock_code}/move")
async def move_favorite(stock_code: str, req: MoveFavRequest, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    ok = await user_data.move_to_group(uid, stock_code, req.group_id)
    if not ok:
        raise HTTPException(404, "自选记录不存在")
    return APIResponse(data={"message": "已移动"}, timestamp=int(time.time()))


# ── 预警配置 ──

@router.get("/configs")
async def list_configs(user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
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
async def create_config(req: AlertConfigRequest, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    async with async_session() as session:
        cfg = UserAlertConfig(user_id=uid, ts_code=req.stock_code, alert_types=req.alert_types, is_active=req.is_active)
        session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
        return APIResponse(data={"id": cfg.id, "message": "已创建"}, timestamp=int(time.time()))


@router.delete("/configs/{cfg_id}")
async def delete_config(cfg_id: int, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
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
async def list_notifications(user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        return APIResponse(data={"total": 0, "items": []}, timestamp=int(time.time()))

    async with async_session() as session:
        r = await session.execute(
            select(AlertNotification).where(AlertNotification.user_id == uid).order_by(AlertNotification.created_at.desc()).limit(50)
        )
        notifs = r.scalars().all()
        items = [{"id": n.id, "ts_code": n.ts_code, "stock_name": n.stock_name, "alert_type": n.alert_type, "content": n.content, "is_read": n.is_read, "created_at": n.created_at.isoformat() if n.created_at else ""} for n in notifs]
        return APIResponse(data={"total": len(items), "items": items}, timestamp=int(time.time()))
