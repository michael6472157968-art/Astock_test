"""因子库元信息 — 配置驱动。

因子元信息全部在 data/factor_meta.json，本模块只读配置并提供访问函数。
用户新增/修改因子：直接改 factor_meta.json，各功能(展示/匹配/诊断/选股)自动应用，无需改代码。
"""
from __future__ import annotations

import json
from pathlib import Path

_META_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "factor_meta.json"

# 维度展示顺序
_DIM_ORDER = ["价格", "量", "基本面", "量(择时)"]


def load_factors() -> dict[str, dict]:
    """读取全部因子元信息，返回 {key: meta}。"""
    with open(_META_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def list_factors_grouped() -> dict[str, list[dict]]:
    """按维度分组返回因子列表，供前端因子库卡片展示。"""
    factors = load_factors()
    grouped: dict[str, list[dict]] = {}
    for key, meta in factors.items():
        grouped.setdefault(meta["dim"], []).append({
            "key": key,
            "code": meta["code"],
            "name": meta["name"],
            "dim": meta["dim"],
            "signal": meta["signal"],
            "ic": meta["ic"],
            "perf": meta["perf"],
            "env": meta["env"],
            "desc": meta["desc"],
        })
    ordered: dict[str, list[dict]] = {}
    for dim in _DIM_ORDER:
        if dim in grouped:
            ordered[dim] = grouped[dim]
    for dim in sorted(grouped):
        if dim not in ordered:
            ordered[dim] = grouped[dim]
    return ordered
