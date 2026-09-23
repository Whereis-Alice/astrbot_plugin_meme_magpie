"""表情包保留策略：判断哪些条目不参与容量上限的自动清理。"""

from __future__ import annotations

from typing import Any
import math

# native  = 本机自己收集的图，正常参与容量淘汰
# external= 外部源（表情包/GitHub/JSON 目录）导入的托管副本
# pinned  = 用户手动钉住
# 后两者不参与容量淘汰：整包导入的图被后台悄悄啃掉一半非常难查，
# 而且它们随时能从来源重新同步，没必要抢本机收集的名额。
EXEMPT_RETENTION_CLASSES: frozenset[str] = frozenset({"external", "pinned"})


def retention_class_of(image_info: Any) -> str:
    """读取条目的保留策略；缺字段的老数据一律按 native 处理。"""

    if not isinstance(image_info, dict):
        return "native"
    value = str(image_info.get("retention_class") or "").strip().lower()
    return value or "native"


def is_capacity_exempt(image_info: Any) -> bool:
    """该条目是否豁免容量淘汰。"""

    return retention_class_of(image_info) in EXEMPT_RETENTION_CLASSES


def count_capacity_managed(image_index: dict[str, Any]) -> int:
    """统计参与容量上限的条目数（收藏仍计入，仅外部源/钉住的不计）。"""

    return sum(1 for info in image_index.values() if not is_capacity_exempt(info))


def finite_nonnegative(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def rank_eviction_candidates(
    index: dict[str, Any], *, strategy: str = "oldest", usage_weight: float = 0.7,
) -> list[tuple[str, int]]:
    """Rank only deletable entries; do not change the existing quota policy.

    Weighted normalization and stable tie-breaking are adapted from upstream
    stealer's retention policy (AGPL-3.0). This function never deletes files.
    """
    items = [
        (path, finite_nonnegative(meta.get("use_count")),
         int(finite_nonnegative(meta.get("created_at"))))
        for path, meta in index.items()
        if isinstance(meta, dict) and not meta.get("is_favorite") and not is_capacity_exempt(meta)
    ]
    if not items:
        return []
    if strategy != "weighted":
        items.sort(key=lambda item: (item[2], item[0]))
    else:
        weight = min(1.0, finite_nonnegative(usage_weight, 0.7))
        min_usage = min(item[1] for item in items)
        usage_span = max(item[1] for item in items) - min_usage
        min_created = min(item[2] for item in items)
        age_span = max(item[2] for item in items) - min_created

        def score(item: tuple[str, float, int]) -> tuple:
            path, usage, created = item
            low_usage = 1 - (usage - min_usage) / usage_span if usage_span else 0.0
            age = 1 - (created - min_created) / age_span if age_span else 0.0
            return -(weight * low_usage + (1 - weight) * age), usage, created, path

        items.sort(key=score)
    return [(path, created) for path, _, created in items]
