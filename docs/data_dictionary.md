# Astock 数据字典

## 日线 (stock_daily, Tushare daily 接口)

| 字段 | 类型 | 单位 | 说明 |
|------|------|------|------|
| `ts_code` | str | — | Tushare 股票代码格式 (600000.SH / 000001.SZ) |
| `trade_date` | str | — | 交易日，YYYYMMDD |
| `open` | float | 元 | **前复权**开盘价 |
| `high` | float | 元 | **前复权**最高价 |
| `low` | float | 元 | **前复权**最低价 |
| `close` | float | 元 | **前复权**收盘价。所有技术指标计算基于此前复权价 |
| `pre_close` | float | 元 | 前日收盘价（未复权） |
| `change` | float | 元 | 涨跌额（未复权） |
| `pct_chg` | float | % | 涨跌幅百分比（未复权，原始值） |
| `volume` | float | 股 | 成交量，单位**股**（非手）。1手=100股 |
| `amount` | float | 元 | 成交额，单位**元**（非万元） |

## 每日指标 (daily_basic, Tushare daily_basic 接口)

| 字段 | 类型 | 单位 | 说明 |
|------|------|------|------|
| `pe` | float | 倍 | 市盈率 |
| `pe_ttm` | float | 倍 | 滚动市盈率 |
| `pb` | float | 倍 | 市净率 |
| `ps` | float | 倍 | 市销率 |
| `ps_ttm` | float | 倍 | 滚动市销率 |
| `total_mv` | float | 万元 | 总市值，单位**万元** |
| `circ_mv` | float | 万元 | 流通市值，单位**万元** |
| `turnover_rate` | float | % | 换手率 |
| `volume_ratio` | float | — | 量比 |

## 财务指标 (fina_indicator 接口)

| 字段 | 类型 | 单位 | 说明 |
|------|------|------|------|
| `roe` | float | % | 净资产收益率 |
| `roa` | float | % | 总资产收益率 |
| `grossprofit_margin` | float | % | 毛利率 |
| `netprofit_margin` | float | % | 净利率 |
| `netprofit_yoy` | float | % | 净利润同比增长 |
| `or_yoy` | float | % | 营业收入同比增长 |
| `current_ratio` | float | — | 流动比率 |
| `quick_ratio` | float | — | 速动比率 |
| `debt_to_assets` | float | % | 资产负债率 |

## 复权说明

所有技术指标（均线、MACD、RSI、布林带等）基于 **前复权** close 计算。
前复权含义：以最近一次除权为基准，历史价格按复权因子向前调整。适合连续技术分析。

## 变更记录

- **2026-08-09**: 初始版本，覆盖 stock_daily / daily_basic / fina_indicator 核心字段。
