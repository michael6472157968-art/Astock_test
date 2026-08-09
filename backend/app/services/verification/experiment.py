"""实验日记 — Purging + Embargo + 实验记录 ORM + CLI 入口。

Purging 公式（来自 backtest_advisor.md）：
  当训练集与测试集相邻时，去掉训练集末尾 `lookback` 天的样本，
  因为因子计算需要 lookback 历史 → 训练集最后 lookback 天的因子值
  包含了测试集前 lookback 天的价格信息。

Embargo：
  训练集结束日 + N 天后才开始测试，N = 持仓周期（forward return 的 N）。

用法：
  python -m app.services.verification.experiment run --pool all
  python -m app.services.verification.experiment list
  python -m app.services.verification.experiment compare exp_001 exp_002
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("experiment")

_BACKEND = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_BACKEND))
_WEIGHTS_PATH = _BACKEND / "data" / "factor_weights.json"
_OUT_DIR = _BACKEND / "data" / "verification_results"


def _config_hash(config: dict) -> str:
    return hashlib.md5(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]


def _lookback_days(config: dict) -> int:
    """根据因子中所需最长历史窗口推算 purge 天数。"""
    fields = config.get("fields", [])
    max_window = 20
    for f in fields:
        if f in ("chg10", "drawdown", "dist_from_low", "vol_ratio", "ma_slope"):
            max_window = max(max_window, 20)
        elif f in ("chg5",):
            max_window = max(max_window, 10)
        elif f in ("chg3", "chg3_recent"):
            max_window = max(max_window, 5)
    return max_window


def purge_split(dates: list[str], train_end: str, test_start: str,
                lookback: int, embargo: int = 1) -> tuple[list[str], list[str]]:
    """对按日期划分的训练/测试集做 Purging。

    - lookback: 因子所需历史窗口，从 train_end 往前去掉 lookback 天
    - embargo: 从 train_end 往后跳 embargo 天再开始测试

    返回 (train_dates, test_dates)
    """
    train_end_idx = dates.index(train_end) if train_end in dates else -1
    test_start_idx = dates.index(test_start) if test_start in dates else -1

    if train_end_idx < 0 or test_start_idx < 0:
        raise ValueError(f"train_end={train_end} or test_start={test_start} not in dates")

    purge_end = train_end_idx - lookback
    train = dates[:max(0, purge_end)]

    embargo_start = train_end_idx + embargo
    test = dates[max(test_start_idx, embargo_start):]

    return train, test


# ── 滚动窗口生成器 ──

def rolling_windows(dates: list[str], train_len: int, test_len: int,
                    lookback: int, embargo: int = 1) -> list[dict]:
    """生成滚动训练/测试窗口拆分。

    返回 [{train_dates, test_dates, purge_end, embargo_start}, ...]
    """
    windows = []
    i = 0
    while i + train_len + test_len <= len(dates):
        train = dates[i:i + train_len]
        test_raw = dates[i + train_len:i + train_len + test_len]

        purge_end_idx = i + train_len - lookback
        train_purged = dates[i:max(i, purge_end_idx)]

        embargo_start = i + train_len + embargo
        test_purged = dates[max(i + train_len, embargo_start):i + train_len + test_len]

        if train_purged and test_purged:
            windows.append({
                "train": train_purged,
                "test": test_purged,
                "purge_end": dates[max(i, purge_end_idx) - 1] if purge_end_idx > i else train[-1],
                "embargo_start": test_purged[0],
            })
        i += test_len
    return windows


# ── 运行完整验证 ──

async def run_experiment(pool_name: str, train_len: int = 80, test_len: int = 20,
                         lookback: int | None = None, sync: bool = False):
    """对一个池跑完整的 IC + 分组回测，带 Purging + Embargo，记录到实验日记。

    使用滚动窗口：train_len 天训练 → test_len 天测试 → 循环。
    """
    from app.core.database import async_session as get_session

    os.makedirs(_OUT_DIR, exist_ok=True)

    with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
        fw = json.load(f)

    pools = list(fw.keys()) if pool_name == "all" else [pool_name]
    unknown = [p for p in pools if p not in fw]
    if unknown:
        logger.error(f"Unknown pools: {unknown}")
        return

    if sync:
        from .ic_analysis import _sync_daily_basic, _sync_stock_daily
        await _sync_daily_basic()
        await _sync_stock_daily(train_len + test_len + 40)

    from app.core.database import async_session as get_session

    async with get_session() as sess:
        r = await sess.execute(text("SELECT MAX(trade_date) FROM stock_daily"))
        last_date = r.scalar()

    logger.info(f"Experiment: pools={pools}, train={train_len}, test={test_len},sync={sync}")
    logger.info(f"DB last trade date: {last_date}")

    t0 = time.perf_counter()
    results = {}

    for p in pools:
        from .ic_analysis import analyze_pool as run_ic
        from .group_backtest import analyze_group as run_group

        ic_result = await run_ic(p, train_len + test_len, sync=False)
        group_result = await run_group(p, n_forward=5, lookback=train_len + test_len, sync=False)

        results[p] = {
            "ic_summary": ic_result.get("summary", []) if ic_result else [],
            "group_monotonic": group_result.get("monotonic") if group_result else None,
            "group_spread": group_result.get("spread", []) if group_result else [],
        }

        # 保存实验日记
        async with get_session() as sess:
            exp = ResearchExperiment(
                experiment_id=f"exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                pool_name=p,
                config_hash=_config_hash(fw[p]),
                date_start=datetime.now().strftime("%Y%m%d"),
                date_end=datetime.now().strftime("%Y%m%d"),
                train_len=train_len,
                test_len=test_len,
                lookback=lookback or _lookback_days(fw[p]),
                ic_summary=json.dumps(results[p]["ic_summary"], ensure_ascii=False),
                group_result=json.dumps(results[p], ensure_ascii=False),
                status="completed",
            )
            sess.add(exp)
            await sess.commit()

        logger.info(f"Experiment saved for {p}")

    elapsed = time.perf_counter() - t0
    logger.info(f"Experiment done: {len(pools)} pools in {elapsed:.1f}s")

    return results


from app.models.orm.models import ResearchExperiment


# ── CLI ──

async def cmd_list():
    from app.core.database import async_session as get_session

    async with get_session() as sess:
        r = await sess.execute(
            select(ResearchExperiment).order_by(ResearchExperiment.created_at.desc()).limit(50)
        )
        exps = r.scalars().all()

    if not exps:
        print("No experiments found. Run 'python -m app.services.verification.experiment run' first.")
        return

    print(f"\n{'ID':<25}{'Pool':<22}{'Status':<12}{'Created'}")
    print("-" * 75)
    for e in exps:
        print(f"{e.experiment_id:<25}{e.pool_name:<22}{e.status:<12}{e.created_at.isoformat() if e.created_at else ''}")


async def cmd_compare(id1: str, id2: str):
    from app.core.database import async_session as get_session

    async with get_session() as sess:
        r1 = await sess.execute(select(ResearchExperiment).where(ResearchExperiment.experiment_id == id1))
        r2 = await sess.execute(select(ResearchExperiment).where(ResearchExperiment.experiment_id == id2))
        e1 = r1.scalars().first()
        e2 = r2.scalars().first()

    if not e1:
        logger.error(f"Experiment not found: {id1}")
        return
    if not e2:
        logger.error(f"Experiment not found: {id2}")
        return

    g1 = json.loads(e1.group_result) if isinstance(e1.group_result, str) else e1.group_result
    g2 = json.loads(e2.group_result) if isinstance(e2.group_result, str) else e2.group_result

    print(f"\n{'='*60}")
    print(f"  Compare: {id1} ({e1.pool_name}) vs {id2} ({e2.pool_name})")
    print(f"{'='*60}")
    print(f"{'Metric':<25}{id1:<18}{id2}")
    print("-" * 60)

    if isinstance(g1, dict) and "group_monotonic" in g1:
        print(f"{'Monotonic':<25}{'YES' if g1['group_monotonic'] else 'NO':<18}{'YES' if g2.get('group_monotonic') else 'NO'}")
    if isinstance(g1, dict) and "group_spread" in g1 and isinstance(g2, dict) and "group_spread" in g2:
        s1 = g1["group_spread"]
        s2 = g2["group_spread"]
        if s1 and s2:
            avg1 = sum(s1) / len(s1) * 100
            avg2 = sum(s2) / len(s2) * 100
            print(f"{'Avg Spread':<25}{avg1:<18.3f}%{avg2:.3f}%")


async def main():
    parser = argparse.ArgumentParser(description="因子验证实验日记")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="运行完整验证实验")
    run_p.add_argument("--pool", default="all")
    run_p.add_argument("--train_len", type=int, default=80)
    run_p.add_argument("--test_len", type=int, default=20)
    run_p.add_argument("--sync", action="store_true")

    sub.add_parser("list", help="列出历史实验")

    cmp_p = sub.add_parser("compare", help="比较两个实验")
    cmp_p.add_argument("exp1")
    cmp_p.add_argument("exp2")

    args = parser.parse_args()

    if args.cmd == "run":
        await run_experiment(args.pool, args.train_len, args.test_len, sync=args.sync)
    elif args.cmd == "list":
        await cmd_list()
    elif args.cmd == "compare":
        await cmd_compare(args.exp1, args.exp2)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
