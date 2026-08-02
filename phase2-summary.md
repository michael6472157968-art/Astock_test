# Phase 2 Summary — 股票代码兼容 + 诊股信号 Tooltip + 诊股按钮动画

## 改动文件

### 1. `frontend/js/app.js` — 新增全局工具函数

**`normalizeStockCode(raw)`** (第 125–135 行)
- 过滤空格和 Unicode 控制字符，清除特殊符号，转大写
- 纯数字 6 位代码自动补后缀：`60xxxx` / `68xxxx` → `.SH`，`00xxxx` / `30xxxx` → `.SZ`
- 带后缀代码（如 `603091.SH`）直接返回不做转换
- 输入 `603091` → `603091.SH`，输入 `000001` → `000001.SZ`，输入 `603091.SH` → `603091.SH`

**`filterStockInput(val)`** (第 138–140 行)
- 实时过滤输入：只保留字母、数字、点号，去掉空格和特殊字符
- 绑定在输入框的 `oninput` 事件上

### 2. `frontend/diagnosis.html` — 三项改动

**股票代码输入（第 55 行）：**
- `placeholder` 改为 `"输入股票代码，如 603091 或 000001.SZ"`
- 新增 `oninput` 调用 `filterStockInput()` 实时过滤
- `search()` 使用 `normalizeStockCode()` 替代 `.trim()`

**量化信号 Tooltip（第 16–32 行 CSS + 第 239–250 行 JS）：**
- CSS: `.signal-tooltip-wrap` 相对定位容器 + `.signal-tooltip` 绝对定位气泡
  - 深色背景 `#1e293b`，白色文字 `#f1f5f9`，圆角 `6px`
  - `transition: opacity 0.2s ease 0.2s` — 200ms 延迟后才出现
  - 带三角箭头（`::after` 伪元素）
- JS: `SIGNAL_DESC` 对象定义四类信号说明文案（成交量/均线趋势/超买超卖/布林带）
- JS: `signalTag(group, text)` 函数输出带 tooltip 包裹的信号标签
- 信号区域标题提示 "（鼠标悬浮查看信号说明）"

**诊股进度条动画（第 34–46 行 CSS + 第 88–155 行 JS）：**
- CSS: `.progress-loader` 居中布局，`.progress-bar-track` 深色轨道 6px 高，`.progress-bar-fill` 渐变填充条
- 错误态 `.progress-bar-fill.error` 变红色渐变
- `showProgress()` — 渲染进度条 UI，文案 "正在结合量化策略分析..."
- `startProgress()` — 每 200ms 更新进度，用衰减公式逼近 90% 后保持
- `finishProgress(status)` — 成功时跳到 100% 并切换渲染；失败时进度条变红 + 显示 "分析失败，请重试"
- 渲染逻辑抽取为 `renderDiagnosis(r, code)` 独立函数

### 3. `frontend/backtest.html` — 股票代码兼容

- 输入框新增 `oninput="this.value=filterStockInput(this.value)"` 实时过滤
- `run()` 函数使用 `normalizeStockCode()` 替代 `.trim()`

## 测试验证清单

| 测试项 | 预期结果 |
|--------|----------|
| 诊断页输入 `603091` 点击诊股 | 自动转为 `603091.SH` 并查询成功 |
| 诊断页输入 `603091.SH` 点击诊股 | 直接使用 `603091.SH` 查询成功 |
| 诊断页输入 `000001` 点击诊股 | 自动转为 `000001.SZ` |
| 回测页输入 `603091` 点回测 | 自动转为 `603091.SH` |
| 输入带空格 `603 091` | 空格被实时过滤，最终为 `603091.SH` |
| 诊股信号标签鼠标悬浮 | 显示对应策略的 tooltip 说明 |
| 诊股按钮点击后 | 进度条从 0% 动画走到 ~90%，加载完成跳 100% 显示结果 |
| 诊股查询失败 | 进度条变红色，显示 "分析失败，请重试" |
