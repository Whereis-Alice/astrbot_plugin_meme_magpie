"""`mp help` 指令的渲染测试。

这条指令是给"记不住自己唤醒前缀"的用户兜底的，所以关键约束只有一条：
清单里的命令示例必须跟着当前会话真实生效的前缀走，前缀被清空时不能凭空
多出一个 `/`（那会变成一条在用户机器上根本不存在的命令）。
"""

import asyncio

from core.commands.command_handler import CommandHandler
from core.util.command_hint import format_command


class _Event:
    """最小 AstrMessageEvent stub：`show_help` 只用到 `plain_result`。"""

    unified_msg_origin = "aiocqhttp:GroupMessage:123"

    @staticmethod
    def plain_result(text):
        return text


class _Plugin:
    """最小 Main stub：`show_help` 只用到 `wake_prefix` 与 `cmd`。"""

    def __init__(self, prefix="/"):
        self._prefix = prefix

    def wake_prefix(self, event=None):
        return self._prefix

    def cmd(self, sub="", event=None):
        return format_command(sub, self._prefix)


def _render(prefix: str) -> str:
    async def run():
        handler = CommandHandler(_Plugin(prefix))
        return [item async for item in handler.show_help(_Event())]

    results = asyncio.run(run())
    assert len(results) == 1
    return results[0]


def test_help_covers_every_section():
    text = _render("/")
    for title in ("开关", "查看", "管理", "从旧插件迁移"):
        assert title in text, title


def test_help_renders_default_prefix():
    text = _render("/")
    for cmd in ("/mp on", "/mp off", "/mp status", "/mp help", "/mp migrate apply"):
        assert cmd in text, cmd


def test_help_follows_custom_prefix():
    text = _render("!")
    assert "!mp status" in text
    # 用户前缀是 `!` 时，清单里不该出现任何 `/` 开头的命令
    assert "/mp" not in text


def test_help_without_prefix_has_no_stray_slash():
    text = _render("")
    assert "mp status" in text
    assert "/mp" not in text
    assert "你的唤醒前缀是空的" in text


def test_help_shows_current_prefix_and_aliases():
    text = _render("#")
    assert "「#」" in text
    for alias in ("#mp", "#magpie", "#神偷"):
        assert alias in text, alias
