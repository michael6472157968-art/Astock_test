# CLAUDE.md — 项目上下文保护

## 禁止读取的文件

以下文件永远不要通过 Read/Glob 工具读取其内容，它们会严重浪费上下文窗口：

- `frontend/lib/*.js` — vendor CDN 库 (ECharts 1MB, Vue 164KB)，只在引用路径时需要
- `backend/data/stock_analyzer.db` — 15MB SQLite 数据库，查询用 SQL 不是读文件
- `backend/data/user_data/**/*.json` — 用户数据，需要时由后端代码处理
- `backend/__pycache__/**`、`backend/**/__pycache__/**` — 构建产物

## 上下文管理策略

- 修改前端时只读具体 HTML 文件，不读 lib 文件
- 后端调试时通过代码分析而非直接读数据库文件
- 排查 API 问题优先读路由代码和 service 代码，不读 ORM models 全量

## 项目关键入口（按需读取）

| 关注点 | 文件 |
|--------|------|
| 全局前端入口 + API 客户端 | `frontend/js/app.js` |
| 全局样式 | `frontend/css/app.css` |
| 后端入口 | `backend/app/main.py` |
| 配置 | `backend/app/core/settings.py` |
| 路由 | `backend/app/api/*.py` |
| 业务逻辑 | `backend/app/services/*.py` |
| 数据模型 | `backend/app/models/orm/models.py` |
