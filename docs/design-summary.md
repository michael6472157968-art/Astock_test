# Design Summary — Stockwin 短线助手 UI 改造

## 改造日期

2026-08-02

## 设计灵感

QuantDinger（AI量化交易系统）品牌 + Bloomberg/TradingView 暗色金融终端风格

## CSS 变量一览

### 背景色
| 变量 | 暗色值 | 角色 |
|------|--------|------|
| `--color-bg` | `#0a0e17` | 页面背景 |
| `--color-surface` | `#111827` | 区块/弹窗背景 |
| `--color-card` | `#1a1f2e` | 卡片/Header/Footer 背景 |

### 文本色
| 变量 | 暗色值 | 角色 |
|------|--------|------|
| `--color-text` | `#e8ecf1` | 标题、正文 |
| `--color-text-muted` | `#8892a4` | 标签、次要信息 |

### 品牌色
| 变量 | 暗色值 | 角色 |
|------|--------|------|
| `--color-primary` | `#00d4aa` | 按钮、链接、激活态 |
| `--color-primary-hover` | `#00e8bb` | 悬停/加深 |

### 语义色
| 变量 | 暗色值 | 角色 |
|------|--------|------|
| `--color-success` | `#22c55e` | 成功 / A股跌(绿) |
| `--color-danger` | `#ef4444` | 错误 / A股涨(红) |
| `--color-warning` | `#f59e0b` | 警告、中风险 |
| `--color-gold` | `#f0b90b` | VIP/高级点缀 |
| `--color-up` | `#ef4444` | A股上涨(红) |
| `--color-down` | `#22c55e` | A股下跌(绿) |

### 边框、阴影、圆角
| 变量 | 值 |
|------|-----|
| `--color-border` | `#1f2937` |
| `--shadow-sm` | `0 1px 2px rgba(0,0,0,0.4)` |
| `--shadow-md` | `0 4px 12px rgba(0,0,0,0.5)` |
| `--shadow-lg` | `0 8px 24px rgba(0,0,0,0.6)` |
| `--radius-sm` | `4px` |
| `--radius-md` | `8px` |
| `--radius-lg` | `12px` |

### 图表色
| 变量 | 值 | 角色 |
|------|-----|------|
| `--chart-line` | `#00d4aa` | 折线 |
| `--chart-fill` | `rgba(0,212,170,0.12)` | 面积填充 |
| `--chart-grid` | `#1f2937` | 网格线 |

## 改动清单

### 1. `frontend/css/app.css`
- **重写 `:root` 块**：旧变量 `--bg-primary`, `--bg-secondary`, `--bg-card`, `--text-primary`, `--text-secondary`, `--accent`, `--accent-hover`, `--border`, `--success`, `--danger`, `--warning`, `--shadow` 全部废弃
- **新变量命名**：采用 `--color-*` 语义化命名，增加 `--color-surface`, `--color-up`, `--color-down`, `--color-gold`, `--chart-*`, `--radius-*` 等
- **三主题保留**：`[data-theme="dark"]`, `[data-theme="light"]`, `[data-theme="warm"]` 均用新变量名重写
- **深色默认**：无 `data-theme` 时默认使用暗色金融终端配色
- **所有规则**中的 `var(--old-name)` 已全部替换为 `var(--new-name)`

### 2. `frontend/js/app.js` (line 132)
- `renderChrome()` 中动态生成的 header HTML 替换为 SVG Logo + "Stockwin 短线助手"

### 3. 所有 HTML 页面 — Logo 替换
- `frontend/index.html` — `<title>` 更新为 "Stockwin 短线助手"
- `frontend/login.html` — header Logo + title
- `frontend/register.html` — header Logo + title
- `frontend/mobile-login.html` — header Logo + title
- `frontend/profile.html` — header Logo + title
- `frontend/stock-pool.html` — title
- `frontend/diagnosis.html` — title
- `frontend/review.html` — title
- `frontend/sector.html` — title
- `frontend/risk-list.html` — title
- `frontend/alerts.html` — title
- `frontend/backtest.html` — title
- `frontend/admin-trigger.html` — title

### 4. 所有 HTML 页面 — 内联 CSS 变量引用更新
- 所有页面内联 `<style>` 和 JS 字符串中的 `var(--border)`, `var(--bg-card)`, `var(--bg-secondary)`, `var(--text-primary)`, `var(--text-secondary)`, `var(--accent)`, `var(--danger)`, `var(--success)`, `var(--warning)`, `var(--shadow)` 全部替换为新变量名

### 5. SVG Logo 设计
- 4根K线柱（青→蓝渐变 `#00d4aa` → `#0ea5e9`），高低参差
- 1条折线贯穿柱顶，表示趋势
- 文字 "Stockwin 短线助手"（Stockwin 加粗青色，短线助手 常规字重灰色）

## 未改动
- 所有 JS 逻辑、Vue 功能、API 调用未触碰
- 页面布局结构保持原样
- 会员卡渐变颜色保留不变
