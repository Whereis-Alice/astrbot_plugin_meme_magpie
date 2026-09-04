"""容量上限相关的回归测试。

背景：容量上限（`max_reg_num`）是本插件唯一会永久删除用户表情包的开关，
1.4.0 之前它的默认值只有 100，而且埋在配置页第 46 项，用户翻到最底下才
看得见——真实反馈就是「表情包莫名少了几十张」。1.4.1 把它挪到配置页最
前面，并补了启动告警。这些测试锁住三件事：

1. 配置项确实排在最前面（不是又被挤回底部）；
2. 填 0（不限制）时 `status` / `capacity` 不会崩、也不会说反话；
3. 启动时会主动报一次超限，且只告警、绝不删除。
"""

import asyncio
import json
import types
from pathlib import Path

import pytest

from core.commands.command_handler import CommandHandler
from core.maintenance.service import MaintenanceService, format_capacity_warning

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class _Event:
    """最小 AstrMessageEvent stub：这两个命令只用到 `plain_result`。"""

    @staticmethod
    def plain_result(text):
        return text


def _drain(agen_factory):
    async def run():
        return [item async for item in agen_factory()]

    return asyncio.run(run())


# --------------------------------------------------------------------------
# 1) 配置项位置：这次改动的全部意义就在于「用户能看见它」
# --------------------------------------------------------------------------


def _schema() -> dict:
    return json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def test_capacity_settings_are_at_the_top_of_the_config_page():
    """会永久删数据的开关必须排在配置页开头，不能再埋到最底下。"""
    keys = list(_schema().keys())
    assert keys[0] == "_capacity_section"
    assert keys.index("max_reg_num") < 5
    assert keys.index("capacity_auto_cleanup") < 5
    # 也得排在第一个功能分组之前，否则等于还在下面
    assert keys.index("max_reg_num") < keys.index("_steal_section")


def test_capacity_labels_spell_out_the_zero_means_unlimited_rule():
    """填 0 = 不限制是个反直觉的约定，标题里就要写出来。"""
    schema = _schema()
    assert "0" in schema["max_reg_num"]["description"]
    hint = schema["max_reg_num"]["hint"]
    assert "永久删除" in hint
    assert "100" in hint  # 提醒老用户配置里存着的旧默认值


def test_every_config_key_is_translated_in_all_locales():
    """新增的 _capacity_section 别忘了三份 i18n。"""
    keys = set(_schema().keys())
    i18n_dir = PLUGIN_ROOT / ".astrbot-plugin" / "i18n"
    for locale_file in sorted(i18n_dir.glob("*.json")):
        config = json.loads(locale_file.read_text(encoding="utf-8"))["config"]
        missing = keys - set(config)
        assert not missing, f"{locale_file.name} 缺翻译: {sorted(missing)}"


# --------------------------------------------------------------------------
# 2) 告警文案
# --------------------------------------------------------------------------


def test_no_warning_when_capacity_is_unlimited():
    assert format_capacity_warning(9999, 0, False, "/mp capacity") is None


def test_no_warning_when_under_limit():
    assert format_capacity_warning(100, 2000, False, "/mp capacity") is None
    assert format_capacity_warning(2000, 2000, True, "/mp capacity") is None


def test_warning_without_auto_cleanup_says_nothing_was_deleted():
    msg = format_capacity_warning(134, 100, False, "!mp capacity")
    assert msg is not None
    assert "134" in msg and "100" in msg and "34" in msg
    assert "没有删除任何文件" in msg
    # 命令必须按用户真实唤醒前缀渲染，不能写死斜杠
    assert "!mp capacity" in msg


def test_warning_with_auto_cleanup_says_it_will_delete():
    msg = format_capacity_warning(134, 100, True, "/mp capacity")
    assert msg is not None
    assert "永久删除" in msg
    assert "34" in msg


# --------------------------------------------------------------------------
# 3) 启动巡检：只告警，绝不删
# --------------------------------------------------------------------------


class _FakeIndexManager:
    def __init__(self, count):
        self.idx = {f"/{i}.png": {"created_at": i} for i in range(count)}
        self.saves = 0

    async def load_index(self):
        return dict(self.idx)

    async def save_index(self, idx):
        self.saves += 1


class _FakeCapacityHandler:
    def __init__(self):
        self.calls = 0

    async def _enforce_capacity(self, idx):
        self.calls += 1


def _make_maintenance(count, *, max_reg, auto):
    index_manager = _FakeIndexManager(count)
    handler = _FakeCapacityHandler()
    plugin = types.SimpleNamespace(
        plugin_config=types.SimpleNamespace(max_reg_num=max_reg, capacity_auto_cleanup=auto),
        index_manager=index_manager,
        event_handler=handler,
        cmd=lambda sub: f"/mp {sub}",
    )
    return MaintenanceService(plugin), index_manager, handler


@pytest.mark.asyncio
async def test_startup_warns_when_already_over_limit():
    """每小时那轮要先睡一小时，所以启动时必须自己报一次。"""
    maint, index_manager, handler = _make_maintenance(134, max_reg=100, auto=False)

    msg = await maint.warn_capacity_pressure()

    assert msg is not None and "34" in msg
    assert handler.calls == 0  # 只告警
    assert index_manager.saves == 0


@pytest.mark.asyncio
async def test_startup_warns_even_when_auto_cleanup_is_on_but_still_deletes_nothing():
    maint, index_manager, handler = _make_maintenance(134, max_reg=100, auto=True)

    msg = await maint.warn_capacity_pressure()

    assert msg is not None and "永久删除" in msg
    assert handler.calls == 0
    assert index_manager.saves == 0


@pytest.mark.asyncio
async def test_startup_is_silent_when_unlimited_or_under_limit():
    maint, _, _ = _make_maintenance(9999, max_reg=0, auto=True)
    assert await maint.warn_capacity_pressure() is None

    maint, _, _ = _make_maintenance(10, max_reg=2000, auto=False)
    assert await maint.warn_capacity_pressure() is None


@pytest.mark.asyncio
async def test_startup_warning_does_not_repeat_one_hour_later():
    """启动报过一次之后，同一个数量不该在每小时巡检里再念一遍。"""
    maint, index_manager, handler = _make_maintenance(134, max_reg=100, auto=False)

    await maint.warn_capacity_pressure()
    assert maint._capacity_warned_count == 134

    await maint._check_capacity_once()
    assert maint._capacity_warned_count == 134
    assert handler.calls == 0
    assert index_manager.saves == 0


@pytest.mark.asyncio
async def test_startup_survives_broken_index_manager():
    maint, _, _ = _make_maintenance(134, max_reg=100, auto=False)

    async def boom():
        raise RuntimeError("db locked")

    maint.plugin.index_manager.load_index = boom
    assert await maint.warn_capacity_pressure() is None


# --------------------------------------------------------------------------
# 4) mp status / mp capacity 在「不限制」下的表现
# --------------------------------------------------------------------------


def _status_plugin(tmp_path, *, max_reg, auto=False, count=3):
    index = {
        f"{tmp_path}/{i}.png": {"category": "开心", "created_at": i} for i in range(count)
    }
    return types.SimpleNamespace(
        plugin_config=types.SimpleNamespace(
            steal_meme=True,
            auto_send_meme=True,
            steal_mode="probability",
            steal_chance=0.5,
            image_processing_cooldown=10,
            meme_chance=0.4,
            content_filtration=False,
            max_reg_num=max_reg,
            capacity_auto_cleanup=auto,
        ),
        index_manager=_StaticIndexManager(index),
        raw_dir=tmp_path / "raw",
        _load_vision_provider_id=lambda: "gpt-4o",
    )


class _StaticIndexManager:
    def __init__(self, index):
        self.index = index

    async def load_index(self):
        return dict(self.index)


def test_status_does_not_divide_by_zero_when_unlimited(tmp_path):
    """填 0 时旧代码会 ZeroDivisionError，直接把 status 命令炸掉。"""
    handler = CommandHandler(_status_plugin(tmp_path, max_reg=0))
    (text,) = _drain(lambda: handler.status(_Event()))
    assert "未设上限" in text
    assert "/0" not in text


def test_status_shows_percentage_when_limited(tmp_path):
    handler = CommandHandler(_status_plugin(tmp_path, max_reg=2000))
    (text,) = _drain(lambda: handler.status(_Event()))
    assert "3/2000" in text


def test_status_background_line_matches_real_behaviour(tmp_path):
    """默认只告警不删，不能再一律写「容量控制: 自动」。"""
    handler = CommandHandler(_status_plugin(tmp_path, max_reg=2000, auto=False))
    (text,) = _drain(lambda: handler.status(_Event()))
    assert "仅告警" in text

    handler = CommandHandler(_status_plugin(tmp_path, max_reg=2000, auto=True))
    (text,) = _drain(lambda: handler.status(_Event()))
    assert "自动清理" in text

    handler = CommandHandler(_status_plugin(tmp_path, max_reg=0))
    (text,) = _drain(lambda: handler.status(_Event()))
    assert "未设上限" in text


def _capacity_plugin(count, max_reg):
    index_manager = _FakeIndexManager(count)
    handler = _FakeCapacityHandler()
    plugin = types.SimpleNamespace(
        plugin_config=types.SimpleNamespace(max_reg_num=max_reg, capacity_auto_cleanup=False),
        index_manager=index_manager,
        event_handler=handler,
    )
    return plugin, index_manager, handler


def test_capacity_command_reports_unlimited_instead_of_deleting_zero():
    """填 0 时旧代码会一路走到清理逻辑，最后报「删除了 0 个」把人绕晕。"""
    plugin, index_manager, handler = _capacity_plugin(134, 0)
    cmd = CommandHandler(plugin)
    (text,) = _drain(lambda: cmd.enforce_capacity(_Event()))
    assert "已关闭" in text and "134" in text
    assert handler.calls == 0
    assert index_manager.saves == 0


def test_capacity_command_still_cleans_when_over_limit():
    plugin, index_manager, handler = _capacity_plugin(134, 100)
    cmd = CommandHandler(plugin)
    (text,) = _drain(lambda: cmd.enforce_capacity(_Event()))
    assert handler.calls == 1
    assert index_manager.saves == 1
    assert "永久删除" in text


def test_capacity_command_is_quiet_when_under_limit():
    plugin, index_manager, handler = _capacity_plugin(10, 100)
    cmd = CommandHandler(plugin)
    (text,) = _drain(lambda: cmd.enforce_capacity(_Event()))
    assert handler.calls == 0
    assert "无需清理" in text
