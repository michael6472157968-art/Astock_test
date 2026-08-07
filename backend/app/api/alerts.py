"""自选股异动预警 API——支持自选股管理（本地JSON+DB双写）、预警配置与通知。"""

from __future__ import annotations

import math
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.database import async_session
from app.core.settings import get_settings
from app.middleware.auth_middleware import require_auth
from app.models.orm.models import UserFavorite, UserFavoriteGroup
from app.models.schemas.common import APIResponse
from app.services import user_data
from sqlalchemy import text as _text

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
            signals.append("站上MA5")
            score += 2
    else:
        score -= 8
        if ma5 < ma10 < 0:
            signals.append("空头排列")
            score -= 5
        else:
            signals.append("跌破MA5")
            score -= 2

    # 涨跌趋势
    if n >= 5:
        chg5 = (closes[-1] / closes[-5] - 1) * 100 if closes[-5] else 0
        if chg5 > 5:
            signals.append(f"5日+{chg5:.1f}%")
            score += 5
        elif chg5 > 3:
            signals.append(f"5日偏强+{chg5:.1f}%")
            score += 2
        elif chg5 < -5:
            signals.append(f"5日{chg5:.1f}%")
            score -= 5
        elif chg5 < -3:
            signals.append(f"5日偏弱{chg5:.1f}%")
            score -= 2

    if n >= 10:
        chg10 = (closes[-1] / closes[-10] - 1) * 100 if closes[-10] else 0
        if chg10 > 10:
            signals.append(f"10日+{chg10:.1f}%")
            score += 4
        elif chg10 > 7:
            signals.append(f"10日偏强+{chg10:.1f}%")
            score += 2
        elif chg10 < -10:
            signals.append(f"10日{chg10:.1f}%")
            score -= 4
        elif chg10 < -7:
            signals.append(f"10日偏弱{chg10:.1f}%")
            score -= 2

    # 量价关系
    if n >= 5:
        vol_up = closes[-1] > closes[-5]
        price_up = closes[-1] > closes[-2] if n >= 2 else False
        if vol_up and price_up:
            signals.append("量价齐升")
            score += 3
        elif not vol_up and not price_up:
            signals.append("缩量回调")
            score -= 2
        elif vol_up and not price_up:
            signals.append("放量滞涨")
            score -= 1
        elif not vol_up and price_up:
            signals.append("缩量反弹")
            score += 1

    # RSI 快速估算
    rsi = _quick_rsi(closes[-8:]) if n >= 8 else 50
    if rsi > 75:
        signals.append(f"RSI超买{rsi:.0f}")
        score -= 5
    elif rsi > 65:
        signals.append("RSI偏强")
        score -= 2
    elif rsi < 25:
        signals.append(f"RSI超卖{rsi:.0f}")
        score += 8
    elif rsi < 35:
        signals.append("RSI偏弱")
        score += 3
    elif rsi > 60:
        score += 3
    elif rsi < 40:
        score -= 3

    # 布林带位置
    boll_pos = _bollinger_position(closes)
    if boll_pos is not None:
        if boll_pos > 0.85:
            signals.append("布林上轨")
            score -= 4
        elif boll_pos > 0.7:
            signals.append("布林偏上轨")
            score -= 1
        elif boll_pos < 0.15:
            signals.append("布林下轨")
            score += 6
        elif boll_pos < 0.3:
            signals.append("布林偏下轨")
            score += 2

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

    return {"score": score, "signals": signals[:6], "risk": risk}


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
    stock_name: str = ""


@router.post("/favorites")
async def add_favorite(req: AddFavRequest, user: dict = Depends(require_auth)):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    stock_name = req.stock_name.strip() if req.stock_name else req.stock_code
    ok, msg = await user_data.add_favorite(uid, req.stock_code, stock_name, tier)
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
    if tier != 99 and tier < 1:
        raise HTTPException(403, "注册用户及以上才可创建分组，请先注册或登录")

    ok, msg, gid = await user_data.create_group(uid, req.name, tier)
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
    if tier != 99:
        raise HTTPException(403, "仅管理员可删除分组")

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
    if tier != 99 and tier < 1 and req.group_id is not None:
        raise HTTPException(403, "注册用户及以上才可使用分组功能，请先注册或登录")

    ok, msg = await user_data.move_to_group(uid, stock_code, req.group_id, tier)
    if not ok:
        raise HTTPException(400, msg)
    return APIResponse(data={"message": msg}, timestamp=int(time.time()))


@router.get("/favorites/groups/stats")
async def favorites_group_stats(
    user: dict = Depends(require_auth),
):
    uid, tier = user["user_id"], user["tier"]
    if not uid:
        raise HTTPException(401, "请先登录")

    stocks = await user_data.get_favorites(uid)
    all_codes = [s["stock_code"] for s in stocks]
    if not all_codes:
        return APIResponse(data={"groups": [], "ungrouped": None, "dates": []}, timestamp=int(time.time()))

    async with async_session() as session:
        # 取最近22个交易日（覆盖一个月窗口）
        r = await session.execute(
            _text("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 22")
        )
        dates = [row[0] for row in r.fetchall()]
        dates.reverse()

        if len(dates) < 1:
            return APIResponse(data={"groups": [], "ungrouped": None, "dates": []}, timestamp=int(time.time()))

        # 批量取所有自选股在这些交易日的 pct_chg
        code_placeholders = ",".join([f":c{i}" for i in range(len(all_codes))])
        date_placeholders = ",".join([f":d{i}" for i in range(len(dates))])
        params = {f"c{i}": code for i, code in enumerate(all_codes)}
        params.update({f"d{i}": d for i, d in enumerate(dates)})
        r2 = await session.execute(
            _text(f"""SELECT ts_code, trade_date, pct_chg FROM stock_daily
                     WHERE ts_code IN ({code_placeholders}) AND trade_date IN ({date_placeholders})"""),
            params
        )
        rows = r2.fetchall()
        code_pct: dict[str, dict[str, float]] = {}
        for ts_code, td, pct in rows:
            code_pct.setdefault(ts_code, {})[td] = round(float(pct), 2) if pct is not None else 0.0

    # 按分组聚合逐日均值
    def _daily_avg(codes_in_group: list[str]) -> list[dict]:
        result = []
        for td in dates:
            vals = []
            for c in codes_in_group:
                v = code_pct.get(c, {}).get(td)
                if v is not None:
                    vals.append(v)
            avg = round(sum(vals) / len(vals), 2) if vals else None
            result.append({"date": td, "avg_chg": avg})
        return result

    group_stocks_map: dict[int, list[str]] = {}
    ungrouped_codes: list[str] = []
    for s in stocks:
        gid = s.get("group_id")
        if gid is not None:
            group_stocks_map.setdefault(gid, []).append(s["stock_code"])
        else:
            ungrouped_codes.append(s["stock_code"])

    groups_out = []
    gs_list, _ = await user_data.get_groups(uid)
    for g in gs_list:
        codes = group_stocks_map.get(g["id"], [])
        daily = _daily_avg(codes) if codes else [{"date": td, "avg_chg": None} for td in dates]
        groups_out.append({
            "id": g["id"],
            "name": g["name"],
            "stock_count": len(codes),
            "daily_chg": daily,
        })

    ungrouped = None
    if ungrouped_codes:
        ungrouped = {
            "stock_count": len(ungrouped_codes),
            "daily_chg": _daily_avg(ungrouped_codes),
        }

    return APIResponse(data={"groups": groups_out, "ungrouped": ungrouped, "dates": dates}, timestamp=int(time.time()))


@router.get("/favorites/stock-prices")
async def favorites_stock_prices(
    codes: str = "",
    t_date: str = "",
    user: dict = Depends(require_auth),
):
    """个股涨跌幅走势 — 近22交易日完整窗口，最多5只股票。t_date由前端按个股独立裁剪。"""
    uid = user["user_id"]
    if not uid:
        raise HTTPException(401, "请先登录")

    code_list = [c.strip() for c in codes.split(",") if c.strip()][:5] if codes else []
    if not code_list:
        return APIResponse(data={"stocks": [], "dates": []}, timestamp=int(time.time()))

    async with async_session() as session:
        # 始终返回近22个交易日完整窗口
        r = await session.execute(
            _text("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 22")
        )
        dates = [row[0] for row in r.fetchall()]
        dates.reverse()

        if not dates:
            return APIResponse(data={"stocks": [], "dates": []}, timestamp=int(time.time()))

        # 批量查 close + pct_chg（全窗口数据，前端按个股T日裁剪）
        code_phs = ",".join([f":c{i}" for i in range(len(code_list))])
        date_phs = ",".join([f":d{i}" for i in range(len(dates))])
        params = {f"c{i}": code for i, code in enumerate(code_list)}
        params.update({f"d{i}": d for i, d in enumerate(dates)})
        r2 = await session.execute(
            _text(f"""SELECT ts_code, trade_date, close, pct_chg FROM stock_daily
                     WHERE ts_code IN ({code_phs}) AND trade_date IN ({date_phs})"""),
            params
        )
        rows = r2.fetchall()
        code_pct: dict[str, dict[str, float]] = {}
        for ts_code, td, close, pct in rows:
            code_pct.setdefault(ts_code, {})[td] = round(float(pct), 2) if pct is not None else None

    stocks_out = []
    async with async_session() as session:
        for code in code_list:
            r = await session.execute(
                _text("SELECT name FROM stocks WHERE ts_code = :code LIMIT 1"),
                {"code": code}
            )
            row = r.fetchone()
            name = row[0] if row else code
            daily_list = [
                {
                    "date": td,
                    "pct_chg": code_pct.get(code, {}).get(td, None),
                }
                for td in dates
            ]
            stocks_out.append({"ts_code": code, "name": name, "daily": daily_list})

    return APIResponse(data={"stocks": stocks_out, "dates": dates}, timestamp=int(time.time()))
