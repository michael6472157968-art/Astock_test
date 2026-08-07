---
name: prompt-self-resume
description: "恢复Astock项目工作会话的标准提示词,一键加载最新上下文"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 39c405a3-d6a6-4daa-aca7-3e0f334a2ed9
  modified: 2026-08-08T00:30:00.000Z
---

# newsession 提示词

恢复Astock项目上下文：
1. 阅读 CLAUDE.md + MEMORY.md + memory/project-session-2026-08-07c.md
2. 运行 git status + git log --oneline -12
3. 确认: master与origin同步，最新 `047970c`

## 本轮(2026-08-08)已完成

### 启动全量同步优化 (40ac43a)
- main.py _auto_sync 改为只在 stocks 表为空时执行
- health从2.6s降到1.4ms, refresh从7-10s降到<100ms
- 每日16:05调度器覆盖全部同步+计算,启动不再重复

### 加载蒙版 (047970c)
- index.html: 全屏蒙版 → Logo + 三层反向圆环动画 + 状态轮播文字
- 跟踪5个关键API(/market/index, mood, index_kline, dashboard, calendar)，全部完成后fade-out
- 7s超时强制关闭兜底
- CSS: #appLoadingMask + @keyframes almSpin (app.css)

### 全站Logo替换 (047970c)
- 旧: 四K柱图 + "Stockwin 短线助手" 蓝色文字
- 新: S曲线 + 上涨折线 + "Stock ↗ in" 青绿渐变 + "短线助手"
- SVG: 260×70, linearGradient #00d4aa→#00a8e8
- 影响: app.js(renderChrome header) + login/register/profile/mobile-login.html
- 加载蒙版用almLogoGrad id, header用lg id

### 版本号 + 清理
- CSS/JS版本: v=11→v=12 (所有14个HTML)
- .dockerignore补充: QuantDinger/ admin-dashboard/ demo-proxy/ data/ **/node_modules/ **/.git/
- WAL压缩: 26MB回收
- QuantDinger/目录删除（无git跟踪）

## Logo规范
- 渐变: #00d4aa(左) → #00a8e8(右)
- 文字: Segoe UI, 700 weight, 34px "Stock" + 折线 + "in"
- 副标题: "短线助手", 11px, 500 weight, letter-spacing:6
- 折线: polyline points="112,48 118,30 126,38 134,18 142,28 150,10 154,10", stroke-width:3
- 蒙版用almLogoGrad, header用lg

## 线上待验证
- fly deploy后: 加载蒙版效果 + API响应时间(health应<500ms)
- 交易日历修复(31b853e: get_index_daily替代stock_daily拉上证指数)

## 重要规则
- 改app.js/css必须升所有HTML的?v=(1年缓存)
- commit至本地不push,除非明确要求
- 前端不重启, deny后输出代码
- 线上vs本地差异: stock_daily无000001.SH/399001.SZ等指数, stock_name可能为空

## 后端启动: `cd backend && python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`
## 测试账号: 15381971542 / cbw523718 (管理员, uid=1)
