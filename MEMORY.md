# A股量化分析助手 — 全局记忆索引

> 每次新会话第一步读取本文件。

---

## 强制行为规范（按优先级排序）

| 优先级 | 规则 | 来源 |
|--------|------|------|
| **P0 最高** | 无「执行验证」指令，禁止任何自动测试/curl/pytest/preview/browser 工具调用 | [feedback_no_auto_verification](feedback_no_auto_verification.md) |
| **P0 最高** | 文档/memory 更新后只用文字告知结果，禁止调用 bash echo 做确认信号 | [feedback_no_bash_confirm](feedback_no_bash_confirm.md) |
| P1 | 前端文件修改不重启服务，代码写完直接输出，等待确认 | [feedback_iteration_discipline_v2](feedback_iteration_discipline_v2.md) |
| P1 | 代码输出完成即本轮结束，不主动追加验证流程 | [feedback_task_boundary](feedback_task_boundary.md) |
| P1 | 功能验证一次通过即停止，禁止循环验证 | [feedback_no_repeat_verification](feedback_no_repeat_verification.md) |

## 项目状态

- **项目名称**：A股量化分析助手 (A-Stock Quant Analyzer)
- **当前阶段**：Phase 1 — Web 网页端本地部署版
- **前端方案**：纯静态多页面 HTML + 原生 JS（方案 B）
- **启动方式**：双击 `start.bat`（Python 3.11 唯一外部依赖）
- **根目录**：`D:\Astock_DetaTest\`

## 技术栈

- **前端**：纯静态 HTML + 原生 JavaScript + CSS
- **后端**：Python FastAPI + SQLite (aiosqlite) + 进程内内存缓存 + APScheduler
- **数据源**：Tushare Pro (2000 积分档)

## 关键文件

- [后端入口](backend/app/main.py)
- [配置中心](backend/app/core/settings.py) — Tushare Token 唯一入口
- [Tushare 客户端](backend/app/services/tushare_client.py)
- [选股池引擎](backend/app/services/stock_pool_engine.py)
- [前端入口](frontend/index.html)
- [前端 JS](frontend/js/app.js)

## 后端代码规范

- API 统一 `/api/v1/` 前缀 + 五状态码 + 分页规范
- Tushare Token 唯一入口 `settings.py`，禁止私自修改接口规则
- 频率安全：通用 180次/min + 财务 70次/min
- 缓存优先：先查缓存 → 未命中查 Tushare/DB → 写入缓存
- 合规：每个数据页面必须有风险横幅 + 页脚

## 阶段进度

| 阶段 | 状态 | 内容 |
|------|------|------|
| 1-5 | ✅ 已完成 | 项目初始化 + 后端 + 前端 + 部署 |

## 会话加载流程

新会话第一步：读取本文件 → 了解进度 → 按需读取对应源代码
