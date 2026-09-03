# 表情包喜鹊（astrbot_plugin_meme_magpie）包初始化文件

from __future__ import annotations

from typing import Any

__all__ = ["Main"]


def __getattr__(name: str) -> Any:
    if name == "Main":
        from .main import Main

        return Main
    raise AttributeError(name)
