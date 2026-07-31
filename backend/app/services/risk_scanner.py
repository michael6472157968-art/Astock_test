"""异动预警扫描引擎——遍历用户自选股，生成技术面异动通知。"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from app.core.database import async_session
from app.core.settings import get_settings
from app.models.orm.models import AlertNotification
from app.services import user_data

logger = logging.getLogger("risk")
_settings = get_settings()


class RiskScanner:

    def _list_favorite_stocks(self) -> dict[int, list[dict]]:
        """遍历所有用户的自选股，返回 {user_id: [{stock_code, stock_name}, ...]}"""
        user_data_dir = _settings.user_data_dir
        if not os.path.isdir(user_data_dir):
            return {}

        result: dict[int, list[dict]] = {}
        for entry in os.scandir(user_data_dir):
            if not entry.is_dir():
                continue
            try:
                uid = int(entry.name)
            except ValueError:
                continue
            stocks = user_data.get_favorites(uid)
            if stocks:
                result[uid] = stocks
        return result

    async def scan_all(self, trade_date: str = "") -> list[dict]:
        """扫描所有用户自选股，检测技术面异动并写入通知表。"""
        if not trade_date:
            trade_date = (date.today() - timedelta(days=1)).strftime("%Y%m%d")

        user_stocks = self._list_favorite_stocks()
        if not user_stocks:
            logger.info("No user favorite stocks to scan")
            return []

        all_codes = set()
        for stocks in user_stocks.values():
            for s in stocks:
                all_codes.add(s["stock_code"])

        if not all_codes:
            return []

        async with async_session() as session:
            from sqlalchemy import text as _text
            alerts = await self._detect_alerts(session, list(all_codes), _text, trade_date)
            await self._save_notifications(session, alerts, user_stocks)
            await session.commit()

        logger.info(f"Alert scan complete: {len(alerts)} alerts for {len(user_stocks)} users")
        return alerts

    async def _detect_alerts(self, session, codes: list[str], _text, trade_date: str) -> list[dict]:
        """对给定股票代码列表检测技术面异动信号。"""
        alerts: list[dict] = []

        # 最新两日日线数据（用于计算变化）
        placeholders = ",".join([f":c{i}" for i in range(len(codes))])
        params = {f"c{i}": c for i, c in enumerate(codes)}

        # RSI 超买超卖检测
        rsi_sql = f"""
            SELECT d.ts_code, s.name, d.rsi_14, d.pct_chg, d.close
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.ts_code IN ({placeholders})
            AND d.trade_date <= :trade_date
            ORDER BY d.trade_date DESC
        """
        query_params = {**params, "trade_date": trade_date}

        # 按股票收集最新一条日线记录
        from collections import defaultdict
        latest = {}
        r = await session.execute(_text(rsi_sql), query_params)
        for row in r.fetchall():
            code = row[0]
            if code not in latest:
                latest[code] = {
                    "ts_code": code,
                    "name": row[1],
                    "rsi_14": row[2],
                    "pct_chg": row[3],
                    "close": float(row[4]) if row[4] else None,
                }

        for code, d in latest.items():
            rsi = d["rsi_14"]
            if rsi is not None:
                rsi = float(rsi)
                if rsi >= 80:
                    alerts.append({"ts_code": code, "stock_name": d["name"], "alert_type": "RSI超买",
                                   "content": f"{d['name']}({code}) RSI={rsi:.1f}，进入超买区间，注意回调风险"})
                elif rsi <= 20:
                    alerts.append({"ts_code": code, "stock_name": d["name"], "alert_type": "RSI超卖",
                                   "content": f"{d['name']}({code}) RSI={rsi:.1f}，进入超卖区间，可能存在反弹机会"})

            pct = d["pct_chg"]
            if pct is not None:
                pct = float(pct)
                if pct >= 9.5:
                    alerts.append({"ts_code": code, "stock_name": d["name"], "alert_type": "涨幅异常",
                                   "content": f"{d['name']}({code}) 单日涨幅 {pct:+.2f}%，注意追高风险"})
                elif pct <= -9.5:
                    alerts.append({"ts_code": code, "stock_name": d["name"], "alert_type": "跌幅异常",
                                   "content": f"{d['name']}({code}) 单日跌幅 {pct:+.2f}%，注意止损或抄底机会"})

        # MACD 金叉/死叉检测（比较最近两日）
        macd_sql = f"""
            SELECT d.ts_code, s.name, d.macd_dif, d.macd_dea, d.macd_bar
            FROM stock_daily d
            JOIN stocks s ON s.ts_code = d.ts_code
            WHERE d.ts_code IN ({placeholders})
            ORDER BY d.ts_code, d.trade_date DESC
        """
        r = await session.execute(_text(macd_sql), query_params)
        rows_by_code: dict[str, list] = {}
        for row in r.fetchall():
            rows_by_code.setdefault(row[0], []).append(row)

        for code, rows in rows_by_code.items():
            if len(rows) < 2:
                continue
            today_row, yesterday_row = rows[0], rows[1]
            name = today_row[1]
            today_dif, today_dea = (float(v) if v is not None else None for v in (today_row[2], today_row[3]))
            yesterday_dif, yesterday_dea = (float(v) if v is not None else None for v in (yesterday_row[2], yesterday_row[3]))
            if None in (today_dif, today_dea, yesterday_dif, yesterday_dea):
                continue

            # 金叉：DIF 上穿 DEA
            if yesterday_dif <= yesterday_dea and today_dif > today_dea:
                alerts.append({"ts_code": code, "stock_name": name, "alert_type": "MACD金叉",
                               "content": f"{name}({code}) MACD金叉：DIF({today_dif:.3f})上穿DEA({today_dea:.3f})，短期看多信号"})
            # 死叉：DIF 下穿 DEA
            elif yesterday_dif >= yesterday_dea and today_dif < today_dea:
                alerts.append({"ts_code": code, "stock_name": name, "alert_type": "MACD死叉",
                               "content": f"{name}({code}) MACD死叉：DIF({today_dif:.3f})下穿DEA({today_dea:.3f})，短期看空信号"})

        # 放量检测（成交量 > MA20 均量的 2 倍）
        vol_sql = f"""
            WITH ranked AS (
                SELECT d.ts_code, s.name, d.vol, d.close, d.trade_date,
                       AVG(d.vol) OVER (PARTITION BY d.ts_code ORDER BY d.trade_date ROWS BETWEEN 19 PRECEDING AND 1 PRECEDING) AS vol_ma20
                FROM stock_daily d
                JOIN stocks s ON s.ts_code = d.ts_code
                WHERE d.ts_code IN ({placeholders})
                AND d.trade_date <= :trade_date
            )
            SELECT ts_code, name, vol, vol_ma20, close, trade_date FROM ranked
            WHERE trade_date = (SELECT MAX(trade_date) FROM ranked r2 WHERE r2.ts_code = ranked.ts_code)
        """
        r = await session.execute(_text(vol_sql), query_params)
        for row in r.fetchall():
            code, name, vol, vol_ma20, close, td = row
            if vol is not None and vol_ma20 is not None and float(vol_ma20) > 0:
                ratio = float(vol) / float(vol_ma20)
                if ratio >= 2.0:
                    direction = "放量上涨" if float(close or 0) > 0 else "放量异动"
                    alerts.append({"ts_code": code, "stock_name": name, "alert_type": "放量异动",
                                   "content": f"{name}({code}) 成交量突增 {ratio:.1f}倍（量比{ratio:.1f}），{direction}，关注后续走势"})

        return alerts

    async def _save_notifications(self, session, alerts: list[dict], user_stocks: dict[int, list[dict]]):
        """将异动通知按用户自选股归属写入 alert_notifications 表。"""
        # 构建 code -> alert 映射，去重
        code_alerts: dict[str, list[dict]] = {}
        for a in alerts:
            code_alerts.setdefault(a["ts_code"], []).append(a)

        now = datetime.now()
        for uid, stocks in user_stocks.items():
            user_codes = {s["stock_code"] for s in stocks}
            for code in user_codes:
                for alert in code_alerts.get(code, []):
                    notif = AlertNotification(
                        user_id=uid,
                        ts_code=alert["ts_code"],
                        stock_name=alert["stock_name"],
                        alert_type=alert["alert_type"],
                        content=alert["content"],
                        is_read=0,
                        created_at=now,
                    )
                    session.add(notif)
