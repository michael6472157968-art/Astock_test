# -*- coding: utf-8 -*-
"""周期转移矩阵: 今日情绪周期 → 明日情绪周期 的历史概率 + 各转移的次日打板收益。

用途: 支撑「三剧本推演」(强延续/弱分歧/强退潮)的历史概率与收益背书。
复刻 review_rule.py 的情绪周期分类(炸板率 + 昨日涨停溢价)。
"""
import sqlite3
from collections import defaultdict

con = sqlite3.connect('data/stock_analyzer.db')
cur = con.cursor()

cur.execute("SELECT trade_date, ts_code, limit_type, status FROM limit_list_records ORDER BY trade_date")
ll = defaultdict(list)
for td, code, lt, st in cur.fetchall():
    ll[td].append((code, lt, st))

cur.execute("SELECT trade_date, ts_code, pct_chg FROM stock_daily ORDER BY trade_date")
daily = defaultdict(dict)
for td, code, pct in cur.fetchall():
    daily[td][code] = pct if pct is not None else 0.0

tds = sorted(td for td in ll if td in daily and len(daily[td]) > 500)


def bn(st):
    try:
        return int(float(st))
    except (ValueError, TypeError):
        return 0


info = {}
for td in tds:
    u = z = 0
    board = {}
    uc = []
    for c, lt, st in ll[td]:
        if lt == 'U':
            u += 1
            uc.append(c)
            board[c] = bn(st)
        elif lt == 'Z':
            z += 1
    info[td] = (u, z, board, uc)


def classify(td):
    u, z, board, uc = info[td]
    zr = z / (u + z) * 100 if u + z else 0
    # 昨日涨停溢价需要 prev
    idx = tds.index(td)
    prev = tds[idx - 1] if idx > 0 else None
    prem = None
    if prev:
        pv = [daily[td][c] for c in info[prev][3] if c in daily[td]]
        prem = sum(pv) / len(pv) if pv else None
    if zr < 15 and (prem is None or prem > 0):
        return '上升期'
    if zr > 30 or (prem is not None and prem < 0):
        return '退潮期'
    return '震荡期'


# 预分类所有日期的周期
cycles = {td: classify(td) for td in tds}

# 转移矩阵 + 各转移次日收益
trans = defaultdict(lambda: {'n': 0, 'chase': [], 'low': []})
for i in range(len(tds) - 1):
    td = tds[i]
    nxt = tds[i + 1]
    c_today = cycles[td]
    c_next = cycles[nxt]
    u, z, board, uc = info[td]
    next_daily = daily[nxt]
    chase = [next_daily[c] for c in uc if board.get(c, 0) >= 2 and c in next_daily]
    low = [next_daily[c] for c in uc if board.get(c, 0) < 2 and c in next_daily]
    t = trans[(c_today, c_next)]
    t['n'] += 1
    if chase:
        t['chase'].append(sum(chase) / len(chase))
    if low:
        t['low'].append(sum(low) / len(low))

con.close()

order = ['上升期', '震荡期', '退潮期']
print('========== 周期转移矩阵: 今日 → 明日 ==========')
print(f'{"今日周期":<6}{"明日周期":<6}{"天数":>6}{"概率":>8}{"追高标次日":>12}{"做低位次日":>12}')
for c_today in order:
    row_total = sum(trans[(c_today, c_n)]['n'] for c_n in order)
    for c_next in order:
        t = trans[(c_today, c_next)]
        if t['n'] == 0:
            continue
        prob = t['n'] / row_total * 100
        chase = sum(t['chase']) / len(t['chase']) if t['chase'] else float('nan')
        low = sum(t['low']) / len(t['low']) if t['low'] else float('nan')
        cs = f'{chase:+.2f}%' if chase == chase else '--'
        ls = f'{low:+.2f}%' if low == low else '--'
        print(f'{c_today:<6}{c_next:<6}{t["n"]:>6}{prob:>7.1f}%{cs:>12}{ls:>12}')
    print()
