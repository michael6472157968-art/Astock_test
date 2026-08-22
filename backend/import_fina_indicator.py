"""在 Fly 上导入 fina_indicator_export.sql.gz → fina_indicator 表（配合 export_fina_indicator.py）。

用法（在 Fly 机器上，或本地 cat 管道）:
  cat import_fina_indicator.py | flyctl ssh console -a astock -C "python3 -"
"""
import gzip
import sqlite3
import time

p = '/app/data/fina_indicator_export.sql.gz'
t0 = time.time()

sql = gzip.open(p, 'rt', encoding='utf-8').read()
print(f'decompressed {len(sql)} chars in {round(time.time()-t0, 1)}s')

conn = sqlite3.connect('/app/data/stock_analyzer.db', timeout=30)
conn.execute('PRAGMA busy_timeout=30000')
conn.executescript(sql)
conn.commit()

n = conn.execute('SELECT COUNT(*) FROM fina_indicator').fetchone()[0]
d = conn.execute('SELECT COUNT(DISTINCT ts_code) FROM fina_indicator').fetchone()[0]
latest = conn.execute('SELECT MAX(end_date) FROM fina_indicator').fetchone()[0]
print(f'DONE rows={n} distinct_ts_code={d} latest_end_date={latest} in {round(time.time()-t0, 1)}s')
conn.close()
