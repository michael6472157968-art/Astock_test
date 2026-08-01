# 上线前代码修复 + 全面检查报告

> 日期: 2026-08-01
> 操作: 4 项修复 + 全量审计

---

## 一、已完成的修复

### 1. P0 — 移除硬编码凭证 (security.py)

**问题**: `backend/app/core/security.py` 中管理员手机号和密码直接写死在源码中。

**修复**:
- 管理员凭证改为从环境变量 `ADMIN_SEED_PHONE` / `ADMIN_SEED_PASSWORD` 读取
- 未配置时 `seed_admin()` 静默跳过（不报错）
- `.env.example` 增加了对应的配置说明
- `.env` 中已填入原有值（该文件被 `.gitignore` 排除，不会提交）

**涉及文件**: `backend/app/core/settings.py`, `backend/app/core/security.py`, `backend/.env.example`, `backend/.env`

### 2. P0 — 分离 dev/prod 依赖

**问题**: `requirements.txt` 中 pytest、pytest-asyncio、pytest-cov 在生产环境也会安装。

**修复**:
- 拆分为 `requirements-prod.txt`（12 个运行时依赖）和 `requirements-dev.txt`（4 个开发依赖）
- `start.bat` 已更新引用新文件名

**涉及文件**: `backend/requirements-prod.txt` (新建), `backend/requirements-dev.txt` (新建), `start.bat`

### 3. P1 — 引入 Alembic 数据库迁移

**问题**: 手写 sqlite3 SQL 做迁移 + `create_all()` 建表，不可回滚、无法追溯。

**修复**:
- 安装 Alembic，初始化 `backend/alembic_migrations/`
- `env.py` 自动从 `app.core.settings` 读取数据库 URL
- 补充了 ORM 模型的 `__table_args__` 索引定义（此前仅在手动 migration 中创建）
- 生成了 `fa4d1be23139_initial_schema.py` 作为基线
- `database.py` 中的 `init_db()` 改为 `create_all()` + `alembic upgrade head` 双保险
- 后续改模型只需 `alembic revision --autogenerate -m "xxx"` + `alembic upgrade head`

**涉及文件**: `backend/alembic.ini`, `backend/alembic_migrations/`, `backend/app/core/database.py`, `backend/app/models/orm/models.py`, `backend/requirements-dev.txt`

### 4. P1 — 前端 Chrome 模板复用

**问题**: 14 个 HTML 文件各自硬编码 `<header>`, `<div class="risk-banner">`, `<footer>`，改动一个布局要改 13 个文件。

**修复**:
- `app.js` 新增 `renderChrome()` 函数，DOM 注入 header/banner/footer
- 9 个内容页面的 `<main>` 添加 `data-chrome` 属性，自动装配骨架
- 4 个页面 (login/register/mobile-login/profile) 原有标记不变，保留自定义布局
- 去掉了 ~130 行重复 HTML（13 页 × 约 10 行）
- 把 `echarts.js` 的加载从 diagnosis.html 移到 `<head>`（它原本在 body 最末之前的 script 标签，已正确保留在需要它的 3 个页面中）

**涉及文件**: `frontend/js/app.js`, 9 个 HTML 页面 (`index/stock-pool/diagnosis/review/sector/risk-list/alerts/backtest/admin-trigger`)

---

## 二、全面审计结果

### 安全

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 硬编码凭证 | ✅ 已修复 | 见修复 #1 |
| .env 是否曾提交 git | ✅ 安全 | `git log` 确认未提交 |
| .gitignore 排除 .env | ✅ 正常 | 已配置排除 |
| JWT 密钥强度 | ✅ 正常 | 32 字符随机值 |
| CORS 配置 | ⚠️ 需上线时改 | 当前 `*`，上线后应改为实际域名 |
| bcrypt 密码哈希 | ✅ 正常 | |
| 移动端连接码 | ✅ 正常 | 6 位数字，5 分钟过期，用完即删 |

### 数据库

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 连接配置 | ✅ 正常 | SQLite + aiosqlite，路径从环境变量动态构建 |
| 迁移方案 | ✅ 正常 | Alembic 已配置，基线已在 |
| 索引覆盖 | ✅ 正常 | stock_daily 有 (ts_code, trade_date) 唯一索引 + trade_date 索引 |
| 数据目录 | ✅ 正常 | `data/` 目录在 gitignore 中（*.db 被排除） |

### 后端

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 异常处理 | ✅ 正常 | 统一 AppError + 全局 handler，401/403/404/429/500/502 全覆盖 |
| API 认证 | ✅ 正常 | require_tier 工厂函数控制权限，管理员 tier=99 绕过一切 |
| 诊股配额 | ✅ 正常 | 免费用户 5 次/天，用完返回 403 |
| 缓存策略 | ✅ 正常 | 进程内存缓存 + TTL，选股池/复盘/风险/板块均有缓存 |
| 定时任务 | ✅ 正常 | APScheduler 收盘后 15:35/15:40 自动同步+计算 |
| 启动自检 | ✅ 正常 | 启动自动同步数据，Tushare 不可用时优雅降级 |
| 重复代码 | ⚠️ 轻微 | `_tier_to_label`/`_label_name`/`_mask_phone` 在 auth.py 和 user.py 各重复一套（不影响功能，后续可提取到共享模块） |

### 前端

| 检查项 | 状态 | 说明 |
|--------|------|------|
| Chrome 复用 | ✅ 已修复 | 见修复 #4 |
| API 客户端 | ✅ 正常 | 统一 `API.get/post/put/del`，自动 refresh token |
| 会话管理 | ✅ 正常 | access_token + refresh_token 双 token 模式 |
| 门禁 | ✅ 正常 | Gate.checkPage 根据 tier 拦截 |
| 主题支持 | ✅ 正常 | light/dark/warm 三主题 |
| 移动端响应 | ⚠️ 部分 | 大部分页面无响应式，仅有 mobile-login.html 做了适配 |
| echarts.js | ⚠️ 较大 | 1MB，3 个页面加载，可考虑 CDN 引用或按需拆分 |
| v=N 缓存版本 | ⚠️ 手动 | JS/CSS 版本号需手动更新（目前 v=5 或 v=2/v=3），遗忘会导致用户看到缓存旧版本 |
| 内联 JS | ⚠️ 可改进 | 所有页面业务逻辑在 `<script>` 中，无模块化，但不影响功能 |

### 基础设施

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 启动脚本 | ✅ 正常 | start.bat 已更新引用 |
| 依赖安装 | ✅ 正常 | 纯 pip，无系统级依赖 |
| Python 版本 | ✅ 正常 | 3.11+（使用了 `from __future__ import annotations` 等特性） |

---

## 三、建议上线前自检清单

按优先级排列，建议在上线前逐项确认：

- [ ] **CORS_ORIGINS**: `backend/.env` 中改为实际域名（如 `https://astock.yourdomain.com`）
- [ ] **DEBUG**: `backend/.env` 中改为 `false`
- [ ] **Tushare Token**: 确认 Token 有效且额度充足（余额查询: https://tushare.pro）
- [ ] **管理员密码**: 确认 `ADMIN_SEED_PASSWORD` 已改为安全密码（当前为开发测试密码 `cbw523718`）
- [ ] **域名**: 确认已有域名并完成 DNS 解析
- [ ] **SSL**: 上线后为自定义域名申请证书（Let's Encrypt / Cloudflare 均可）
- [ ] **v= 版本号**: JS `app.js` 的 `v=5` 和 CSS 的 `v=2`/`v=3` 上线前统一为 `v=1` 或改为自动 hash
- [ ] **ECharts 体积**: 1MB 库文件考虑用 CDN（`https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js`）替代本地文件
- [ ] **数据库备份**: 上线前备份 `backend/data/stock_analyzer.db`（83MB，包含历史日线数据）
- [ ] **健康检查**: 启动后访问 `/api/v1/health` 确认响应正常

---

## 四、结论

4 项代码修复全部完成，无阻塞性问题。应用整体结构清晰、安全基础扎实、API 设计合理。前端非模块化和响应式缺失是**工程债务**，不影响上线运行，可在后续迭代中逐步改进。

**可以上线。** 下一步：新 session 中写 Dockerfile + 部署到 Fly.io。
