"""选股池 API——4大选股池列表与详情。"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.cache import cache_get as _cache_get, cache_set as _cache_set
from app.core.settings import get_settings
from app.middleware.auth_middleware import require_auth_optional
from app.models.schemas.common import APIResponse
from app.utils.trading_calendar import get_latest_trade_date, is_trade_date

router = APIRouter(prefix="/api/v1/stock-pool", tags=["选股池"])
_settings = get_settings()

POOL_TYPES = {
    "hot_leader": {"name": "热点龙头池", "desc": "当日热点板块内放量突破的强势股"},
    "dip_ambush": {"name": "低吸埋伏池", "desc": "回调到关键支撑位、缩量企稳的优质股"},
    "oversold_rebound": {"name": "超跌反弹池", "desc": "短期超跌、出现反转信号的博弈股"},
    "steady_swing": {"name": "稳健波段池", "desc": "趋势向上、量价健康的中短线标的"},
}


async def _resolve_trade_date() -> tuple[str, bool]:
    """返回 (trade_date, is_trade_day) — DB MAX 优先。"""
    from sqlalchemy import text as _text
    from app.core.database import async_session as _sess

    today_str = date.today().strftime("%Y%m%d")
    trade_date = today_str

    try:
        async with _sess() as s:
            r = await s.execute(_text("SELECT MAX(trade_date) FROM stock_daily"))
            max_td = r.scalar()
            if max_td:
                trade_date = max_td
    except Exception:
        pass

    if trade_date == today_str:
        try:
            trade_date = await get_latest_trade_date()
        except Exception:
            pass

    try:
        is_td_val = await is_trade_date(today_str)
    except Exception:
        is_td_val = date.today().weekday() < 5
    return trade_date, is_td_val


@router.get("/categories")
async def list_categories(user: dict = Depends(require_auth_optional)):
    return APIResponse(
        data={"categories": [{"type": k, **v} for k, v in POOL_TYPES.items()]},
        timestamp=int(time.time()),
    )


@router.get("/{pool_type}")
async def list_pool(pool_type: str, page: int = 1, page_size: int = 20,
                    exclude_300: bool = False, exclude_301: bool = False,
                    exclude_688: bool = False, exclude_920: bool = False,
                    user: dict = Depends(require_auth_optional)):
    if pool_type not in POOL_TYPES:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"无效的选股池类型: {pool_type}")

    from sqlalchemy import text as _text
    from app.core.database import async_session as _sess

    trade_date, is_td = await _resolve_trade_date()

    from app.core.cache import cache_get as _cg
    cached = await _cg(f"pool:{pool_type}:{trade_date}")
    items = cached if cached else []

    # 缓存 miss 时从 DB 降级读取
    if not items:
        async with _sess() as s:
            r = await s.execute(_text(
                "SELECT ts_code, stock_name, market_data_json, inclusion_reason "
                "FROM stock_pool_results "
                "WHERE calc_date = :cd AND pool_type = :pt "
                "ORDER BY rank_in_pool ASC"
            ), {"cd": trade_date, "pt": pool_type})
            for row in r:
                try:
                    md = json.loads(row[2]) if row[2] else {}
                except Exception:
                    md = {}
                items.append({
                    "stock_code": row[0],
                    "stock_name": row[1],
                    "close": md.get("close"),
                    "change_pct": md.get("change_pct"),
                    "volume_ratio": md.get("volume_ratio"),
                    "inclusion_reason": row[3],
                })
            if items:
                await _cache_set(f"pool:{pool_type}:{trade_date}", items, ttl=_settings.cache_offline_ttl)

    # 按用户偏好过滤交易权限板块
    exclude_prefixes = set()
    if exclude_300: exclude_prefixes.add("300")
    if exclude_301: exclude_prefixes.add("301")
    if exclude_688: exclude_prefixes.add("688")
    if exclude_920: exclude_prefixes.add("920")

    if exclude_prefixes:
        items = [i for i in items if not any(
            (i.get("stock_code","") or "").startswith(p) for p in exclude_prefixes
        )]

    seen = set()
    deduped = []
    for item in items:
        code = item.get("stock_code", "")
        if code and code not in seen:
            seen.add(code)
            deduped.append(item)
    items = deduped

    total = len(items)

    return APIResponse(
        data={
            "pool_type": pool_type, "date": trade_date,
            "trade_date": trade_date, "is_trade_day": is_td,
            "total": total, "items": items,
        },
        timestamp=int(time.time()),
        ext_info={"cache_hit": cached is not None},
    )

