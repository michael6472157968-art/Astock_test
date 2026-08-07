---
name: prompt-self-resume
description: "恢复Astock项目工作会话的标准提示词,一键加载最新上下文"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 39c405a3-d6a6-4daa-aca7-3e0f334a2ed9
  modified: 2026-08-07T13:29:36.585Z
---

# newsession 提示词

恢复Astock项目上下文：
1. 阅读 CLAUDE.md + MEMORY.md + memory/project-session-2026-08-07b.md
2. 运行 git status + git log origin/master..HEAD --oneline
3. 确认: master领先origin 55 commits，最新 `7a38b17`
4. 工作目录干净

## 本轮(2026-08-07下半场)成果

### 游客模糊覆盖层 (3db98c5)
- app.js needAuth两处移除alerts.html，游客不再被重定向
- alerts.html: 硬门控改为renderGuestAlertsPage() → 模糊分组+锁覆盖层+"立即登录"CTA
- renderMockFavContent()生成模拟分组卡片，fav-blur-wrapper包裹

### obs-btn门控恢复 (3db98c5)
- f08e798被6a9eb58回退，重新在group-obs-btn和fav-obs-btn加canViewDashboard门控

### 第三方文件清理 (09a5407)
- QuantDinger(740文件)/admin-dashboard/demo-proxy/data从git索引移除
- .gitignore更新防止再次跟踪

### 自选速览面板始终显示 (7a38b17)
- 移除groupInfos为空时的display:none提前return
- 分组观测按钮改为toggle模式(添加/取消)，多分组可同时观测
- FavStore新增getObserveGroupIds/setObserveGroupIds
- 无观测分组时默认显示所有分组(hasObs兜底)
- 个股涨跌幅不再自动填充5只股票
- switchFavTab切换时不再隐藏空面板

## 待讨论
- 游客访问alerts.html流程(已改为模糊覆盖层，已完成)
- 教学弹窗拖动编辑模式脚本(无需落地文件，已完成)

## 后端启动: `cd backend && python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`
## 测试账号: 15381971542 / cbw523718 (管理员)
## 行为规则: commit至本地不push, 前端不重启, 命令被deny后输出代码
