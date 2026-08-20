# -*- coding: utf-8 -*-
"""精确版回测: 用真实 limit_list_d 数据(涨停/炸板/连板),复刻 review_rule.py 的情绪周期与操作风格规则,验证次日收益。

复刻的真实规则(review_rule.py step1 + step7):
  情绪周期:
    上升期 = 炸板率<15% 且 (昨日涨停溢价>0 或 未知)
    退潮期 = 炸板率>30% 或 昨日涨停溢价<0
    震荡期 = 其余
  操作风格:
    上升期: j1>=30 → 追2-3板主线龙头; j1<20 → 只做首板/1进2; 其余 → 做主线首板/2板
    震荡期: 低位首板轻仓试错
    退潮期: 空仓观望

本脚本对每个历史交易日,复刻规则给出「该做什么」,再统计该打法的次日实际收益。
"""
import sqlite3
from collections import defaultdict

con = sqlite3.connect('data/stock_analyzer.db')
cur = con.cursor()

# 涨跌停数据: trade_date, ts_code, limit_type, status(连板数)
cur.execute("SELECT trade_date, ts_code, limit_type, status FROM limit_list_records ORDER BY trade_date")
ll = defaultdict(list)
for td, code, lt, st in cur.fetchall():
    ll[td].append((code, lt, st))

# 个股日线: trade_date, ts_code, pct_chg
cur.execute("SELECT trade_date, ts_code, pct_chg FROM stock_daily ORDER BY trade_date")
daily = defaultdict(dict)
for td, code, pct in cur.fetchall():
    daily[td][code] = pct if pct is not None else 0.0

trade_dates = sorted(ll.keys())
# 只保留同时有日线的日期
trade_dates = [td for td in trade_dates if td in daily and len(daily[td]) > 500]
print(f'可用涨跌停交易日: {len(trade_dates)}, 范围 {trade_dates[0]} ~ {trade_dates[-1]}')


def board_num(st):
    try:
        return int(float(st))
    except (ValueError, TypeError):
        return 0


# 预计算每日 ladder(连板分布) + U/D/Z + 每只涨停股的连板数
info = {}
for td in trade_dates:
    u = z = d = 0
    ladder = defaultdict(int)
    u_codes = []
    board_of = {}
    for code, lt, st in ll[td]:
        if lt == 'U':
            u += 1
            u_codes.append(code)
            b = board_num(st)
            board_of[code] = b
            if b >= 1:
                ladder[b] += 1
        elif lt == 'Z':
            z += 1
        elif lt == 'D':
            d += 1
    info[td] = {'U': u, 'D': d, 'Z': z, 'ladder': dict(ladder), 'u_codes': u_codes, 'board_of': board_of}

# 逐日复刻规则 + 统计次日收益
# 打法分组: 追高标(连板>=2), 做低位(首板), 空仓(全市场)
agg = defaultdict(lambda: {'chase': [], 'low': [], 'mkt': [], 'n': 0})

for i in range(len(trade_dates)):
    td = trade_dates[i]
    if i == 0:
        continue
    prev_td = trade_dates[i - 1]
    nxt_td = trade_dates[i + 1] if i + 1 < len(trade_dates) else None
    if nxt_td is None:
        continue

    inf = info[td]
    u, z, d = inf['U'], inf['Z'], inf['D']
    zha_rate = z / (u + z) * 100 if (u + z) else 0

    # 昨日涨停溢价: prev 的 U 股在 td 的 pct_chg 平均
    prev_u = info[prev_td]['u_codes']
    prem_vals = [daily[td][c] for c in prev_u if c in daily[td]]
    avg_premium = sum(prem_vals) / len(prem_vals) if prem_vals else None

    # 复刻情绪周期
    if zha_rate < 15 and (avg_premium is None or avg_premium > 0):
        cycle = '上升期'
    elif zha_rate > 30 or (avg_premium is not None and avg_premium < 0):
        cycle = '退潮期'
    else:
        cycle = '震荡期'

    ladder = inf['ladder']
    j1 = round(ladder.get(2, 0) / ladder.get(1, 0) * 100, 1) if ladder.get(1) else None

    # 复刻操作风格
    if cycle == '上升期':
        if j1 is not None and j1 >= 30:
            style = '追2-3板主线龙头'
        elif j1 is not None and j1 < 20:
            style = '只做首板/1进2'
        else:
            style = '做主线首板/2板'
    elif cycle == '震荡期':
        style = '低位首板轻仓试错'
    else:
        style = '空仓观望'

    # 次日收益
    next_daily = daily[nxt_td]
    board_of = inf['board_of']
    # 追高标: 当日连板股(>=2板) 次日均涨
    chase_codes = [c for c in inf['u_codes'] if board_of.get(c, 0) >= 2]
    chase_vals = [next_daily[c] for c in chase_codes if c in next_daily]
    # 做低位: 当日首板(1板) 次日均涨
    low_codes = [c for c in inf['u_codes'] if board_of.get(c, 0) < 2]
    low_vals = [next_daily[c] for c in low_codes if c in next_daily]
    # 全市场
    mkt_vals = [v for v in next_daily.values()]

    if not chase_vals and not low_vals:
        continue

    a = agg[(cycle, style)]
    a['n'] += 1
    if chase_vals:
        a['chase'].append(sum(chase_vals) / len(chase_vals))
    if low_vals:
        a['low'].append(sum(low_vals) / len(low_vals))
    a['mkt'].append(sum(mkt_vals) / len(mkt_vals))

con.close()

# 输出: 按 操作风格 分组
print('\n================ 复刻你的规则 → 次日实际收益 ================')
print(f'{"情绪周期":<6}{"操作风格(你的规则)":<16}{"样本日":>6}{"追高标次日":>12}{"做低位次日":>12}{"全市场次日":>12}')
for (cycle, style), a in sorted(agg.items()):
    n = a['n']
    chase = sum(a['chase']) / len(a['chase']) if a['chase'] else float('nan')
    low = sum(a['low']) / len(a['low']) if a['low'] else float('nan')
    mkt = sum(a['mkt']) / len(a['mkt']) if a['mkt'] else float('nan')
    cs = f'{chase:+.2f}%' if chase == chase else '--'
    ls = f'{low:+.2f}%' if low == low else '--'
    ms = f'{mkt:+.2f}%' if mkt == mkt else '--'
    print(f'{cycle:<6}{style:<16}{n:>6}{cs:>12}{ls:>12}{ms:>12}')

# 汇总: 周期维度(不分 style)
print('\n================ 按情绪周期汇总 ================')
by_cycle = defaultdict(lambda: {'chase': [], 'low': [], 'mkt': [], 'n': 0})
for (cycle, style), a in agg.items():
    by_cycle[cycle]['n'] += a['n']
    by_cycle[cycle]['chase'].extend(a['chase'])
    by_cycle[cycle]['low'].extend(a['low'])
    by_cycle[cycle]['mkt'].extend(a['mkt'])

print(f'{"周期":<6}{"样本日":>6}{"追高标·连板":>12}{"做低位·首板":>12}{"全市场":>12}{"追高-低位":>10}')
for cycle in ['上升期', '震荡期', '退潮期']:
    b = by_cycle[cycle]
    if not b['n']:
        continue
    chase = sum(b['chase']) / len(b['chase']) if b['chase'] else float('nan')
    low = sum(b['low']) / len(b['low']) if b['low'] else float('nan')
    mkt = sum(b['mkt']) / len(b['mkt']) if b['mkt'] else float('nan')
    diff = chase - low if (chase == chase and low == low) else float('nan')
    cs = f'{chase:+.2f}%' if chase == chase else '--'
    ls = f'{low:+.2f}%' if low == low else '--'
    ms = f'{mkt:+.2f}%' if mkt == mkt else '--'
    ds = f'{diff:+.2f}%' if diff == diff else '--'
    print(f'{cycle:<6}{b["n"]:>6}{cs:>12}{ls:>12}{ms:>12}{ds:>10}')
