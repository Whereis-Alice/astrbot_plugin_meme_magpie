"""表情包保留策略：判断哪些条目不参与容量上限的自动清理。"""

from __future__ import annotations

from typing import Any

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
