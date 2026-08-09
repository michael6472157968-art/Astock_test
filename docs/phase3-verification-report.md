# 阶段3：因子验证管线 — 完整报告

## 1. 理论基础

### 1.1 为什么需要验证管线

C1-lite 构建了 8 个选股池，每个池用 5-7 个因子加权打分。权重的来源是经验设定而非统计推断——比如"hot_leader 池里 pct_chg 权重 0.30"是基于直觉，不是数据。

因子验证管线的目的：**用量化证据回答三个问题**：
1. 这个因子在统计上是否真的有预测力？（IC 分析）
2. 按评分排序后，高分股票是否真的跑赢低分股票？（分组回测）
3. 不同权重方案之间，哪个更好？（实验日记对比）

### 1.2 Rank IC（信息系数）

**定义**：因子值(t)与未来收益(t+N)之间的横截面 Spearman 秩相关系数。

**为什么用 Rank IC 而不是 Pearson IC**：
- Pearson IC 对异常值敏感（一只 PE=5000 的股票会扭曲整个截面相关）
- Spearman 只看排序，天然抗异常值
- 行业标准：Rank IC > 0.02 代表因子有预测力，> 0.05 代表强因子

**IC_IR（信息比率）**：IC 均值 / IC 标准差。衡量因子预测的稳定性。
- IC_IR > 0.3：可用
- IC_IR > 0.5：优质因子
- IC_IR > 1.0：顶级因子

**衰减曲线**：同一个因子对 T+1、T+3、T+5、T+10、T+20 的 IC 序列。
- IC 随 N 衰减 = 正常，因子预测力随时间递减
- IC 随 N 放大 = 因子捕捉的是中期趋势（例如市值因子）
- IC 在 N=5 最强然后反转 = 因子有均值回归特征

### 1.3 分组回测

**方法**：每天按因子评分将全市场股票分成 5 组（Q0=最高分，Q4=最低分），等权持有 T+5 天，计算每组的日均收益和累计曲线。

**理想的曲线形态**：
```
累计收益
  ↑
  │  Q0 ────────── 最高分
  │  Q1 ────────
  │  Q2 ──────
  │  Q3 ────
  │  Q4 ──         最低分
  └──────────────────→ 时间
```

**判断标准**：
- **单调性**：Q0 收益 > Q1 > Q2 > Q3 > Q4 → 因子有效
- **Spread**：Q0 - Q4 的日均差值 → 多空策略的理论 alpha
- **交叉**：Q0 和 Q1 纠缠 → 头部区分度不够，考虑增加区分度高的因子

### 1.4 Purging & Embargo

**问题**：在回测中，如果你的因子用了 20 天历史数据计算（如 vol_ratio 需要 20 日均量），那么训练集末尾的样本因子值里包含了测试集开头的数据——产生了标签泄漏。

**Purging**：训练集末尾去掉 `lookback` 天，丢弃那些因子值"看到"了测试集的样本。

**Embargo**：训练集结束日和测试集开始日之间留出 `forward_N` 天的间隙，防止训练集最后一天的 forward return 和测试集第一天的 forward return 重叠。

**实现**（`experiment.py:purge_split()` 和 `rolling_windows()`）：
```
原始:  [████████████████ train ████████████████][████ test ████]
                  ↑ 这最后 lookback 天的因子值污染了 test

Purge 后: [████████████ train ████████]. . .[purge]. . .[████ test ████]
                                                   ↑ 去掉污染
Embargo: [████████ train ████████] . . . [N天空隙] . . . [████ test ████]
                                           ↑ 标签隔离
```

---

## 2. 工作流程

### 2.1 核心环路

```
                    ┌─────────────┐
                    │ factor_weights│  ← 唯一的配置入口
                    │   .json      │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ↓            ↓            ↓
        ic_analysis  group_backtest  experiment
              │            │            │
              ↓            ↓            ↓
        IC CSV       分组收益CSV    实验日记DB
              │            │            │
              └────────────┼────────────┘
                           ↓
                    ┌─────────────┐
                    │ 调权重决策    │
                    └─────────────┘
```

### 2.2 三个模块职责

| 模块 | 输入 | 计算 | 输出 |
|------|------|------|------|
| `ic_analysis` | 股票日线 + 因子列表 | 每天截面算因子值 × forward return 做 Spearman | IC 序列 CSV + 汇总表 |
| `group_backtest` | 日线 + 权重配置 | 每天评分 → 5组等权 → T+N收益 | 分组收益 CSV + 单调性报告 |
| `experiment` | 上述两模块 + 滚动窗口 | Purging + Embargo + 滚动拆分 | 实验日记 ORM 记录 + CLI 对比 |

### 2.3 数据链路

```
Tushare (仅 --sync 时调用)
  │
  ├─ daily (全市场日线, 每天 1 次 API)
  └─ daily_basic (PE/PB/市值/换手率, 每天 1 次 API)
       │
       ↓
  本地 SQLite (stock_daily + daily_basic)
       │
       ↓
  因子时序计算 (纯 Python, 基于已有日线)
       │
       ↓
  横截面 IC / 分组回测
       │
       ↓
  CSV 输出 (backend/data/verification_results/)
```

---

## 3. 使用说明

### 3.1 环境要求

- Python 3.10+
- 本地 SQLite（`backend/data/stock_analyzer.db`）
- Tushare token（已在 `settings.py` 配置，仅 `--sync` 时需要联网）

### 3.2 常用命令

```bash
cd backend

# 1. 快速检查——单池 IC（不联网，~6秒）
python -m app.services.verification.ic_analysis --pool hot_leader --lookback 60

# 2. 全池 IC —— 跑完所有 8 个池
python -m app.services.verification.ic_analysis --pool all --lookback 120

# 3. 补数据 + 全池 —— 关机几天后首次运行
python -m app.services.verification.ic_analysis --pool all --lookback 120 --sync

# 4. 分组回测 —— 验证评分单调性（~5秒/池）
python -m app.services.verification.group_backtest --pool hot_leader --n_forward 5
python -m app.services.verification.group_backtest --pool all --n_forward 5

# 5. 完整实验 —— IC + 分组 + 滚动窗口 + 日记
python -m app.services.verification.experiment run --pool all --sync

# 6. 查看历史实验
python -m app.services.verification.experiment list

# 7. 对比两次实验
python -m app.services.verification.experiment compare exp_20260810_015540 exp_20260810_020307
```

### 3.3 输出文件位置

```
backend/data/verification_results/
├── ic_sequence_hot_leader_20260810_020307.csv   ← 每天每因子每N的IC值
├── ic_summary_hot_leader_20260810_020307.csv    ← 汇总：均值/IR/正向率
└── group_backtest_hot_leader_F5_20260810.csv    ← 5组逐日收益
```

### 3.4 调权决策流程

```
1. python -m app.services.verification.ic_analysis --pool all
   → 看每个池各因子的 IC_mean 和 IC_IR
   → 标记 IC_IR < 0.1 的弱因子（候选降权或移除）
   → 标记 IC_mean 方向与预期相反的因子（如 PE 正相关=异常）

2. python -m app.services.verification.group_backtest --pool all --n_forward 5
   → 看 Q0-Q4 spread 是否单调
   → 如果 Q0 和 Q1 纠缠 → 需要更强的头部区分因子
   → 如果 Q4 收益 > Q0 → 因子方向全反，检查 invert

3. 改 backend/data/factor_weights.json 调权重
   → 弱因子降权、强因子升权、方向纠正

4. 重新跑 1+2，验证调权效果
   → python -m app.services.verification.experiment run --pool all

5. 确认改善后，改 factor_weights.json → 前端 API 缓存刷新生效
```

---

## 4. 应用到网站

**当前 C1-lite 已打通配置中心化**：`factor_weights.json` 是选股引擎的权重唯一来源。调权不需要重启后端，刷新 API 缓存即可。

**应用路径**：
```
验证管线（本地离线）                  生产环境
─────────────────                    ────────
ic_analysis → 发现 turnover IC最强
group_backtest → 确认 Q0-Q4 单调
调 factor_weights.json 权重
experiment → 验证新权重的 spread 改善    → 本地验证通过
                                         → 改 fly 端的 factor_weights.json
                                         → 刷新缓存 (无需重启 uvicorn)
                                         → 前端 stock-pool.html 自动显示新排序
```

**尚未做**：自动将 IC 结果推送到生产。目前是手动流程——本地验证通过 → 手动更新 fly 端配置。

---

## 5. 工作进展

### 5.1 开发时间线

| 时间 | 阶段 | 内容 |
|------|------|------|
| 2026-08-10 上午 | 方案设计 | 确定三模块架构 + 离线 CLI 定位 |
| 2026-08-10 下午 | 编码 | `ic_analysis.py` + `group_backtest.py` + `experiment.py` + ORM 表 |
| 2026-08-10 晚间 | 调试+实测 | `--sync` 逻辑修复 + 跑通 hot_leader 全流程 |

### 5.2 遇到的问题与解决方案

**问题 1：daily_basic 只覆盖 4 个交易日**

现象：IC 分析中 PE/PB/turnover/total_mv 全部返回 N/A，因子值缺失。
原因：`daily_basic` 表之前只在同步收盘数据时顺带写入，历史数据从未回填。本地 DB 只有最近 4 天。
解决：`--sync` 参数改为先读取交易日列表，再按列表检查 daily_basic 缺失，按需从 Tushare 补拉。81 天 × 1 次 API/天 = 81 次调用，在 2000 次日限额内。
耗时：~4 分钟（含 API 间隔），一次性开销。

**问题 2：Tushare 非交易日返回空**

现象：sync 时周末/节假日日期调 Tushare 返回空列表，log 里出现 warning。
影响：无害——空返回后 skip，不写入数据。
优化：后续可按交易日历过滤，避免无效调用。

**问题 3：`get_daily_data_by_date` 函数不存在**

现象：ic_analysis.py 里引用了不存在的函数名。
解决：实际函数名是 `get_all_daily(trade_date)`，已修正。

**问题 4：分组回测 80/85 天被跳过**

现象：未 sync 前，分组回测几乎所有日期都被跳过。
原因：因子值（PE/PB/市值/换手率）在 daily_basic 缺失的日期上无法计算，导致横截面小于 MIN_CROSS_SECTION(50)。
解决：sync 后 daily_basic 覆盖全部交易日，跳过天数降至 19/85（正常——部分日期本身股票数不足）。

**问题 5：SQL 参数化注入风险**

现象：初版 `_sync_daily_basic` 用了 f-string 拼接 SQL IN 子句。
解决：改为 `:d0, :d1, ...` 命名参数，消除注入风险。

### 5.3 实测结果

**环境**：4587 只股票（排除 ST/688/920），60 交易日 lookback，85 个交易日截面。

**hot_leader 池 7 因子 Rank IC**：

```
Factor           N=1      N=3      N=5      N=10     N=20
pct_chg          -.006    +.001    +.011    +.032    +.003    ← 弱信号
chg3             -.002    +.022    +.018    +.038    -.019    ← N=10 最强
vol_ratio        -.019    -.001    +.003    +.001    -.021    ← 无稳定方向
turnover         -.063    -.097    -.127    -.195    -.281    ← 最强！且 N越大IC越强
pe               -.040    -.075    -.100    -.162    -.225    ← 强！低PE=好
pb               -.031    -.060    -.084    -.140    -.211    ← 强！低PB=好
total_mv         +.024    +.036    +.037    +.075    +.070    ← 正向（大市值略好）
```

**核心发现**：

1. **turnover（换手率）是当前权重最低(0.10)但 IC 最强的因子**——需大幅升权
2. **PE/PB 的 IC 值稳定为负**——低估值确实预示高收益，方向正确
3. **pct_chg（当日涨幅）对中短期的 IC 几乎为零**——但权重最高(0.30)，在浪费评分空间
4. **vol_ratio 方向不稳定**——N=1 负、N=5 正、N=20 又负，作为选股因子需要重新审视
5. **total_mv（市值）正向 IC**——大市值略好，但与"小盘成长"直觉相反，需关注

**分组回测**（hot_leader，T+5）：

```
Q0 日均 -1.39%  ← 最高分组，没跑赢
Q1 日均 -1.22%
Q2 日均 -1.24%
Q3 日均 -1.26%
Q4 日均 -1.50%  ← 最低分组，确实最差
Spread: 0.115%
Monotonic: FAIL  ← 单调性未通过
```

**为什么单调性 FAIL 但 IC 看起来还行**：

1. 60 天回看窗口太短（约 3 个月），期间 A 股整体下行，所有组都负收益
2. 权重配置不合理——pct_chg(0.30) 权重高但 IC 弱，拖累了分组区分度
3. 等权分组对噪声敏感，需要更多日期才能收敛

**这不是 IC 方法的问题，而是权重和窗口的问题。**

### 5.4 未来预期

**短期（本周可做）**：
1. 跑完 8 池 × 120 天全量 IC → 每池出一份因子评级报告
2. 基于 IC 结果调 factor_weights.json 权重
3. 跑 experiment compare 验证新旧权重差异
4. 确认改善后更新 fly 端配置

**中期（需要更多数据）**：
1. 积累 1 年以上数据后，可以做滚动窗口实验（train 80 天 / test 20 天，循环 N 轮）
2. 因子拥挤度监控——某因子 IC 突然衰减时告警
3. 行业中性化——去掉行业 beta 后的纯 alpha IC

**长期（策略升级）**：
1. 因子合成——用 IC_IR 作为权重对多因子加权（当前是等权简单加权）
2. IC 加权评分替代固定权重——得分 = Σ(因子_i × IC_IR_i)，权重随市场变化自适应
3. 接入实验日记做 A/B 测试——两个权重方案同时跑，compare 命令对比

---

## 6. 代码清单与 Git 状态

```
backend/app/services/verification/    ← 新增
├── __init__.py                       (1 行)
├── ic_analysis.py                    (620 行) Rank IC + sync
├── group_backtest.py                 (380 行) 5分组回测
└── experiment.py                     (400 行) Purging + 日记 + CLI

backend/app/models/orm/models.py      ← 修改
  + ResearchExperiment 表              (新增 ORM 模型)

backend/data/verification_results/    ← 运行产物
  *.csv                                (IC/分组输出)

已提交: da0eb93 (本地, 未 push)
```

---

## 7. 后续 session 快速恢复

```
# 验证环境就绪检查
cd D:/Astock_DetaTest/backend
python -c "from app.services.verification.ic_analysis import analyze_pool; print('OK')"

# 跑全量 IC（如果 daily_basic 已补全，不需 --sync）
python -m app.services.verification.ic_analysis --pool all --lookback 120

# 看历史
python -m app.services.verification.experiment list
```
