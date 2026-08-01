# 短线风险避雷清单 — 技术文档

## 概述

`risk_scanner.py` 是 A股量化分析助手的双引擎风险模块，提供：

| 引擎 | 方法 | 功能 | 目标 |
|------|------|------|------|
| 异动预警引擎 | `scan_all()` | 遍历用户自选股，检测技术面异动 | 个人自选股预警 |
| 风险扫描引擎 | `scan_risk_list()` | 全市场扫描，5维度分层 | 全球风险避雷 |

---

## 风险扫描引擎 — 5 维度扫描

### 1. ST退市风险 (`st_risk`)

**扫描逻辑**：查询 `stocks` 表 `name` 列包含 `ST` 或 `*ST` 的股票。

**风险提示**："XXX 为ST/*ST股票，存在退市风险，短线交易流动性差、涨跌幅受限"

**数据来源**：`stocks` 表全表扫描，不依赖日线数据。

**阈值**：无参数，全面包含。

---

### 2. 连板过热 (`surge_overheat`)

**扫描逻辑**：
- 取最新交易日和 5 个交易日前的时间点
- 使用 `_find_closest_trade_date()` 在 ±3 天内查找最近交易日
- 计算近5日累计涨幅：(`close_now` - `close_5d`) / `close_5d` × 100
- 同时要求最新日涨幅 > 5%

**SQL 查询**：
```sql
SELECT d0.ts_code, d5.close AS close_base, d0.close AS close_now,
       ROUND((d0.close - d5.close) / d5.close * 100, 2) AS cum_pct,
       ROUND(d0.pct_chg, 2) AS latest_pct
FROM stock_daily d0
JOIN stock_daily d5 ON d5.ts_code = d0.ts_code
WHERE d0.trade_date = :trade_date AND d5.trade_date = :date_5d
```

**阈值**：
- 近5日累计涨幅 > 30%
- 最新日涨幅 > 5%
- LIMIT 80

**风险提示**："近5日累计涨幅 {cum_pct}%（{close_base}→{close_now}），今日涨幅 {latest_pct}%，追高风险极大"

---

### 3. 断崖下跌 (`cliff_drop`)

**扫描逻辑**：
- 取最近 4 个交易日（去重后）
- 统计收阴天数（`pct_chg < 0`）
- 计算期间累计跌幅
- 使用 CTE 去重（`GROUP BY ts_code, trade_date`）

**SQL 查询**：
```sql
WITH dedup AS (
    SELECT ts_code, trade_date, MAX(close) AS close, MAX(pct_chg) AS pct_chg
    FROM stock_daily WHERE trade_date <= :trade_date
    GROUP BY ts_code, trade_date
),
recent AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
    FROM dedup
)
-- 取 rn <= 4 的窗口，统计 down_days 和 cum_pct
```

**阈值**：
- 最近 4 日（至少 3 天数据）
- ≥ 3 天收阴
- 累计跌幅 > 5%
- LIMIT 50

**风险提示**："近7日 {down_days} 天收阴，累计跌幅 {cum_pct}%，短线趋势恶化，注意止损"

---

### 4. 高换手异动 (`high_turnover`)

**扫描逻辑**：
- 按最新交易日成交额降序排列
- 过滤单日成交额 > 10 亿元人民币的股票
- 使用 GROUP BY 去重

**SQL 查询**：
```sql
SELECT d.ts_code, s.name,
       ROUND(MAX(d.amount) / 10000.0, 2) AS amount_wan
FROM stock_daily d
JOIN stocks s ON s.ts_code = d.ts_code
WHERE d.trade_date = :trade_date AND d.amount > 0
GROUP BY d.ts_code
ORDER BY amount_wan DESC
LIMIT 100
```

**阈值**：
- 单日成交额 > 100,000 万元（10 亿）
- LIMIT 50

**风险提示**："单日成交额 {amount} 万元，涨跌幅 {pct_chg}%，注意主力出货风险"

---

### 5. 缩量阴跌 (`volume_drain`)

**扫描逻辑**：
- 取最近 4 个交易日（去重后）
- 对比近期（rn ≤ 2）和早期（rn > 2）的平均成交量
- 统计下跌天数
- 计算累计跌幅

**SQL 查询**：
```sql
WITH dedup AS (...),
ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
    FROM dedup
),
latest_vol AS (
    SELECT ts_code,
           AVG(CASE WHEN rn <= 2 THEN vol END) AS vol_recent,
           AVG(CASE WHEN rn > 2 THEN vol END) AS vol_earlier,
           SUM(CASE WHEN rn <= 4 AND pct_chg < 0 THEN 1 ELSE 0 END) AS down_days,
           ROUND((...) AS cum_pct
    FROM ranked WHERE rn <= 4
    GROUP BY ts_code
    HAVING COUNT(*) >= 3
)
```

**阈值**：
- 最近 4 日（至少 3 天数据）
- ≥ 2 天收阴
- 累计跌幅 > 5%
- 近期成交量 < 早期成交量的 80%
- LIMIT 50

**风险提示**："近5日 {down_days} 天收阴，累计跌幅 {cum_pct}%，成交量萎缩 {vol_chg_pct}%，资金持续离场"

---

## 异动预警引擎 — 4 类信号

### 1. RSI 超买超卖

**检测逻辑**：
- 取最新日线的 RSI(14) 值
- RSI ≥ 80：超买警告
- RSI ≤ 20：超卖提示

---

### 2. MACD 金叉死叉

**检测逻辑**：
- 取最近两日的 MACD DIF/DEA 值
- DIF 上穿 DEA：金叉信号
- DIF 下穿 DEA：死叉信号

---

### 3. 涨跌幅异常

**检测逻辑**：
- 单日涨幅 ≥ 9.5%：涨幅异常
- 单日跌幅 ≤ -9.5%：跌幅异常

---

### 4. 放量异动

**检测逻辑**：
- 使用窗口函数计算 20 日均量
- 当日成交量 > 20 日均量的 2 倍：放量信号

---

## 数据持久化

### 风险扫描结果

- 写入 `risk_list_results` 表（按 `calc_date` 分区，先删后插）
- 缓存至内存 `risk:list:{trade_date}`，TTL 86400 秒

### 异动预警通知

- 写入 `alert_notifications` 表（按 `user_id` 分区）
- 无缓存，每次实时读 DB

---

## 定时调度

收盘后自动运行，在 `scheduler.py` 中配置：

| 任务 | 时间 | 触发 |
|------|------|------|
| 日线同步 | 15:35 (北京时间) | `sync_daily_data()` |
| 离线引擎 | 15:40 (北京时间) | `scan_all()` + `scan_risk_list()` |

---

## 表结构

### `risk_list_results`

```sql
CREATE TABLE risk_list_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calc_date VARCHAR(10) NOT NULL,
    risk_category VARCHAR(30) NOT NULL,
    ts_code VARCHAR(20) NOT NULL,
    stock_name VARCHAR(50),
    risk_detail TEXT,
    created_at DATETIME
);
```

### `alert_notifications`

```sql
CREATE TABLE alert_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    alert_config_id INTEGER,
    ts_code VARCHAR(20),
    stock_name VARCHAR(50),
    alert_type VARCHAR(50),
    content TEXT,
    is_read INTEGER DEFAULT 0,
    created_at DATETIME
);
```

---

## 已知限制

1. **日线数据连续性**：市场有节假日导致日期间断，影响窗口计算。使用 `_find_closest_trade_date()` 辅助查找最近交易日。
2. **stock_daily 重复行**：某些交易日同一 ts_code 有多行数据（如 20260730/20260731），所有 SQL 必须 `GROUP BY ts_code, trade_date` 去重。
3. **无财务数据**：`stock_financials` 表当前为空（0 行），财务维度扫描暂未启用。
4. **无涨跌停深度分析**：高换手指标仅用成交额绝对值，未结合流通市值计算换手率。
