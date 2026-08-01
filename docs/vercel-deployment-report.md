# A股量化分析助手 — Vercel 部署可行性 & 架构审查报告

> 生成日期: 2026-08-01
> 项目版本: 2.0.0
> 代码规模: 前端 ~550 行 JS/CSS + 后端 ~3,700 行 Python

---

## 一、项目架构概览

| 层 | 技术栈 | 详情 |
|---|--------|------|
| 前端 | 原生 HTML/CSS/JS + CDN 库 | 14 个独立 HTML 页面，无打包工具，无 npm |
| 后端 | FastAPI + SQLite + APScheduler | 单进程，API + 静态资源一体服务 |
| 数据库 | SQLite (83MB) | 本地文件，含股票日线/财务/用户等 15 张表 |
| 数据源 | Tushare Pro SDK | 唯一外部数据依赖，含频率限流 |
| 部署 | systemd + Nginx (传统 VPS) | 现有 `deploy/` 目录为此方案设计 |

前端是一个**经典 MPA（多页应用）**——每个页面是独立的 HTML 文件，通过 `<script src="/js/app.js">` 加载共享的 API 客户端和导航逻辑，3 个页面额外加载 `echarts.js`（1MB CDN 文件）。JS 子目录 `api/`、`components/`、`stores/`、`views/` 全部为空，表明曾规划过 Vue 组件化改造但未实施。

---

## 二、Vercel 部署可行性——核心矛盾

### 2.1 Vercel 不支持长驻进程

当前架构的核心假设是：**一个永不停止的 Python 进程**，在内存中维护 APScheduler 定时器、内存缓存、数据库连接池。启动时执行大量同步逻辑（拉取 Tushare 数据 + 计算选股池/板块/复盘/风险清单）。

Vercel 的 Python 运行时是 **serverless function**：
- 有执行时间上限（Pro 版 60s，Enterprise 300s）
- 空闲后自动回收（cold start）
- 无状态，每次调用是独立的

这意味着 **FastAPI 直接部署到 Vercel 不可行**。

### 2.2 SQLite 无法在 Vercel 上持久化

Vercel 的文件系统是**只读的**（除了 `/tmp`，但 `/tmp` 是临时的，函数回收后清空）。当前 83MB 的 SQLite 数据库存储在进程本地文件系统，在 Vercel 上：
- 无法写入（只读文件系统）
- 即使写入 `/tmp`，也会在函数回收后丢失
- 无法跨函数实例共享

### 2.3 APScheduler 无法运行

收盘后（15:35/15:40）自动触发数据同步和离线计算的定时任务，依赖于 APScheduler 常驻后台线程。这是 Vercel 完全无法支持的场景。

### 2.4 启动逻辑过长

`main.py` 的 `lifespan` 中，启动时同步执行：
1. `sync_stock_basic()` — 拉取股票列表
2. `sync_daily_data()` — 拉取日线数据
3. `sync_historical_daily(days=120)` — 首次安装时拉历史数据
4. `StockPoolEngine().compute_all()` — 选股池计算
5. `SectorAnalysisEngine().compute_all()` — 板块分析
6. `MarketReviewEngine().compute()` — 复盘简报
7. `RiskScanner().scan_risk_list()` — 风险清单

这些同步操作在 Vercel 函数中会直接超时（60s 限制）。

---

## 三、要迁移到 Vercel，需要做什么

如果坚持使用 Vercel，需要以下架构改造（工作量估计 **3-5 周**）：

| 改造项 | 难度 | 说明 |
|--------|------|------|
| 数据库迁到云 DB | 高 | 需替换为 Turso（SQLite 兼容）或 Supabase/PlanetScale（PostgreSQL/MySQL），改 SQLAlchemy 连接 URL 和异步驱动 |
| 定时任务迁到外部 | 中 | 用 Vercel Cron Jobs（Pro 版功能）或 GitHub Actions 替代 APScheduler，每分钟/每小时触发一次 API endpoint |
| 启动逻辑异步化 | 中 | `lifespan` 中的同步阻塞操作需改为后台任务或拆分到 Cron Jobs |
| 前端与后端分离 | 低 | 前端放 Vercel Static，后端 API 作为 Serverless Functions |
| 内存缓存替换 | 低 | 当前 `app/core/cache.py` 的 dict-based 缓存在 serverless 中每次重建，要么接受，要么加 Redis |
| ECharts 优化 | 低 | 1MB 的 `echarts.js` 按需加载可优化，但不阻塞 |

**关键在于数据库**：Turso 是最接近 SQLite 的云方案，但 83MB 数据量会触及免费额度边界。迁移到 PostgreSQL 则需要改 ORM 和部分查询语法。

---

## 四、更务实的替代方案

### 方案 A：Railway / Render / Fly.io（推荐）

这些平台**原生支持 Docker 化长驻服务**，几乎不需要改代码：

- **Railway**: 支持 Dockerfile 部署，自带 PostgreSQL，有 Cron Jobs，免费额度含 $5/月
- **Render**: 支持 Python 服务，有 Cron Jobs，免费层有冷启动但可用
- **Fly.io**: 支持 Docker，全球边缘部署，免费 3 个共享 VM

迁移成本：**1-2 天**（写 Dockerfile + 改数据库 URL 为环境变量 + 部署）。现有的项目结构直接能用。

### 方案 B：Vercel（前端）+ Railway（后端）混合

- 14 个 HTML + CSS + JS → Vercel Static Hosting（零成本，秒级部署）
- FastAPI + SQLite → Railway/Render Docker 服务
- CORS 已配置为 `*`，天然支持跨域

### 方案 C：全栈 Railway / Render

后端+前端一起部署，Nginx 由平台处理。域名配置在平台 UI 中完成，不需要手写 nginx.conf。

---

## 五、架构质量问题

### 5.1 硬编码凭证

`backend/app/core/security.py:96-97` 硬编码了管理员手机号和密码：

```python
admin_phone = "15381971542"
admin_password = "cbw523718"
```

这是严重的安全风险。应通过环境变量或 `.env` 注入。

### 5.2 .env 文件未脱敏

`.gitignore` 已配置 `.env` 被忽略，但 `backend/.env` 实际仍在工作区中。当前 git 状态显示 clean，但需确认 `.env` 是否曾提交过。运行 `git log -- backend/.env` 检查。

### 5.3 无数据库迁移系统

使用 `Base.metadata.create_all()` + 手动 sqlite3 迁移（`database.py:33-68`），没有 Alembic。随着迭代推进，每次改模型都需要手写 sqlite3 SQL，容易出错且不可回滚。

### 5.4 前端无模块化

- `api/`、`components/`、`stores/`、`views/`、`utils/` 五个目录全空
- 所有业务逻辑内联在 14 个 HTML 的 `<script>` 标签中
- 共享逻辑全部在 `app.js` 中（209 行），混入了 API 客户端、会话管理、导航渲染、主题切换
- 样式分散：全局 `app.css`（341 行）+ 每个 HTML 页面的 `<style>` 块
- 版本号用 `?v=5` 手动管理，容易遗漏

### 5.5 启动耦合过高

`main.py` 的 `lifespan` 中启动即执行所有数据同步和计算引擎，导致：
- 启动慢（首次可能数分钟）
- 数据问题会导致整个应用无法启动（虽然有 try/except）
- 无法独立伸缩 API 服务和数据服务

### 5.6 无 package.json / requirements-dev.txt

生产依赖和开发依赖混在一个 `requirements.txt` 中（pytest 系列不应装在生产环境）。

---

## 六、后续更新维护评估

### 优点
- **结构清晰**：后端 API ↔ Service 分层明确，路由按功能模块拆分
- **纯静态前端**：无构建步骤，改了 HTML/JS 直接生效，反馈快
- **本地开发简单**：`start.bat` 一行启动，零配置
- **配置集中**：`settings.py` 是所有配置的唯一入口

### 痛点
- **新增页面**：需要复制 HTML 骨架（header/nav/footer），改写内联 `<script>` 和 `<style>`
- **共享组件修改**：导航栏改一个链接要改全部 14 个 HTML？不——好在导航是通过 `renderNav()` 在 `app.js` 动态生成的，这一点处理得好
- **JS 代码定位**：报错堆栈只显示 `app.js:行号`，业务逻辑混在页面内联脚本中，定位问题困难
- **前端改动验证**：改 CSS 后需要手动刷新每个页面确认没有样式冲突
- **v=5 缓存版本号**：每次改 JS/CSS 要手动更新 `?v=N` 参数，忘记改就会导致用户看到旧版本

### 建议的渐进式改进

| 优先级 | 改进项 | 成本 |
|--------|--------|------|
| P0 | 移除硬编码凭证，改为环境变量 | 5 分钟 |
| P0 | 分离 dev/prod 依赖 | 5 分钟 |
| P1 | 增加 Alembic 数据库迁移 | 2 小时 |
| P1 | 给 HTML 页面引入一个共享的 header/footer 模板片段（可以用后端 Jinja2，或前端简单的 fetch+insert） | 1 小时 |
| P2 | 使用简单打包工具（esbuild 或 vite）实现 JS/CSS 自动 hash 版本化 | 2 小时 |
| P3 | 拆分 app.js 为独立模块 | 4 小时 |

---

## 七、总结

| 维度 | 评价 |
|------|------|
| Vercel 直接部署可行性 | **不可行** — 核心矛盾是长驻进程 + SQLite + 定时任务 |
| 改造后 Vercel 部署可行性 | 可行但成本高 (3-5 周)，需换数据库 + 拆服务 |
| 推荐部署路径 | **Railway / Render / Fly.io** Docker 部署，1-2 天迁移 |
| 代码架构质量 | 后端结构清晰（7/10），前端无模块化（4/10） |
| 后续更新便利性 | 小改动快（改一处即生效），大改动累（无模块化，内联代码多） |
| 安全性 | 有隐患（硬编码凭证），上线前需要修复 |
| 整体评价 | 功能完整的 MVP，适合个人/小团队使用。架构选择（MPA + SQLite + 单进程）在当前阶段是务实的，但限制了云平台的迁移灵活性 |

**一句话结论**：当前架构针对 VPS/Nginx 传统部署设计，不适合直接上 Vercel。建议改用 Railway 或 Render（支持 Docker + 持久存储），迁移成本极低且保留现有架构优势。如果必须用 Vercel，需要搭配 Turso 云数据库 + Vercel Cron Jobs 进行中等规模的改造。
