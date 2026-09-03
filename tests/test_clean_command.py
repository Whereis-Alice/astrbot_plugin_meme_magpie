"""`mp clean` / `mp tag_stats` 的回归测试。

上游 astrbot_plugin_stealer 里 `clean` 的方法定义行丢了，导致两个后果：

1. `CommandHandler.clean` 根本不存在，`mp clean` 直接 AttributeError；
2. 那段清理 raw 的代码被归到了 `tag_stats` 的函数体里，于是只该读数据的
   `mp tag_stats` 会顺带把整个 raw 暂存目录删掉。

这两条各锁一个测试，防止后续重构时又把两个方法粘回去。
"""

import asyncio
import inspect

from core.commands.command_handler import CommandHandler


class _Event:
    """最小 AstrMessageEvent stub：这两个命令只用到 `plain_result`。"""

    @staticmethod
    def plain_result(text):
        return text


class _EventHandler:
    """记下 `_clean_raw_directory` 被谁叫过。"""

    def __init__(self):
        self.calls = 0

    async def _clean_raw_directory(self):
        self.calls += 1
        return 7


class _DbService:
    @staticmethod
    def get_tag_stats(top_n):
        return {
            "total_emojis": 3,
            "total_with_tags": 2,
            "zero_tag_count": 1,
            "top_tags": [{"tag": "开心", "count": 2}],
            "single_use_tags": ["瞌眸"],
            "top_scenes": [{"scene": "吐槽", "count": 2}],
        }


class _Plugin:
    def __init__(self):
        self.event_handler = _EventHandler()
        self.db_service = _DbService()


def _drain(agen_factory):
    async def run():
        return [item async for item in agen_factory()]

    return asyncio.run(run())


def test_clean_is_a_real_method():
    """上游缺失的就是这一行：main.py 调 `command_handler.clean(event, mode)`。"""
    assert hasattr(CommandHandler, "clean")
    assert inspect.isasyncgenfunction(CommandHandler.clean)
    params = list(inspect.signature(CommandHandler.clean).parameters)
    assert params == ["self", "event", "mode"]


def test_clean_reports_deleted_count():
    plugin = _Plugin()
    handler = CommandHandler(plugin)
    results = _drain(lambda: handler.clean(_Event()))
    assert plugin.event_handler.calls == 1
    assert len(results) == 1
    assert "7" in results[0]


def test_clean_accepts_legacy_force_argument():
    """老用户习惯输入 `clean force`，不能因此报错。"""
    plugin = _Plugin()
    handler = CommandHandler(plugin)
    results = _drain(lambda: handler.clean(_Event(), "force"))
    assert plugin.event_handler.calls == 1
    assert len(results) == 1


def test_tag_stats_is_read_only():
    """统计命令不得碰 raw 目录，也不得多吐一句清理报告。"""
    plugin = _Plugin()
    handler = CommandHandler(plugin)
    results = _drain(lambda: handler.tag_stats(_Event()))
    assert plugin.event_handler.calls == 0
    assert len(results) == 1
    assert "标签统计" in results[0]
    assert "raw" not in results[0]
