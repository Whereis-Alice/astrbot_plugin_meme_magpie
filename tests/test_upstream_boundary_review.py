"""Independent regressions for review findings; all effects stay in tmp_path."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.events.meme_sender_engine import MemeSenderEngine
from core.search.candidate_reranker import MemeCandidateReranker, MemeDecision


class _Event:
    unified_msg_origin = "test:group:123"

    def __init__(self):
        self.extras = {}

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


def test_attach_stops_when_tool_sent_while_reranking():
    async def run():
        selector = SimpleNamespace(
            attach_emoji_to_result=AsyncMock(),
            attach_selected_emoji=AsyncMock(return_value="selected.png"),
            record_emoji_usage=AsyncMock(),
        )
        engine = MemeSenderEngine(SimpleNamespace(meme_selector=selector, auto_meme_attach_timeout=1))
        event = _Event()

        async def choose(*args):
            engine.emoji_turn_state(event).mark_active_sent()
            return MemeDecision("selected.png", "hash")

        engine._candidate_reranker.choose = choose
        engine._candidate_reranker.is_current = lambda decision, event: True
        assert await engine.attach_emoji_to_result(event, object(), "回复", []) is True
        selector.attach_selected_emoji.assert_not_awaited()
        selector.attach_emoji_to_result.assert_not_awaited()
        selector.record_emoji_usage.assert_not_awaited()
        assert engine._auto_emoji_cooldowns == {}

    asyncio.run(run())


@pytest.mark.parametrize(
    "origin,current,allowed",
    [("", ("group", "123"), False), ("group:123", ("", ""), False),
     ("group:999", ("group", "123"), False), ("group:123", ("group", "123"), True)],
)
def test_local_rerank_requires_both_known_matching_targets(tmp_path, origin, current, allowed):
    image = tmp_path / "local.png"
    image.write_bytes(b"test image; metadata selection does not decode it")
    path = str(image)
    metadata = {"hash": "hash", "scope_mode": "local", "origin_target": origin, "desc": "local only"}
    llm = AsyncMock(return_value=SimpleNamespace(completion_text='{"choice":"emoji_1"}'))
    plugin = SimpleNamespace(
        base_dir=tmp_path,
        plugin_config=SimpleNamespace(
            enable_meme_rerank=True, meme_rerank_timeout=1,
            emotion_analysis_provider_id="test-model", get_event_target=lambda event: current,
        ),
        is_send_enabled_for_event=lambda event: True,
        db_service=SimpleNamespace(get_emoji=lambda candidate: metadata if candidate == path else None),
        meme_selector=SimpleNamespace(
            smart_search=AsyncMock(return_value=[(path, "desc", "happy", "tags")]),
            # The old selector policy is deliberately permissive: the new
            # reranker must enforce complete local-scope metadata itself.
            is_path_allowed_for_event=lambda path, event: True,
        ),
        context=SimpleNamespace(llm_generate=llm),
    )
    result = asyncio.run(MemeCandidateReranker(plugin).choose(_Event(), "回复"))
    if allowed:
        assert result == MemeDecision(path, "hash")
        llm.assert_awaited_once()
    else:
        assert result == MemeDecision()
        llm.assert_not_awaited()


@pytest.mark.parametrize("keys", [["happy", "../escape"], ["Happy", "happy"], ["happy", "C:\\escape"]])
def test_main_setattr_config_route_rejects_invalid_keys_before_persistence(tmp_path, monkeypatch, keys):
    from core.config import config as config_module
    from astrbot_plugin_meme_magpie.main import Main

    monkeypatch.setattr(config_module, "StarTools", SimpleNamespace(get_data_dir=lambda name: tmp_path))
    cfg = config_module.PluginConfig({})
    original_keys = list(cfg.categories)
    original_bytes = cfg.categories_path.read_bytes()
    plugin = Main.__new__(Main)
    plugin.plugin_config = cfg
    # Exercise the real Main route, which writes via setattr rather than
    # PluginConfig.update_config and catches validation errors itself.
    plugin.update_config({"categories": keys})
    assert cfg.categories == original_keys
    assert cfg.categories_path.read_bytes() == original_bytes
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("case", ["unsafe_key", "outside_index_path"])
def test_category_delete_preflights_all_targets_before_any_removal(tmp_path, monkeypatch, case):
    from astrbot_plugin_meme_magpie import plugin_api as module

    key = "../escape" if case == "unsafe_key" else "happy"
    category_dir = tmp_path / "categories" / "happy"
    category_dir.mkdir(parents=True)
    inside = category_dir / "inside.png"
    inside.write_bytes(b"keep inside")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep outside")
    index = {
        str(inside): {"category": key, "hash": "inside"},
        str(outside): {"category": key, "hash": "outside"},
    }
    remove_file = AsyncMock(return_value=True)
    remove_tree = Mock()
    update = Mock()
    cfg = SimpleNamespace(categories=[key, "sad"], categories_dir=tmp_path / "categories")
    plugin = SimpleNamespace(
        base_dir=tmp_path,
        plugin_config=cfg,
        db_service=SimpleNamespace(get_index_cache_readonly=lambda: index),
        update_config=update,
    )
    api = module.PluginAPI(plugin)
    delete_rows = AsyncMock()
    monkeypatch.setattr(api, "_delete_index_paths", delete_rows)
    monkeypatch.setattr(module, "safe_remove_file", remove_file)
    monkeypatch.setattr(module.shutil, "rmtree", remove_tree)
    monkeypatch.setattr(module, "request", SimpleNamespace(get_json=AsyncMock(return_value={"key": key})))
    monkeypatch.setattr(module, "jsonify", lambda body: body)

    result = asyncio.run(api.handle_delete_category())
    assert result["success"] is False
    remove_file.assert_not_awaited()
    delete_rows.assert_not_awaited()
    remove_tree.assert_not_called()
    update.assert_not_called()
    assert inside.read_bytes() == b"keep inside"
    assert outside.read_bytes() == b"keep outside"
