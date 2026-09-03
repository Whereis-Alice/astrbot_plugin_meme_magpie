"""唤醒前缀推导与命令文案渲染测试。

AstrBot 的 `wake_prefix` 是用户可改的顶层配置（list 类型，出厂 `["/"]`），
所以插件里所有“接下来请执行 xxx”的提示都不能写死 `/`。这组测试盯住
四种真实会遇到的配置形态：列表、字符串、空（只靠 @机器人 唤醒）、读配置报错。
"""

import re

from core.util.command_hint import (
    CHINESE_COMMAND_GROUP,
    COMMAND_GROUP,
    DEFAULT_WAKE_PREFIX,
    LEGACY_COMMAND_GROUP,
    command_like_pattern,
    format_command,
    resolve_wake_prefix,
)


class _Context:
    """模拟 AstrBot Context：`get_config(umo=None)` 返回一份 dict-like 配置。"""

    def __init__(self, config, session_config=None, raise_error=False):
        self._config = config
        self._session_config = session_config
        self._raise_error = raise_error
        self.calls = []

    def get_config(self, umo=None):
        self.calls.append(umo)
        if self._raise_error:
            raise RuntimeError("config backend down")
        if umo and self._session_config is not None:
            return self._session_config
        return self._config


# ── resolve_wake_prefix ──


def test_list_prefix_takes_first_string():
    ctx = _Context({"wake_prefix": ["!", "/"]})
    assert resolve_wake_prefix(ctx) == "!"


def test_string_prefix_is_used_as_is():
    assert resolve_wake_prefix(_Context({"wake_prefix": "#"})) == "#"


def test_empty_list_means_no_prefix():
    # 用户清空了唤醒前缀：此时 AstrBot 只靠 @机器人 / 私聊唤醒，提示不应再带 `/`
    assert resolve_wake_prefix(_Context({"wake_prefix": []})) == ""
    assert resolve_wake_prefix(_Context({"wake_prefix": [""]})) == ""


def test_missing_key_falls_back_to_default():
    assert resolve_wake_prefix(_Context({})) == DEFAULT_WAKE_PREFIX


def test_config_error_falls_back_to_default():
    ctx = _Context({"wake_prefix": ["!"]}, raise_error=True)
    assert resolve_wake_prefix(ctx) == DEFAULT_WAKE_PREFIX


def test_non_string_list_items_fall_back_to_default():
    assert resolve_wake_prefix(_Context({"wake_prefix": [None, 3]})) == DEFAULT_WAKE_PREFIX


def test_umo_reads_session_level_config():
    ctx = _Context({"wake_prefix": ["/"]}, session_config={"wake_prefix": ["~"]})
    assert resolve_wake_prefix(ctx, "aiocqhttp:GroupMessage:123") == "~"
    assert ctx.calls == ["aiocqhttp:GroupMessage:123"]


# ── format_command ──


def test_format_command_default_group():
    assert format_command("migrate apply") == "/mp migrate apply"


def test_format_command_custom_prefix_and_empty_sub():
    assert format_command("list", "!") == "!mp list"
    assert format_command("", "!") == "!mp"
    assert format_command("  ", "#") == "#mp"


def test_format_command_without_prefix():
    # 空前缀时只给“mp xxx”，避免凭空多出一个斜杠让用户照抄失败
    assert format_command("help", "") == "mp help"


def test_format_command_supports_legacy_group():
    assert format_command("help", "/", LEGACY_COMMAND_GROUP) == "/magpie help"


# ── command_like_pattern ──


def test_pattern_matches_all_group_aliases():
    pattern = command_like_pattern("/")
    for group in (COMMAND_GROUP, LEGACY_COMMAND_GROUP, CHINESE_COMMAND_GROUP):
        assert re.search(pattern, f"/{group} list"), group
        # 前缀可选：私聊 / @机器人 时 AstrBot 会先把前缀剥掉
        assert re.search(pattern, f"{group} list"), group


def test_pattern_respects_custom_prefix():
    pattern = command_like_pattern("!")
    assert re.search(pattern, "!mp list")
    assert re.search(pattern, "mp list")


def test_pattern_allows_chinese_alias_inside_sentence():
    pattern = command_like_pattern("/")
    assert re.search(pattern, "用神偷 发图")


def test_pattern_ignores_plain_chat():
    pattern = command_like_pattern("/")
    assert not re.search(pattern, "今天天气真好啊")
    assert not re.search(pattern, "mp")
    # 单词里嵌着 mp 的普通聊天不能被误判（如 "amp 3"）
    assert not re.search(pattern, "amp 3")


def test_pattern_escapes_regex_metacharacters():
    # 用户把前缀设成 `.` 或 `?` 时，不能把正则玩坏
    pattern = command_like_pattern(".")
    assert re.compile(pattern)
    assert re.search(pattern, ".mp list")
    assert not re.search(pattern, "xmp")
