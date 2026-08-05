"""异动预警扫描引擎——基于用户配置的条件触发站内通知。

收盘后数据同步完成后运行，确保预警时间戳=数据更新时间。
同一用户+股票+交易日+预警类型幂等，不重复生成。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select, text

from app.core.database import async_session
from app.models.orm.models import AlertNotification, UserAlertConfig

logger = logging.getLogger("alert_engine")

DEFAULT_THRESHOLDS = {
    "pct_chg": 5.0,       # 涨跌幅 ±5%
    "vol_surge": 2.0,     # 成交量 > 5日均量2倍
    "breakout_20": True,  # 突破20日最高/最低
}


class AlertEngine:
    """扫描用户预警配置，匹配当日行情数据，生成站内通知。"""

    async def scan_all(self, trade_date: str = "") -> int:
        if not trade_date:
            async with async_session() as sess:
                r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
                trade_date = r.scalar() or date.today().strftime("%Y%m%d")

        async with async_session() as session:
            r = await session.execute(
                select(UserAlertConfig).where(UserAlertConfig.is_active == 1)
            )
            configs = r.scalars().all()

        if not configs:
            logger.info("No active alert configs, skip scan")
            return 0

        codes = list({c.ts_code for c in configs if c.ts_code})

        # 批量拉取所有关注股票的当日行情
        stocks_data = await self._fetch_stocks_data(codes, trade_date)

        # 批量拉取5日量均和20日高低点
        vol_avg = await self._fetch_vol_avg(codes, trade_date)
        highs_lows = await self._fetch_20d_high_low(codes, trade_date)

        generated = 0
        for cfg in configs:
            stock = stocks_data.get(cfg.ts_code)
            if not stock:
                continue

            alert_types = self._parse_alert_types(cfg.alert_types)
            for atype in alert_types:
                content = self._check_condition(
                    atype, cfg.ts_code, stock, trade_date,
                    vol_avg.get(cfg.ts_code), highs_lows.get(cfg.ts_code),
                )
                if content:
                    ok = await self._save_notification(cfg, atype, content, trade_date)
                    if ok:
                        generated += 1

        logger.info(f"Alert scan complete: {generated} notifications for {trade_date}")
        return generated

    async def _fetch_stocks_data(self, codes: list[str], trade_date: str) -> dict:
        if not codes:
            return {}
        async with async_session() as session:
            placeholders = ",".join([f":c{i}" for i in range(len(codes))])
            params = {f"c{i}": c for i, c in enumerate(codes)}
            params["td"] = trade_date
            r = await session.execute(text(
                f"SELECT d.ts_code, s.name, d.close, d.open, d.pct_chg, d.volume "
                f"FROM stock_daily d JOIN stocks s ON s.ts_code = d.ts_code "
                f"WHERE d.trade_date = :td AND d.ts_code IN ({placeholders})"
            ), params)
            return {
                row[0]: {
                    "name": row[1], "close": float(row[2] or 0),
                    "open": float(row[3] or 0), "pct_chg": float(row[4] or 0),
                    "volume": float(row[5] or 0),
                }
                for row in r
            }

    async def _fetch_vol_avg(self, codes: list[str], trade_date: str) -> dict:
        if not codes:
            return {}
        async with async_session() as session:
            # 取最近5个交易日
            r = await session.execute(text(
                "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= :td "
                "ORDER BY trade_date DESC LIMIT 5"
            ), {"td": trade_date})
            dates = [row[0] for row in r.fetchall()]
            if len(dates) < 2:
                return {}

            placeholders = ",".join([f":c{i}" for i in range(len(codes))])
            params = {f"c{i}": c for i, c in enumerate(codes)}
            params["d0"] = dates[0]
            params["d1"] = dates[1] if len(dates) > 1 else dates[0]
            params["d2"] = dates[2] if len(dates) > 2 else dates[0]
            params["d3"] = dates[3] if len(dates) > 3 else dates[0]
            params["d4"] = dates[4] if len(dates) > 4 else dates[0]

            r = await session.execute(text(
                f"SELECT ts_code, AVG(volume) FROM stock_daily "
                f"WHERE ts_code IN ({placeholders}) AND trade_date IN (:d0,:d1,:d2,:d3,:d4) "
                f"GROUP BY ts_code"
            ), params)
            return {row[0]: float(row[1] or 0) for row in r}

    async def _fetch_20d_high_low(self, codes: list[str], trade_date: str) -> dict:
        if not codes:
            return {}
        async with async_session() as session:
            r = await session.execute(text(
                "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= :td "
                "ORDER BY trade_date DESC LIMIT 20"
            ), {"td": trade_date})
            dates = [row[0] for row in r.fetchall()]
            if not dates:
                return {}

            placeholders = ",".join([f":c{i}" for i in range(len(codes))])
            date_placeholders = ",".join([f":dt{i}" for i in range(len(dates))])
            params = {f"c{i}": c for i, c in enumerate(codes)}
            for i, d in enumerate(dates):
                params[f"dt{i}"] = d

            r = await session.execute(text(
                f"SELECT ts_code, MAX(high), MIN(low) FROM stock_daily "
                f"WHERE ts_code IN ({placeholders}) AND trade_date IN ({date_placeholders}) "
                f"GROUP BY ts_code"
            ), params)
            return {row[0]: {"max_high": float(row[1] or 0), "min_low": float(row[2] or 0)} for row in r}

    def _parse_alert_types(self, raw: str | None) -> list[str]:
        if not raw:
            return ["pct_chg", "vol_surge", "breakout_20"]
        try:
            types = json.loads(raw)
            return types if types else ["pct_chg", "vol_surge", "breakout_20"]
        except (json.JSONDecodeError, TypeError):
            return ["pct_chg", "vol_surge", "breakout_20"]

    def _check_condition(self, atype: str, ts_code: str, stock: dict,
                         trade_date: str, vol_avg: float | None,
                         hl: dict | None) -> str | None:
        close = stock["close"]
        pct = stock["pct_chg"]
        vol = stock["volume"]
        name = stock["name"]

        if atype == "pct_chg":
            if abs(pct) >= DEFAULT_THRESHOLDS["pct_chg"]:
                direction = "大涨" if pct > 0 else "大跌"
                return f"{name}({ts_code}) {direction} {pct:+.2f}%"
        elif atype == "vol_surge":
            if vol_avg and vol_avg > 0 and vol / vol_avg >= DEFAULT_THRESHOLDS["vol_surge"]:
                ratio = vol / vol_avg
                return f"{name}({ts_code}) 成交量异常放大 {ratio:.1f}倍（5日均量{vol_avg:.0f}）"
        elif atype == "breakout_20":
            if hl:
                if close >= hl["max_high"]:
                    return f"{name}({ts_code}) 收盘价 {close:.2f} 突破20日最高价 {hl['max_high']:.2f}"
                if close <= hl["min_low"]:
                    return f"{name}({ts_code}) 收盘价 {close:.2f} 跌破20日最低价 {hl['min_low']:.2f}"
        return None

    async def _save_notification(self, cfg, atype: str, content: str,
                                 trade_date: str) -> bool:
        async with async_session() as session:
            existing = await session.execute(
                select(AlertNotification).where(
                    AlertNotification.user_id == cfg.user_id,
                    AlertNotification.ts_code == cfg.ts_code,
                    AlertNotification.alert_type == atype,
                    AlertNotification.created_at >= f"{trade_date} 00:00:00",
                )
            )
            if existing.scalar_one_or_none():
                return False

            session.add(AlertNotification(
                user_id=cfg.user_id,
                ts_code=cfg.ts_code,
                stock_name=self._get_stock_name(cfg.ts_code),
                alert_type=atype,
                content=content,
                is_read=False,
            ))
            await session.commit()
            return True

    def _get_stock_name(self, ts_code: str) -> str:
        return ts_code
