# 数据接入盘点清单（8000 积分 + 2000 元）

> 目的：把用户当前权限能调用的数据接口全部接入网站 DB，避免遗漏导致重复补拉。
> 当前权限：**8000 积分**（2026-08 升级）+ **2000 元 A股历史分钟**。

## 一、已接入数据（DB 表，全部同步中）

| 数据 | 表 | 接口 | 积分 | 同步频率 |
|------|-----|------|------|---------|
| 日线行情 | stock_daily | daily | 120 | 每日 |
| 每日估值 | daily_basic | daily_basic | 120 | 每日（含 pe_ttm/ps/ps_ttm/dv_ratio/dv_ttm，2026-08 补全）|
| 指数日线 | stock_daily | index_daily | 120 | 每日 |
| 股票基础 | stocks | stock_basic | 0 | 每日 |
| 板块分类 | sectors | index_classify | 0 | 每日 |
| 涨跌停 | limit_list_records | limit_list | 120 | 每日 |
| 融资融券 | margin_records | margin | 2000 | 每日 |
| 个股资金流 | moneyflow_records | moneyflow | 2000 | 每日 |
| 北向资金 | moneyflow_hsgt | moneyflow_hsgt | 2000 | 每日 |
| 十大成交股 | hsgt_top10 | hsgt_top10 | 2000 | 每日 |
| 十大流通股东 | top10_floatholders | top10_floatholders | 2000 | 每周 |
| 股东户数 | stk_holdernumber | stk_holdernumber | 2000 | 每周 |
| **筹码及胜率** | cyq_perf | cyq_perf | **8000** | 每日 |
| **龙虎榜** | top_list | top_list | **5000** | 每日 |
| **龙虎榜机构** | top_inst | top_inst | **5000** | 每日 |
| **概念/行业板块** | dc_index | dc_index | **6000** | 每日 |
| **概念成分** | dc_member | dc_member | **6000** | 按需 |
| **券商金股** | broker_recommend | broker_recommend | **6000** | 每月 |
| **限售解禁** | share_float | share_float | **5000** | 每周 |
| **股东增减持** | stk_holdertrade | stk_holdertrade | **5000** | 每周 |
| **业绩快报** | express | express | 2000 | 每周 |

## 二、已封装未接入/未同步的接口

| 接口 | 积分 | 说明 | 状态 |
|------|------|------|------|
| income/balancesheet/cashflow | 2000 | 三大报表（fina_indicator 已覆盖大部分，报表反推备用）| 已封装未用 |
| forecast 业绩预告 | 2000 | 需 ann_date/ts_code 参数 | 未封装 |
| top10_holders 十大股东 | 2000 | 已有 top10_floatholders，重复度高 | 已建表未同步 |
| stk_factor_pro 量化因子 | 8000 | 技术指标（已研究证伪），数据存 pkl 供研究 | 未同步 DB |
| cyq_chips 筹码分布明细 | 8000 | 单股筹码分布（cyq_perf 是汇总，chips 是分价位）| 已确认可用未同步 |

## 三、2000 元分钟（独立权限）

| 接口 | 说明 | 状态 |
|------|------|------|
| stk_mins | A股历史分钟 1/5/15/30/60min | 已证伪（分钟因子是日线影子），数据按需拉存 pkl 供研究 |

## 四、8000 积分以上（未达，不可用）

- 10000 积分：盈利预测、机构调研
- 15000 积分：游资、涨停榜单、热板

## 五、同步入口

- 定时任务：`backend/app/core/scheduler.py` `_sync_daily_all()`（收盘后）
- 手动触发：`POST /api/v1/admin/tasks/run-daily-batch`
- 启动自动同步：`backend/app/main.py` `_auto_sync()`
