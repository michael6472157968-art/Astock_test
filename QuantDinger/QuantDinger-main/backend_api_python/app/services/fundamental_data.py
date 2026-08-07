"""Point-in-time fundamental observations and panel enrichment."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Mapping

import pandas as pd

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

FUNDAMENTAL_FIELDS = (
    "revenue",
    "net_income",
    "book_value",
    "shareholder_equity",
    "total_debt",
    "free_cash_flow",
    "shares_outstanding",
    "market_cap",
    "pe_ratio",
    "pb_ratio",
    "return_on_equity",
    "revenue_growth",
    "debt_to_equity",
)


class FundamentalDataService:
    """Load only observations that were public at each simulated date."""

    @staticmethod
    def ensure_schema() -> None:
        with get_db_connection() as db:
            cur = db.cursor()
            for field in FUNDAMENTAL_FIELDS:
                cur.execute(
                    f"ALTER TABLE qd_fundamental_snapshots ADD COLUMN IF NOT EXISTS {field} DOUBLE PRECISION"
                )
            db.commit()
            cur.close()

    def enrich_panel(
        self,
        frames: Mapping[str, pd.DataFrame],
        members: list[dict],
    ) -> dict[str, pd.DataFrame]:
        self.ensure_schema()
        identities = {}
        for item in members:
            symbol = str(item.get("symbol") or "").upper()
            market = str(item.get("market") or "")
            key = str(item.get("key") or "")
            identities[symbol] = (market, symbol)
            if key:
                identities[key] = (market, symbol)
        output = {}
        for key, frame in frames.items():
            market, symbol = identities.get(key, identities.get(str(key).upper(), ("", str(key))))
            output[key] = self.enrich_frame(
                market=market,
                symbol=symbol,
                frame=frame,
            )
        return output

    def enrich_frame(self, *, market: str, symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty or not market or not symbol:
            return frame
        try:
            rows = self._load_rows(market, symbol, frame.index.max())
        except Exception as exc:
            logger.warning("fundamental point-in-time load failed market=%s symbol=%s: %s", market, symbol, exc)
            return frame
        if not rows:
            return frame
        enriched = frame.copy()
        dates = pd.DatetimeIndex(enriched.index).normalize()
        observations = pd.DataFrame(rows)
        observations["available_at"] = pd.to_datetime(observations["available_at"])
        observations = observations.sort_values(["available_at", "period_end"]).drop_duplicates("available_at", keep="last")
        observations = observations.set_index("available_at")
        for field in FUNDAMENTAL_FIELDS:
            source = observations[field] if field in observations.columns else pd.Series(index=observations.index, dtype=float)
            values = pd.to_numeric(source, errors="coerce")
            enriched[field] = values.reindex(dates, method="ffill").to_numpy()
        derived_market_cap = pd.to_numeric(enriched["close"], errors="coerce") * pd.to_numeric(
            enriched["shares_outstanding"], errors="coerce"
        )
        enriched["market_cap"] = pd.to_numeric(enriched["market_cap"], errors="coerce").fillna(derived_market_cap)
        return enriched

    @staticmethod
    def _load_rows(market: str, symbol: str, end: Any) -> list[dict]:
        FundamentalDataService.ensure_schema()
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT period_end, available_at, {', '.join(FUNDAMENTAL_FIELDS)}
                FROM qd_fundamental_snapshots
                WHERE market = ? AND symbol = ? AND available_at <= ?
                ORDER BY available_at, period_end, ingested_at
                """,
                (market, symbol, pd.Timestamp(end).date()),
            )
            rows = cur.fetchall() or []
            cur.close()
        return rows

    @staticmethod
    def upsert(payload: dict) -> None:
        FundamentalDataService.ensure_schema()
        values = [payload.get(field) for field in FUNDAMENTAL_FIELDS]
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                INSERT INTO qd_fundamental_snapshots
                  (market, symbol, period_end, available_at, frequency, currency,
                   {', '.join(FUNDAMENTAL_FIELDS)}, source, source_version, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, {', '.join(['?'] * len(FUNDAMENTAL_FIELDS))}, ?, ?, ?)
                ON CONFLICT (market, symbol, period_end, available_at, source) DO UPDATE SET
                  {', '.join(f'{field} = EXCLUDED.{field}' for field in FUNDAMENTAL_FIELDS)},
                  frequency = EXCLUDED.frequency,
                  currency = EXCLUDED.currency,
                  source_version = EXCLUDED.source_version,
                  metadata_json = EXCLUDED.metadata_json,
                  ingested_at = NOW()
                """,
                (
                    str(payload.get("market") or ""),
                    str(payload.get("symbol") or "").upper(),
                    payload.get("period_end"),
                    payload.get("available_at"),
                    str(payload.get("frequency") or "quarterly"),
                    str(payload.get("currency") or ""),
                    *values,
                    str(payload.get("source") or "manual"),
                    str(payload.get("source_version") or ""),
                    json.dumps(payload.get("metadata") or {}, ensure_ascii=False),
                ),
            )
            db.commit()
            cur.close()

    def coverage(self) -> dict[str, Any]:
        self.ensure_schema()
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT market, COUNT(*) AS observations, COUNT(DISTINCT symbol) AS symbols,
                       MIN(available_at) AS first_available_at, MAX(available_at) AS last_available_at,
                       MAX(ingested_at) AS last_ingested_at
                FROM qd_fundamental_snapshots
                GROUP BY market
                ORDER BY market
                """
            )
            rows = cur.fetchall() or []
            cur.close()
        return {
            "available": bool(rows),
            "fields": list(FUNDAMENTAL_FIELDS),
            "markets": rows,
        }

    def sync_current(self, *, market: str, symbol: str) -> dict[str, Any]:
        normalized_market = str(market or "").strip()
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_market not in {"USStock", "CNStock", "HKStock"} or not normalized_symbol:
            raise ValueError("factor.fundamentalMarketUnsupported")
        from app.services.market_data_collector import MarketDataCollector

        raw = MarketDataCollector()._get_fundamental(normalized_market, normalized_symbol) or {}
        statements = raw.get("financial_statements") if isinstance(raw.get("financial_statements"), dict) else {}
        income = statements.get("income_statement") if isinstance(statements.get("income_statement"), dict) else {}
        balance = statements.get("balance_sheet") if isinstance(statements.get("balance_sheet"), dict) else {}
        cashflow = statements.get("cash_flow") if isinstance(statements.get("cash_flow"), dict) else {}
        today = date.today()
        period_end = income.get("latest_date") or balance.get("latest_date") or cashflow.get("latest_date") or today
        values = {
            "revenue": raw.get("revenue") or income.get("total_revenue"),
            "net_income": raw.get("net_income") or income.get("net_income"),
            "book_value": raw.get("book_value"),
            "shareholder_equity": raw.get("shareholder_equity") or balance.get("total_equity"),
            "total_debt": raw.get("total_debt") or raw.get("debt") or balance.get("debt"),
            "free_cash_flow": raw.get("free_cash_flow") or cashflow.get("free_cash_flow"),
            "shares_outstanding": raw.get("shares_outstanding"),
            "market_cap": raw.get("market_cap"),
            "pe_ratio": raw.get("pe_ratio"),
            "pb_ratio": raw.get("pb_ratio"),
            "return_on_equity": raw.get("return_on_equity") or raw.get("roe"),
            "revenue_growth": raw.get("revenue_growth"),
            "debt_to_equity": raw.get("debt_to_equity"),
        }
        usable = {key: _finite_or_none(value) for key, value in values.items()}
        if not any(value is not None for value in usable.values()):
            raise ValueError("factor.fundamentalDataUnavailable")
        payload = {
            "market": normalized_market,
            "symbol": normalized_symbol,
            "period_end": pd.Timestamp(period_end).date(),
            "available_at": today,
            "frequency": "snapshot",
            "source": str(raw.get("source") or "market_data_collector"),
            "source_version": today.isoformat(),
            "metadata": {"pointInTime": True, "collectedAt": pd.Timestamp.now(tz="UTC").isoformat()},
            **usable,
        }
        self.upsert(payload)
        return payload

    def sync_history(self, *, market: str, symbol: str) -> dict[str, Any]:
        normalized_market = str(market or "").strip()
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_market != "USStock" or not normalized_symbol:
            raise ValueError("factor.fundamentalHistoryMarketUnsupported")

        import yfinance as yf

        ticker = yf.Ticker(normalized_symbol)
        income = ticker.quarterly_income_stmt
        balance = ticker.quarterly_balance_sheet
        cashflow = ticker.quarterly_cash_flow
        periods = sorted(
            {
                pd.Timestamp(column).tz_localize(None).normalize()
                for frame in (income, balance, cashflow)
                if frame is not None and not frame.empty
                for column in frame.columns
            }
        )
        if not periods:
            raise ValueError("factor.fundamentalDataUnavailable")

        earnings_dates = _earnings_dates(ticker)
        prices = ticker.history(
            start=(periods[0] + pd.Timedelta(days=1)).date().isoformat(),
            end=(date.today() + timedelta(days=1)).isoformat(),
            auto_adjust=False,
        )
        stored = 0
        for index, period in enumerate(periods):
            available_at, availability_source = _availability_date(period, earnings_dates)
            revenue = _statement_value(income, period, "Total Revenue", "Revenue")
            net_income = _statement_value(
                income,
                period,
                "Net Income",
                "Net Income Common Stockholders",
            )
            equity = _statement_value(balance, period, "Stockholders Equity", "Total Equity Gross Minority Interest")
            debt = _statement_value(balance, period, "Total Debt")
            shares = _statement_value(
                balance,
                period,
                "Ordinary Shares Number",
                "Share Issued",
            ) or _statement_value(income, period, "Diluted Average Shares", "Basic Average Shares")
            free_cash_flow = _statement_value(cashflow, period, "Free Cash Flow")
            previous_revenue = None
            if index >= 4:
                previous_revenue = _statement_value(income, periods[index - 4], "Total Revenue", "Revenue")
            close = _close_as_of(prices, available_at)
            market_cap = close * shares if close is not None and shares is not None else None
            roe = (net_income * 4.0 / equity) if net_income is not None and equity not in (None, 0.0) else None
            revenue_growth = (
                revenue / previous_revenue - 1.0
                if revenue is not None and previous_revenue not in (None, 0.0)
                else None
            )
            payload = {
                "market": normalized_market,
                "symbol": normalized_symbol,
                "period_end": period.date(),
                "available_at": available_at,
                "frequency": "quarterly",
                "revenue": revenue,
                "net_income": net_income,
                "book_value": equity / shares if equity is not None and shares not in (None, 0.0) else None,
                "shareholder_equity": equity,
                "total_debt": debt,
                "free_cash_flow": free_cash_flow,
                "shares_outstanding": shares,
                "market_cap": market_cap,
                "return_on_equity": roe,
                "revenue_growth": revenue_growth,
                "debt_to_equity": debt / equity if debt is not None and equity not in (None, 0.0) else None,
                "source": "yfinance_quarterly",
                "source_version": date.today().isoformat(),
                "metadata": {
                    "pointInTime": True,
                    "availabilitySource": availability_source,
                    "marketCapMethod": "close_on_or_before_available_at_x_reported_shares",
                },
            }
            if any(_finite_or_none(payload.get(field)) is not None for field in FUNDAMENTAL_FIELDS):
                self.upsert(payload)
                stored += 1
        if not stored:
            raise ValueError("factor.fundamentalDataUnavailable")
        return {
            "market": normalized_market,
            "symbol": normalized_symbol,
            "observations": stored,
            "firstAvailableAt": _availability_date(periods[0], earnings_dates)[0].isoformat(),
            "lastAvailableAt": _availability_date(periods[-1], earnings_dates)[0].isoformat(),
        }


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _statement_value(frame: pd.DataFrame, period: pd.Timestamp, *names: str) -> float | None:
    if frame is None or frame.empty:
        return None
    column = next(
        (
            item for item in frame.columns
            if pd.Timestamp(item).tz_localize(None).normalize() == period
        ),
        None,
    )
    if column is None:
        return None
    for name in names:
        if name in frame.index:
            value = _finite_or_none(frame.loc[name, column])
            if value is not None:
                return value
    return None


def _earnings_dates(ticker: Any) -> list[date]:
    try:
        values = ticker.get_earnings_dates(limit=32)
    except Exception:
        return []
    if values is None or values.empty:
        return []
    return sorted({pd.Timestamp(item).date() for item in values.index})


def _availability_date(period: pd.Timestamp, earnings_dates: list[date]) -> tuple[date, str]:
    period_date = period.date()
    candidates = [item for item in earnings_dates if period_date < item <= period_date + timedelta(days=120)]
    if candidates:
        return candidates[0], "reported_earnings_date"
    return period_date + timedelta(days=60), "conservative_60_day_lag"


def _close_as_of(prices: pd.DataFrame, available_at: date) -> float | None:
    if prices is None or prices.empty or "Close" not in prices.columns:
        return None
    index = pd.DatetimeIndex(prices.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    visible = prices.copy()
    visible.index = index
    visible = visible.loc[visible.index.normalize() <= pd.Timestamp(available_at)]
    if visible.empty:
        return None
    return _finite_or_none(visible["Close"].iloc[-1])


_service: FundamentalDataService | None = None


def get_fundamental_data_service() -> FundamentalDataService:
    global _service
    if _service is None:
        _service = FundamentalDataService()
    return _service
