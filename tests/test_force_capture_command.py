"""Force collection command parameters and intake-window replies."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.commands.command_handler import CommandHandler
from core.commands.force_capture_args import parse_force_capture_args


@pytest.mark.parametrize(
    "message,expected",
    [
        ("/mp 偷", ("", "")),
        ("!mp 偷 艾拉", ("艾拉", "")),
        ("#magpie 偷 艾拉 可塑性记忆", ("艾拉", "可塑性记忆")),
        ('神偷 偷 "Akane Kurokawa" "Oshi No Ko"', ("Akane Kurokawa", "Oshi No Ko")),
        ("/mp 偷 角色=艾拉 作品=可塑性记忆", ("艾拉", "可塑性记忆")),
        ('/mp 偷 作品="Plastic Memories" 角色=艾拉', ("艾拉", "Plastic Memories")),
        ('/mp 偷 character=Isla work="Plastic Memories"', ("Isla", "Plastic Memories")),
        ("/mp 偷 作品=可塑性记忆", ("", "可塑性记忆")),
        ("/mp 偷 艾拉 作品=可塑性记忆", ("艾拉", "可塑性记忆")),
        ("/mp 偷 角色：艾拉 作品＝可塑性记忆", ("艾拉", "可塑性记忆")),
        ("/mp 偷 蕾姆 Re:Zero", ("蕾姆", "Re:Zero")),
    ],
)
def test_complete_command_arguments_are_parsed(message, expected):
    assert parse_force_capture_args(message=message) == expected


def test_raw_arguments_recover_names_split_by_astrbot_command_binding():
    assert parse_force_capture_args(
        '"Akane', 'Kurokawa"',
        message='!mp 偷 "Akane Kurokawa" "Oshi No Ko"',
    ) == ("Akane Kurokawa", "Oshi No Ko")


def test_direct_call_parameters_are_kept_without_raw_command():
    assert parse_force_capture_args("艾拉", "可塑性记忆") == ("艾拉", "可塑性记忆")
    assert parse_force_capture_args("作品=可塑性记忆") == ("", "可塑性记忆")


@pytest.mark.parametrize(
    "tail",
    [
        '"艾拉', "角色=", "作品=", "角色=艾拉 角色=蕾姆",
        "艾拉 可塑性记忆 多余参数", "角色=艾拉 可塑性记忆 多余参数", "foo=艾拉",
    ],
)
def test_invalid_arguments_never_silently_discard_tokens(tail):
    with pytest.raises(ValueError):
        parse_force_capture_args(message=f"!mp 偷 {tail}")


def _handler(message=""):
    event = SimpleNamespace(message_str=message, plain_result=lambda value: value)
    begin = Mock()
    plugin = SimpleNamespace(
        begin_force_capture=begin,
        cmd=lambda command, event=None: f"!mp {command}",
    )
    return CommandHandler(plugin), event, begin


def _capture(handler, event, *args):
    async def run():
        return [value async for value in handler.capture(event, *args)]
    return asyncio.run(run())


def test_capture_opens_a_minute_window_for_all_images_and_reports_labels():
    handler, event, begin = _handler("!mp 偷 作品=可塑性记忆 角色=艾拉")
    replies = _capture(handler, event)
    begin.assert_called_once_with(event, 60, character="艾拉", work="可塑性记忆")
    assert len(replies) == 1
    assert "所有图片" in replies[0]
    assert "艾拉" in replies[0] and "可塑性记忆" in replies[0]
    assert "窗口结束后" in replies[0] and "继续处理" in replies[0]


def test_capture_without_labels_preserves_automatic_metadata():
    handler, event, begin = _handler("!mp 偷")
    replies = _capture(handler, event)
    begin.assert_called_once_with(event, 60, character="", work="")
    assert "指定信息" not in replies[0]


def test_capture_reads_get_message_str_when_message_attribute_is_absent():
    handler, event, begin = _handler()
    del event.message_str
    event.get_message_str = lambda: '!mp 偷 "Akane Kurokawa" "Oshi No Ko"'
    _capture(handler, event, '"Akane', 'Kurokawa"')
    begin.assert_called_once_with(event, 60, character="Akane Kurokawa", work="Oshi No Ko")


def test_invalid_command_returns_dynamic_usage_without_changing_a_window():
    handler, event, begin = _handler("!mp 偷 角色=艾拉 角色=蕾姆")
    replies = _capture(handler, event)
    begin.assert_not_called()
    assert "!mp 偷 [角色] [作品]" in replies[0]
    assert "/mp" not in replies[0]


def test_capture_reports_missing_service():
    handler, event, _begin = _handler()
    del handler.plugin.begin_force_capture
    assert "未初始化" in _capture(handler, event)[0]


def test_help_advertises_minute_capture_parameters_using_active_prefix():
    handler, event, _begin = _handler()
    handler.plugin.wake_prefix = lambda event=None: "!"

    async def run():
        return [reply async for reply in handler.show_help(event)]

    reply = asyncio.run(run())[0]
    assert "!mp 偷 [角色] [作品]" in reply
    assert "60 秒收录窗口" in reply
    assert "你的所有图片" in reply
    assert "30 秒" not in reply
