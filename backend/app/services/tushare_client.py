"""Tushare SDK 代理——统一入口，内置频率限流，缓存优先。

所有 Tushare 访问必须通过此模块。业务代码禁止直接 import tushare。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any

import tushare as ts

from app.core.settings import get_settings

logger = logging.getLogger("tushare")

_settings = get_settings()
_pro: Any = None

# 进程内每分钟计数器
_minute_counters: dict[str, int] = {"general": 0, "financial": 0}
_minute_start: float = time.time()


def _reset_minute_if_needed() -> None:
    global _minute_counters, _minute_start
    if time.time() - _minute_start >= 60:
        _minute_counters = {"general": 0, "financial": 0}
        _minute_start = time.time()


class TushareQuotaError(Exception):
    """每日额度用尽时抛出。"""


class TushareServiceError(Exception):
    """Tushare 调用失败时抛出。"""


async def _track_call(call_type: str = "general") -> None:
    """递增分钟计数器，超额时均匀节流（sleep而非抛异常）。"""
    from app.core.cache import cache_get

    # 每日额度检查
    today = date.today().isoformat()
    daily_key = f"tushare_daily:{today}"
    daily = await cache_get(daily_key) or 0
    daily += 1

    limit = _settings.tushare_daily_credit_limit
    if daily > limit * 0.9:
        logger.warning(f"Tushare 日调用量 {daily}/{limit}")
    if daily >= limit:
        raise TushareQuotaError(f"Tushare 日额度已用尽: {daily}/{limit}")

    from app.core.cache import cache_set
    await cache_set(daily_key, daily, ttl=86400)

    # 每分钟限流
    _reset_minute_if_needed()
    cap = _settings.tushare_general_rate if call_type == "general" else _settings.tushare_financial_rate
    _minute_counters[call_type] += 1
    if _minute_counters[call_type] > cap:
        await asyncio.sleep(60 / cap)


def get_pro() -> Any:
    global _pro
    if _pro is None:
        if not _settings.tushare_token:
            raise TushareServiceError("Tushare Token 未配置，请在 backend/.env 中填入 TUSHARE_TOKEN")
        _pro = ts.pro_api(_settings.tushare_token)
        logger.info(f"Tushare client initialized (token: ...{_settings.tushare_token[-8:]})")
    return _pro


async def call_tushare(func_name: str, call_type: str = "general", **kwargs) -> Any:
    """统一 Tushare 调用入口。

    Args:
        func_name: Tushare函数名，如 'daily', 'stock_basic'
        call_type: 'general'(180/min) 或 'financial'(70/min)
        **kwargs: 传给Tushare函数的参数

    Returns:
        DataFrame 或 None
    """
    await _track_call(call_type)

    try:
        pro = get_pro()
        func = getattr(pro, func_name)
        result = func(**kwargs)
        if result is None or (hasattr(result, 'empty') and result.empty):
            logger.warning(f"Tushare {func_name}({kwargs}) 返回空")
            return None
        return result
    except (TushareQuotaError, TushareServiceError):
        raise
    except Exception as e:
        logger.error(f"Tushare {func_name}({kwargs}) 失败: {e}")
        raise TushareServiceError(f"数据服务暂不可用: {e}")


# ── 高级缓存包装器 ──

async def get_stock_basic() -> list[dict]:
    """全市场股票列表，永久缓存。"""
    from app.core.cache import cache_get, cache_set

    cached = await cache_get("stock:basic:all")
    if cached:
        return cached

    result = await call_tushare("stock_basic", fields="ts_code,symbol,name,industry,area,market,list_date")
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    records = result.to_dict(orient="records")
    await cache_set("stock:basic:all", records)
    logger.info(f"Stock basic: {len(records)} stocks cached permanently")
    return records


async def get_daily_data(ts_code: str, start_date: str, end_date: str, call_type: str = "general") -> list[dict]:
    """个股日线行情，Tushare直调（不缓存，由上层服务缓存）。"""
    result = await call_tushare("daily", call_type=call_type, ts_code=ts_code,
                                 start_date=start_date, end_date=end_date)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_all_daily(trade_date: str) -> list[dict]:
    """全市场日线，单日期批量查询。"""
    result = await call_tushare("daily", trade_date=trade_date)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_income(ts_code: str, end_date: str | None = None) -> list[dict]:
    """利润表——使用财务专用额度(70/min)。"""
    params = {"ts_code": ts_code}
    if end_date:
        params["end_date"] = end_date
    result = await call_tushare("income", call_type="financial", **params)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_balancesheet(ts_code: str, end_date: str | None = None) -> list[dict]:
    """资产负债表——财务专用额度。"""
    params = {"ts_code": ts_code}
    if end_date:
        params["end_date"] = end_date
    result = await call_tushare("balancesheet", call_type="financial", **params)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_cashflow(ts_code: str, end_date: str | None = None) -> list[dict]:
    """现金流量表——财务专用额度。"""
    params = {"ts_code": ts_code}
    if end_date:
        params["end_date"] = end_date
    result = await call_tushare("cashflow", call_type="financial", **params)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_index_daily(ts_code: str, start_date: str, end_date: str) -> list[dict]:
    """大盘指数日线。"""
    result = await call_tushare("index_daily", ts_code=ts_code, start_date=start_date, end_date=end_date)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_limit_list(trade_date: str) -> list[dict]:
    """涨跌停列表。"""
    result = await call_tushare("limit_list", trade_date=trade_date)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_sector_list() -> list[dict]:
    """申万行业分类列表。"""
    result = await call_tushare("index_classify", src="SW2021")
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_moneyflow_hsgt(start_date: str, end_date: str) -> list[dict]:
    """沪深港通资金流向——北向/南向净流入。2000积分解锁。"""
    result = await call_tushare("moneyflow_hsgt", start_date=start_date, end_date=end_date)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_daily_basic(trade_date: str) -> list[dict]:
    """每日指标——PE/PB/换手率/总市值等。"""
    result = await call_tushare("daily_basic", trade_date=trade_date)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")


async def get_moneyflow(ts_code: str, start_date: str, end_date: str) -> list[dict]:
    """个股资金流向——主力/超大单/大单/中单/小单净流入。2000积分解锁。"""
    result = await call_tushare("moneyflow", ts_code=ts_code, start_date=start_date, end_date=end_date)
    if result is None or (hasattr(result, 'empty') and result.empty):
        return []
    return result.to_dict(orient="records")
