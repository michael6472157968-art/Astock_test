# Astock_test × QuantDinger 对比分析 & 优化清单

> 生成日期: 2026-08-03 | 更新: 2026-08-03 (Tushare 2000积分 + AI成本分析)
> 
> **Tushare 积分状态**: 2000 积分档位 — 北向资金、资金流向、财务数据、每日指标等高级接口已解锁

---

## 第1步：项目基本情况

| 维度 | Astock_test (我) | QuantDinger |
|------|------------------|-------------|
| 定位 | A股量化分析助手 | 开源AI自动化交易系统 |
| 后端框架 | FastAPI (Python) | Flask (Python) |
| 数据库 | SQLite (单文件) | PostgreSQL + 2×Redis |
| 前端 | 原生 JS (无框架) | Vue 3 + Ant Design Vue (独立仓库) |
| 移动端 | 无 | Capacitor H5 + 原生 (独立仓库) |
| 图表库 | ECharts | KLineCharts + ECharts |
| 数据源 | Tushare (单一) | yfinance, AkShare, CCXT, 多源 |
| 市场支持 | 仅A股 | 加密货币、美股、港股、外汇、大宗商品 |
| 策略系统 | 简单回测 (2策略) | Strategy API V2, 实盘/模拟盘运行 |
| AI能力 | 无 | 多LLM提供商, AI聊天, AI分析, MCP |
| 部署 | 本地 uvicorn | Docker Compose 多进程 |
| 可观测性 | 无 | Prometheus + Grafana + Alertmanager |
| 社区 | 无 | 指标交易市场、社区 |

---

## 第2步：QuantDinger 后端设计分析

### 架构模式

QuantDinger 使用**多进程职责分离**：

| 进程 | 职责 |
|------|------|
| `backend` (API) | HTTP 请求处理、认证、校验 |
| `trading-worker` | 策略运行、待处理订单、券商会话管理 |
| `scheduler-worker` | 组合、部署、支付、信号调度 |
| `celery-worker` | 有限异步任务 (AI、回测、报告) |
| `celery-beat` | 定时向 Redis 投递任务 |

核心原则：
- HTTP API **不承载**长期运行的交易循环和调度线程
- 缓存 Redis 与任务 Redis **完全分离** (不同淘汰策略)
- 风险操作通过 PostgreSQL 命令记录 → Worker 消费 (而非同步执行)
- 所有适配器标准化为内部契约，不依赖 Flask request/g

### API 设计

- 100+ 个 HTTP 端点
- 统一的 Human API 响应信封: `{code: 1, msg: "success", data: ...}`
- 独立的 Agent Gateway: `/api/agent/v1` (权限范围、限流、审计)
- OpenAPI 3.0 自动生成文档 (ReDoc 渲染)
- 分页标准: `PaginationMeta {page, page_size, total}`

### 关键技术栈

- Python 3.12 + Gunicorn + Celery
- PostgreSQL 18 + Redis 8
- Docker Compose 多容器编排
- Prometheus metrics + Grafana 仪表盘
- MCP Server (Agent Tool 暴露)
- 多AI提供商: OpenRouter, OpenAI, Google, DeepSeek, Grok, MiniMax

---

## 第3步：功能模块对比

### 3.1 Astock 已有功能

| 模块 | 页面 | 后端路由 | 状态 |
|------|------|---------|------|
| 首页仪表盘 | index.html | /api/v1/market/* | 指数+情绪+热门 |
| 诊股 | diagnosis.html | /api/v1/diagnosis | 技术指标+K线 |
| 板块轮动 | sector-rotation.html | /api/v1/sector-rotation | 板块热力图+表格 |
| 板块行情 | sector.html | /api/v1/sector | 板块成分股列表 |
| 每日复盘 | review.html | /api/v1/review | 市场回顾报告 |
| 风险扫描 | risk-list.html | /api/v1/risk-list | 业绩暴雷、减持等 |
| 策略回测 | backtest.html | /api/v1/backtest | MA交叉/RSI(仅2策略) |
| 股票池 | stock-pool.html | /api/v1/stock-pool | 热门龙头选股 |
| 自选股提醒 | alerts.html | /api/v1/alerts | 价格提醒 |
| 用户系统 | login/register/profile | /api/v1/auth, /api/v1/user | JWT+会员等级 |
| 会员系统 | membership | /api/v1/membership | 免费/月/年/管理员 |

### 3.2 QuantDinger 有而我没有

| 模块 | 说明 | 优先级 |
|------|------|--------|
| **全局市场仪表盘** | 美股/港股/加密货币/外汇/大宗 综合视图 + 经济日历 + 情绪指数 + 新闻聚合 | 高 |
| **AI 分析** | 任意标的快分析, LLM 多模型支持, 异步提交, 聊天会话历史 | 高 |
| **策略自动化** | Python 策略编写 → 模拟盘 → 实盘执行 → 监控 (非仅回测) | 高 |
| **实盘/券商对接** | IBKR、Alpaca、Binance、OKX、Bitget、Bybit、Gate、HTX | 中 |
| **社区指标市场** | 发布/购买指标、评论、销售统计、管理员审核 | 低 |
| **可观测性** | Prometheus + Grafana + Alertmanager (请求ID、指标、仪表盘) | 中 |
| **MCP Server** | Cursor/Claude Code 等客户端可调用交易工具 | 中 |
| **Agent Gateway** | /api/agent/v1 受限 API，权限范围+审计 | 中 |
| **多用户管理后台** | 用户CRUD、积分管理、VIP管理、登录日志 | 中 |
| **账单系统** | USDT 支付、积分、套餐 | 低 |
| **通知系统** | Email、SMS、Telegram 多渠道通知 | 中 |
| **股票池/Universe** | 标准化股票池管理、快照、基本面同步 | 中 |

### 3.3 我有但 QuantDinger 没有

| 模块 | 说明 |
|------|------|
| A股诊股 (技术分析) | QuantDinger 的快分析更偏宏观+AI，没有纯技术指标诊股 |
| 板块轮动热力图 | A股特有概念 |
| 每日中文复盘 | A股市场复盘 |
| 风险避雷列表 | 业绩暴雷、减持、ST等 |

---

## 第4步：前端设计对比

### 4.1 QuantDinger 前端 (参考 Vue 独立仓库)

根据 README 披露的技术栈：
- **框架**: Vue 3 + Ant Design Vue (企业级组件库)
- **图表**: KLineCharts (K线) + ECharts
- **移动端**: Capacitor 跨平台

### 4.2 Astock 前端技术栈

- **技术**: 原生 JavaScript (无框架)
- **CSS**: 自定义 CSS Variables 三主题系统 (dark/light/warm)
- **图表**: ECharts CDN
- **路由**: 纯 HTML 多页面 (无 SPA 路由)
- **状态**: localStorage (无响应式状态管理)

### 4.3 差距与改进方向

| 维度 | 当前状态 | 目标 | 难度 |
|------|---------|------|------|
| UI框架 | 原生 JS，手写DOM | 短期不改；长期可考虑轻量框架 | 高 |
| 设计系统 | CSS Variables 三主题 | 已经很完整，保持 | - |
| 数据卡牌 | 手动 grid 布局 | 可优化动画和过渡 | 低 |
| K线图表 | ECharts 基础K线 | 增加更多交互(拖拽缩放、指标叠加) | 中 |
| 移动端 | 仅响应式CSS | 优化移动端触摸体验 | 低 |
| 导航 | 顶部导航栏 | 可增加面包屑、侧边栏 | 中 |

---

## 第5步：优化清单

---

## 高优先级 (视觉冲击大、功能价值高、改动量可控)

### - [x] 1. 首页升级为"A股市场总览"仪表盘 **`[已提交本地 21db146]`**

**状态**: 已完成。新增 KPI 四栏(北向资金/成交额/市场宽度/行业宽度) + 板块热力迷你图 + 新增 `/api/v1/market/dashboard` 聚合端点 + `get_moneyflow_hsgt()` Tushare 客户端方法。全局 CSS 已对齐 QuantDinger Ant Design 深色蓝科技风(调色板+圆角+Inter 字体)。

**改动文件**: `frontend/index.html`, `frontend/css/app.css`, `backend/app/api/market.py`, `backend/app/services/tushare_client.py`

**当前状态**: 首页只展示4个指数卡片 + 涨跌比 + 4只热门股，信息密度低

**目标状态**: 参考 QuantDinger dashboard/summary 的数据维度，增加：
  - 板块热力概览 (复用已有 sector-rotation 数据，小尺寸预览)
  - 今日成交额 (总成交额显示)
  - 市场宽度指标 (涨跌比基础上加行业宽度)
  - 北向资金净流入 (2000积分已解锁 moneyflow_hsgt 接口，无权限风险)

**改动范围**: `frontend/index.html` + `backend/app/api/market.py`

**风险等级**: 低 (所有数据源均已就绪)

---

### - [ ] 2. 全局市场日历 / 休市倒计时

**当前状态**: 仅在首页 banner 显示"今日休市"，无后续交易日预告

**目标状态**: 参考 QuantDinger economic_calendar 功能，增加：
  - 本周交易日历显示
  - 下一个交易日前休市倒计时
  - 即将发生的重要宏观事件 (中国PMI、CPI 等)

**改动范围**: `frontend/index.html` (新增小组件) + `backend/app/api/market.py` (新增日历端点)

**风险等级**: 低

---

### - [ ] 3. K线图增加更多技术指标叠加

**当前状态**: 诊股页只有基础K线 + 成交量，技术指标用文字输出

**目标状态**: K线图上叠加:
  - MA5/MA10/MA20/MA60 均线
  - MACD 副图
  - KDJ / RSI 副图
  - 布林带 (上轨/中轨/下轨)

**改动范围**: `frontend/diagnosis.html` + `backend/app/api/diagnosis.py`

**风险等级**: 中 (需要重构 K线图代码)

---

### - [ ] 4. 策略回测升级 — 支持更多策略类型和可视化

**当前状态**: 仅 MA交叉 和 RSI 两个策略，回测结果JSON展示

**目标状态**: 参考 QuantDinger backtest_center:
  - 增加策略类型: MACD金叉死叉、布林带突破、动量策略
  - 结果可视化: 权益曲线图、回撤图、月度收益热力图
  - 参数优化入口 (简单网格搜索)

**改动范围**: `frontend/backtest.html` + `backend/app/services/backtest_engine.py` + `backend/app/api/backtest.py`

**风险等级**: 中

---

### - [ ] 5. 个股诊断增加AI分析入口

**当前状态**: 诊股纯技术指标计算，无AI辅助分析

**目标状态**: 参考 QuantDinger fast_analysis:
  - 诊股页底部增加"AI 辅助解读"按钮
  - 后台接入 LLM (优先国内模型如 DeepSeek/通义千问，性价比优于 GPT-4o)
  - 按需扣积分，异步提交

**费用分析**:

| 项目 | 估算 |
|------|------|
| 单次调用 prompt | ~1000-1500 tokens (技术指标数据 + 系统指令) |
| 单次调用输出 | ~500-800 tokens (一段中文分析) |
| DeepSeek-V3 单价 | ¥0.001/1K tokens 输入, ¥0.002/1K tokens 输出 |
| **单次成本** | **≈ ¥0.003** (不到1分钱) |
| 100用户 × 5只/天 | ≈ ¥1.5/天, ≈ ¥45/月 |
| GPT-4o 对比 | 约 $0.015/次, 同等用量 ≈ ¥330/月 (6-7倍差距) |

**三层防线** (费用失控保护):
1. **会员限额**: 免费用户 3次/天, 月付 10次/天, 年付 30次/天
2. **积分扣除**: 每次分析扣 1 积分 (与会员并行)
3. **结果缓存**: 同一股票 + 同一交易日 → 直接返回缓存，不重复调用 LLM

**改动范围**: `frontend/diagnosis.html` + `backend/app/api/diagnosis.py` + `backend/app/services/ai_analysis.py` (新建)

**风险等级**: 低 (费用可控，三层防线兜底)

---

### - [ ] 6. 诊股页增加资金流向分析 (2000积分新解锁)

**当前状态**: 诊股页仅技术指标 (MA/MACD/KDJ/RSI)，无资金面数据

**目标状态**: 利用 Tushare moneyflow 接口：
  - 主力净流入/流出趋势图 (近20日)
  - 超大单/大单/中单/小单资金分布
  - 资金流向与股价走势叠加对比

**改动范围**: `frontend/diagnosis.html` + `backend/app/services/tushare_client.py` + `backend/app/api/diagnosis.py`

**风险等级**: 低 (moneyflow 接口 2000 积分直接可用，纯展示，无第三方依赖)

---

## 中优先级 (功能体验提升)

### - [ ] 7. 多数据源支持

**当前状态**: 仅 Tushare，token 过期或额度耗尽即不可用

**目标状态**: 参考 QuantDinger multi-provider 设计:
  - 增加 akshare/yfinance 作为免费备选
  - 数据源抽象层：DataSource → 统一内部格式
  - 自动降级：Tushare不可用时切换到备选

**改动范围**: `backend/app/services/tushare_client.py` (抽象化) + 新建 `backend/app/data_sources/`

**风险等级**: 中 (需要重构数据获取层)

---

### - [ ] 8. 添加请求级缓存机制

**当前状态**: 简单内存缓存 (cache_get/cache_set)，无并发保护、无软过期

**目标状态**: 参考 QuantDinger `cached_or_compute`:
  - 并发互斥: 同一key只有一个并发请求执行计算
  - 软过期: 过期后先返回旧值，后台异步刷新
  - 手动强制刷新: `?force=true`

**改动范围**: `backend/app/core/cache.py`

**风险等级**: 低

---

### - [ ] 9. 增加通知订阅功能

**当前状态**: 仅浏览器端价格提醒弹窗，无推送通知

**目标状态**: 参考 QuantDinger multi-channel notifications:
  - 价格突破、策略信号 → 站内通知 + 可选邮件/Telegram
  - 通知设置页面: 配置渠道、开关

**改动范围**: `frontend/alerts.html` + `backend/app/api/alerts.py` + 新建 `backend/app/services/notifications/`

**风险等级**: 中

---

### - [ ] 10. 用户管理后台增强

**当前状态**: admin-trigger.html 仅含手动同步数据按钮

**目标状态**: 参考 QuantDinger admin routes:
  - 用户列表 + 搜索/筛选
  - 手动管理VIP/积分
  - 查看用户活跃统计
  - 导出功能

**改动范围**: `frontend/admin-trigger.html` (扩展) + `backend/app/api/admin.py` (扩展)

**风险等级**: 中

---

### - [ ] 11. 增加 OpenAPI / API 文档页

**当前状态**: 无API文档，调试靠手工

**目标状态**: 参考 QuantDinger ReDoc:
  - FastAPI 自带的 Swagger UI 已可用: `/docs`
  - 增加自定义文档页 (中文说明)
  - 在管理后台添加API文档入口

**改动范围**: `backend/app/main.py` (已有FastAPI自带的/docs，仅需确认)

**风险等级**: 低

---

### - [ ] 12. 行业龙头成分股展示

**当前状态**: sector.html 展示板块列表和个股

**目标状态**: 参考 QuantDinger universe/members 设计:
  - 每个板块的龙头股列表 (按市值/换手率/涨跌幅)
  - 资金流向概览 (主力净流入)
  - 龙头股的 mini K线预览

**改动范围**: `frontend/sector.html` + `backend/app/api/market.py` (sector_router)

**风险等级**: 低

---

## 低优先级 (锦上添花)

### - [ ] 13. 移动端PWA支持

**当前状态**: 仅响应式CSS适配手机

**目标状态**: 参考 QuantDinger mobile:
  - 添加 manifest.json + Service Worker
  - 支持添加到主屏幕
  - 离线缓存关键数据

**改动范围**: `frontend/` (新增 manifest + sw.js) + `frontend/index.html` (meta标签)

**风险等级**: 低

---

### - [ ] 14. 增加暗色模式自动切换

**当前状态**: 三主题手动选择 (dark/light/warm)，需用户在header选择

**目标状态**: 
  - 默认跟随系统 `prefers-color-scheme`
  - 手动选择覆盖自动
  - 记住偏好

**改动范围**: `frontend/js/app.js` (新增) + `frontend/css/app.css` (调整)

**风险等级**: 低

---

### - [ ] 15. 导出报告功能增强 (PDF/图片)

**当前状态**: 复盘报告有基础PDF下载

**目标状态**: 参考 QuantDinger AI chat report PDF:
  - 诊股报告支持PDF下载
  - 回测结果支持导出
  - 支持截图分享

**改动范围**: `backend/app/api/review.py` + `backend/app/services/` (PDF生成)

**风险等级**: 低

---

### - [ ] 16. 性能分析/诊断工具

**当前状态**: 无性能监控，慢查询不可见

**目标状态**: 参考 QuantDinger observability:
  - API 请求耗时日志
  - 缓存命中率统计
  - 健康检查增强 (DB响应时间、缓存状态)

**改动范围**: `backend/app/core/` (新增 middleware) + `backend/app/main.py`

**风险等级**: 低

---

## 附录: 两个项目的架构对比总结

```
Astock_test (当前)                  QuantDinger (参考目标)
─────────────────────────           ─────────────────────────
FastAPI → SQLite (单进程)           Flask → PostgreSQL + 2×Redis (多进程)
内存缓存 (简单KV)                   分层缓存 (并发互斥+软过期)
单一数据源 (Tushare)                多数据源 (抽象层+自动降级)
JS原生 + ECharts                    Vue3 + Ant Design + KLineCharts
JWT + 会员等级                      JWT + Agent Token + MCP
手动同步                            调度器 + Celery定时任务
无监控                              Prometheus + Grafana
本地部署                             Docker Compose多容器
A股专属                             全球市场
纯回测                              回测→模拟盘→实盘完整链路
```

---

## 实施建议

建议按以下顺序推进 (每次1-2项，独立合入):

1. **先改前端可见的**: 首页仪表盘升级 (#1) ✅ + 资金流向 (#6) ✅ + K线技术指标叠加 (#3)
2. **积分体系 + 诊股改造**: 积分表+注册/激活送积分+签到+诊股改积分消耗 ✅ **（阶段1完成）**
3. **AI + 竞猜 + 用户管理**: AI分析+竞猜+管理后台升级（阶段2）
4. **再改数据可靠性**: 缓存机制升级 (#8) + 多数据源备选 (#7)
5. **然后增强分析**: 策略回测升级 (#4)
6. **最后锦上添花**: 通知系统 (#9) + 其余低优项

### 关键决策点

| 议题 | 决策 |
|------|------|
| AI 模型选择 | 优先 DeepSeek-V4-Flash（速度更快，诊股场景够用），V4-Pro 备选 |
| AI 防滥用 | 积分扣除（2分/次）+ 同股同日缓存，双重保护 |
| 北向资金 | 2000 积分已解锁，直接可用，无权限风险 |
| 数据源策略 | Tushare 主数据源，akshare 仅降级备选 |
| AI API 兼容 | DeepSeek API 兼容 OpenAI SDK，改 base_url 即可 |
| 诊股模型 | 移除每日次数限制，改为积分消耗（1分/次，VIP免费） |
| 积分安全 | User.credits 原子操作 + CreditLedger 流水不可删改 |

---

## 第6步：积分体系 + 诊股改造 + AI分析 + 用户管理 方案 v2

> 讨论日期: 2026-08-03 | 状态: 方案已确认，待实施

### 6.1 积分获取渠道

| 渠道 | 积分 | 规则 |
|------|------|------|
| 注册账号 | +10 | 一次性，注册时同一事务发放 |
| 每日签到 | +3（免费）/ +5（VIP） | 每天一次，连续7天额外 +5（第7天共 +8/+10），断签重置 |
| 竞猜大盘涨跌 | +5 猜对 / +1 参与 | 交易日 9:00 前提交涨/跌，收盘后结算，每人每天一次 |
| 月度会员激活 | +100 | 激活码兑换时同一事务赠送 |
| 年度会员激活 | +500 | 激活码兑换时同一事务赠送 |
| 管理员手动发放 | 不限 | 后台操作，必填备注，写流水可追溯 |

### 6.2 积分消耗渠道

| 渠道 | 消耗 | 规则 |
|------|------|------|
| 诊股 | 1积分/次 | VIP（tier≥2）免费，缓存命中不扣 |
| AI 辅助解读 | 2积分/次 | 全用户统一，同股同日缓存命中不扣 |
| 诊股PDF下载 | 免费 | 纯功能导出，不扣积分 |

### 6.3 VIP 特权汇总

| 特权 | 免费用户 | 月度VIP | 年度VIP |
|------|---------|---------|---------|
| 诊股消耗 | 1积分/次 | **免费** | **免费** |
| AI 解读 | 2积分/次 | 2积分/次 | 2积分/次 |
| 每日签到 | +3 | **+5** | **+5** |
| 竞猜猜对 | +5 | +5 | +5 |
| 竞猜参与 | +1 | +1 | +1 |
| 激活赠送 | — | +100 | +500 |

### 6.4 数据安全设计

- User 表新增 `credits INTEGER DEFAULT 0`，积分与用户数据同行，UPDATE 原子操作
- 新增 `credit_ledger` 表：每笔变动记录 amount/type/ref_id/balance_after/note，不可删改
- 新增 `checkin_records` 表：UNIQUE(user_id, date) 防重复签到
- 新增 `market_guesses` 表：UNIQUE(user_id, guess_date) 防重复竞猜
- 所有积分操作走数据库事务，不进缓存
- 对账接口 `GET /credits/ledger` 用户可查流水

### 6.5 诊股逻辑变更

| | 改前 | 改后 |
|------|------|------|
| 游客 | 每日 2 次 | 登录后 1积分/次 |
| 注册用户 | 每日 3 次 | 1积分/次 |
| VIP/管理员 | 无限 | **免费** |
| 缓存命中 | 不扣次数 | **不扣积分** |
| 缓存提示 | 无 | 顶部条："数据缓存于 X，24h 后失效，建议下载报告保存" |
| PDF下载 | 无 | 免费下载 |

### 6.6 AI 分析设计

- **模型**: DeepSeek-V4-Flash（API 兼容 OpenAI SDK，`base_url=https://api.deepseek.com`）
- **端点**: `POST /api/v1/diagnosis/{stock_code}/ai-analysis`
- **流程**: 校验积分 → 检查同股同日缓存 → 构建 prompt（技术指标JSON + 系统指令）→ 调用 LLM → 扣积分写流水 → 返回分析文字
- **缓存 key**: `ai:{stock_code}:{trade_date}`，同股同一交易日只扣一次积分
- **费用**: 单次约 ~2000 tokens，成本极低（< ¥0.01/次）
- **System prompt**: 定位为"A股短线技术分析师"，解读技术指标 + 看多/看空因素 + T+3~T+7 操作建议 + 风险提示

### 6.7 管理员用户管理模块

在 `admin-trigger.html` 扩展为新后台，新增页签：

**用户列表页面：**
- 表格列：ID / 脱敏手机号 / 等级 / 积分 / 会员到期 / 注册时间 / 操作
- 支持手机号搜索、等级筛选

**用户详情页：**
- 基本信息 + 积分流水（分页）+ 签到日历（30天）+ 诊股统计 + 会员历史

**操作功能：**
- 调整积分（正/负数，必填备注，写流水）
- 调整等级（含到期日设置）
- 重置密码
- 禁用/启用账号（User 表新增 `is_active` 字段）

**统计概览：**
- 总用户/今日新增/本周新增
- 等级分布 + 今日诊股次数/积分消费

### 6.8 实施分阶段

**第一阶段（核心积分+诊股改造）：** ✅ **已完成 (commit: 98d6396)**
- DB 迁移：User 加 credits + is_active + 3张新表
- 注册送积分、激活送积分、签到功能
- 诊股改积分消耗、移除次数限制、缓存提示
- 积分流水查询
- 前端：诊股页改造 + 个人中心积分展示
- 修复 token 过期后 UI 状态不一致 (30s 定时巡检 + clearAndRefresh)
- 登录/注册页 Enter 键触发

**第二阶段（AI+竞猜+用户管理）：**
- AI 分析端点 + DeepSeek 客户端
- 竞猜入口 + 结算（在每日复盘 scheduler 中）
- 管理员用户管理模块（列表+详情+操作+统计）

### 6.9 改动文件总览

| 文件 | 阶段 | 改动 |
|------|------|------|
| `backend/app/models/orm/models.py` | 1 | User 加 credits/is_active；新增 CreditLedger/CheckinRecord/MarketGuess |
| `backend/app/api/auth.py` | 1 | 注册时 +10 积分，写流水 |
| `backend/app/api/membership.py` | 1 | 激活时 +100/+500 积分，写流水 |
| `backend/app/api/diagnosis.py` | 1+2 | 移除次数限制→积分扣减；缓存提示；新增 AI 分析端点 |
| `backend/app/api/credits.py` | 1 | **新建** — 签到、竞猜、积分流水、余额查询 |
| `backend/app/services/ai_analysis.py` | 2 | **新建** — DeepSeek 客户端 + prompt 模板 |
| `backend/app/api/admin.py` | 2 | 用户管理 CRUD、积分调整、等级调整、统计接口 |
| `backend/app/core/settings.py` | 2 | deepseek_api_key + AI/积分配置 |
| `frontend/diagnosis.html` | 1+2 | 移除配额、缓存提示条、AI 解读卡片 |
| `frontend/profile.html` | 1 | 积分余额 + 流水表 + 签到按钮 |
| `frontend/index.html` | 2 | 签到入口 + 竞猜入口 |
| `frontend/admin-trigger.html` | 2 | 改为后台总入口（用户管理 + 数据同步 + 激活码） |
| `frontend/js/app.js` | 1 | Session 增加 credits 字段 |
