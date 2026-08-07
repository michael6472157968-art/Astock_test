"""Point-in-time multi-asset data portal for Strategy API V2."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Callable

import pandas as pd

from .instruments import parse_instrument


class StrategyDataError(ValueError):
    pass


class MultiAssetDataPortal:
    REQUIRED_COLUMNS = ("open", "high", "low", "close")

    def __init__(
        self,
        frames: Mapping[str, pd.DataFrame],
        *,
        universe_resolver: Callable[[str, pd.Timestamp], Iterable[str]] | None = None,
    ) -> None:
        self.frames: dict[str, pd.DataFrame] = {}
        self.aliases: dict[str, str] = {}
        self.current_dt: pd.Timestamp | None = None
        self._visible_dt: pd.Timestamp | None = None
        # A grid can inspect dozens of resting orders for the same instrument
        # on every bar.  Keep the current timestamp's normalized OHLC rows in
        # memory so those checks do not repeatedly perform pandas index lookup
        # and Series construction.  The cache is deliberately scoped to one
        # timestamp: it cannot expose future data and remains bounded by the
        # number of instruments in the universe.
        self._bar_cache_timestamp: pd.Timestamp | None = None
        self._bar_cache: dict[str, dict[str, Any] | None] = {}
        self.universe_resolver = universe_resolver
        for raw_key, raw_frame in frames.items():
            instrument = parse_instrument(raw_key)
            frame = self._normalize_frame(raw_frame, instrument.key)
            self.frames[instrument.key] = frame
            self.aliases[instrument.symbol] = instrument.key
            self.aliases[str(raw_key)] = instrument.key
        values: set[pd.Timestamp] = set()
        for frame in self.frames.values():
            values.update(pd.Timestamp(item) for item in frame.index)
        self._timestamps = pd.DatetimeIndex(sorted(values))

    @property
    def timestamps(self) -> pd.DatetimeIndex:
        return self._timestamps

    def set_clock(self, current_dt: Any, *, include_current: bool) -> None:
        timestamp = pd.Timestamp(current_dt)
        self.current_dt = timestamp
        if include_current:
            self._visible_dt = timestamp
            return
        previous_index = int(self._timestamps.searchsorted(timestamp, side="left")) - 1
        self._visible_dt = self._timestamps[previous_index] if previous_index >= 0 else None

    def resolve_key(self, symbol: object) -> str:
        raw = str(symbol or "").strip()
        if raw in self.frames:
            return raw
        if raw in self.aliases:
            return self.aliases[raw]
        parsed = parse_instrument(raw)
        if parsed.key in self.frames:
            return parsed.key
        matching = [key for key in self.frames if key.split(":", 1)[-1].split("@", 1)[0] == parsed.symbol]
        if len(matching) == 1:
            return matching[0]
        raise StrategyDataError(f"strategyV2.dataUnavailable:{raw}")

    def visible_frame(self, symbol: object, count: int | None = None) -> pd.DataFrame:
        key = self.resolve_key(symbol)
        frame = self.frames[key]
        if self._visible_dt is None:
            return frame.iloc[0:0].copy()
        end_index = int(frame.index.searchsorted(self._visible_dt, side="right"))
        visible = frame.iloc[:end_index]
        if count is not None and int(count) > 0:
            visible = visible.tail(int(count))
        return visible.copy()

    def history(
        self,
        symbols: object,
        *,
        count: int,
        fields: object = None,
    ) -> pd.DataFrame | dict[str, pd.DataFrame]:
        requested = _as_list(symbols)
        selected_fields = [str(item).strip().lower() for item in _as_list(fields)] if fields else []
        output: dict[str, pd.DataFrame] = {}
        for symbol in requested:
            key = self.resolve_key(symbol)
            frame = self.visible_frame(key, count=count)
            if selected_fields:
                available = [field for field in selected_fields if field in frame.columns]
                frame = frame.loc[:, available]
            output[key] = frame
        if len(output) == 1:
            return next(iter(output.values()))
        return output

    def current(self, symbol: object, field: str = "close", default: float = 0.0) -> float:
        frame = self.visible_frame(symbol, count=1)
        if frame.empty or field not in frame.columns:
            return float(default)
        try:
            value = float(frame.iloc[-1][field])
            return value if value == value else float(default)
        except Exception:
            return float(default)

    def open_at(self, symbol: object, timestamp: Any) -> float | None:
        bar = self.bar_at(symbol, timestamp)
        value = float((bar or {}).get("open") or 0.0)
        return value if value > 0 else None

    def close_at(self, symbol: object, timestamp: Any) -> float | None:
        bar = self.bar_at(symbol, timestamp)
        value = float((bar or {}).get("close") or 0.0)
        return value if value > 0 else None

    def bar_at(self, symbol: object, timestamp: Any) -> dict[str, Any] | None:
        key = self.resolve_key(symbol)
        ts = pd.Timestamp(timestamp)
        if self._bar_cache_timestamp != ts:
            self._bar_cache_timestamp = ts
            self._bar_cache.clear()
        if key in self._bar_cache:
            return self._bar_cache[key]

        frame = self.frames[key]
        if ts not in frame.index:
            self._bar_cache[key] = None
            return None
        row = frame.loc[ts]
        try:
            bar: dict[str, Any] = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume") or 0.0),
            }
            for name in (
                "suspended",
                "is_suspended",
                "limit_up",
                "is_limit_up",
                "limit_down",
                "is_limit_down",
                "lot_size",
                "industry",
            ):
                if name in row.index:
                    bar[name] = row.get(name)
            self._bar_cache[key] = bar
            return bar
        except (KeyError, TypeError, ValueError):
            self._bar_cache[key] = None
            return None

    def panel(self, symbols: Iterable[object] | None = None, *, count: int | None = None) -> dict[str, pd.DataFrame]:
        requested = list(symbols or self.frames.keys())
        return {self.resolve_key(symbol): self.visible_frame(symbol, count=count) for symbol in requested}

    def universe(self, reference: str) -> list[str]:
        if not self.universe_resolver:
            return []
        when = self.current_dt or pd.Timestamp.utcnow()
        return [parse_instrument(value).key for value in self.universe_resolver(reference, when)]

    @classmethod
    def _normalize_frame(cls, raw: pd.DataFrame, key: str) -> pd.DataFrame:
        if raw is None or raw.empty:
            raise StrategyDataError(f"strategyV2.emptyData:{key}")
        frame = raw.copy()
        if not isinstance(frame.index, pd.DatetimeIndex):
            time_column = next((name for name in ("time", "datetime", "date", "timestamp") if name in frame.columns), "")
            if not time_column:
                raise StrategyDataError(f"strategyV2.timeIndexRequired:{key}")
            frame.index = pd.to_datetime(frame.pop(time_column), errors="coerce")
        else:
            frame.index = pd.to_datetime(frame.index, errors="coerce")
        frame = frame[~frame.index.isna()].sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        missing = [column for column in cls.REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise StrategyDataError(f"strategyV2.ohlcRequired:{key}:{','.join(missing)}")
        if "volume" not in frame.columns:
            frame["volume"] = 0.0
        return frame


def _as_list(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return list(value.keys())
    try:
        return list(value)
    except TypeError:
        return [value]
