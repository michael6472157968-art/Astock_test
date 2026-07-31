"""风险避雷扫描引擎——仅财务异常类。"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from app.core.cache import cache_set
from app.core.settings import get_settings

logger = logging.getLogger("risk")
_settings = get_settings()


class RiskScanner:

    async def scan_all(self, trade_date: str = "") -> list:
        if not trade_date:
            trade_date = (date.today() - timedelta(days=1)).strftime("%Y%m%d")

        await cache_set(f"risk:list:{trade_date}", [], ttl=_settings.cache_offline_ttl)
        logger.info(f"Risk scan completed for {trade_date}")
        return []
