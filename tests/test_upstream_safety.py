"""Regression tests for adopted directory and capacity boundaries."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config.config import PluginConfig
from core.util.category_paths import category_path, validate_category_key
from core.util.retention import count_capacity_managed, rank_eviction_candidates
from core.events.event_handler import EventHandler


@pytest.mark.parametrize("key", ["开心", "happy", "Happy", "自定义 分类", "笑脸_2", "raw", "cache", "pending", "categories"])
def test_existing_portable_category_keys_are_not_renamed(tmp_path, key):
    assert validate_category_key(key) == key
    assert category_path(tmp_path, key) == tmp_path / key
    assert not (tmp_path / key).exists()


@pytest.mark.parametrize("key", ["", ".", "..", "../escape", "C:\\escape", "a/b", "a\\b", "CON", "con.txt", "nul", "lpt1", "x.", " x", "x ", "x\x00", "a:b"])
def test_unsafe_category_keys_are_rejected_without_creating_files(tmp_path, key):
    with pytest.raises(ValueError):
        category_path(tmp_path, key)
    assert list(tmp_path.iterdir()) == []


def test_category_directory_cannot_follow_link(tmp_path, monkeypatch):
    # Test the boundary on Windows even without symlink creation privileges.
    monkeypatch.setattr(Path, "is_symlink", lambda _self: True)
    with pytest.raises(ValueError):
        category_path(tmp_path, "happy")


@pytest.mark.parametrize("key", ["COM¹", "COM².txt", "com³", "LPT¹", "LPT²", "lpt³.txt", "CON .txt", "aux  .data"])
def test_windows_device_aliases_are_rejected_on_all_platforms(tmp_path, key):
    with pytest.raises(ValueError):
        category_path(tmp_path, key)
    assert list(tmp_path.iterdir()) == []


def test_ensure_category_dir_checks_before_mkdir(tmp_path):
    cfg = SimpleNamespace(categories_dir=tmp_path)
    with pytest.raises(ValueError):
        PluginConfig.ensure_category_dir(cfg, "../escape")
    assert list(tmp_path.iterdir()) == []
    assert PluginConfig.ensure_category_dir(cfg, "开心").is_dir()


def test_weighted_order_keeps_popular_old_images():
    idx = {
        "old_popular": {"created_at": 10, "use_count": 100},
        "new_unused": {"created_at": 20, "use_count": 0},
        "favorite": {"created_at": 0, "is_favorite": True},
        "imported": {"created_at": 0, "retention_class": "external"},
        "pinned": {"created_at": 0, "retention_class": "pinned"},
    }
    assert rank_eviction_candidates(idx)[0][0] == "old_popular"
    assert rank_eviction_candidates(idx, strategy="weighted")[0][0] == "new_unused"
    assert count_capacity_managed(idx) == 3  # Preserve the existing quota contract.
    assert len(rank_eviction_candidates(idx, strategy="weighted")) == 2


@pytest.mark.parametrize("weight", [0, 1, float("nan"), float("inf"), "bad", None, -1])
def test_weighted_order_is_finite_and_deterministic(weight):
    idx = {"b": {"use_count": float("nan"), "created_at": "bad"}, "a": {"use_count": None, "created_at": float("inf")}}
    assert rank_eviction_candidates(idx, strategy="weighted", usage_weight=weight) == [("a", 0), ("b", 0)]


def test_capacity_configuration_keeps_safe_defaults():
    fields = PluginConfig.model_fields
    assert fields["capacity_auto_cleanup"].default is False
    assert fields["capacity_eviction_strategy"].default == "oldest"
    assert fields["enable_meme_rerank"].default is False


def test_category_api_rejects_entire_batch_before_saving(tmp_path, monkeypatch):
    from astrbot_plugin_meme_magpie import plugin_api as module

    saved = []
    api = module.PluginAPI(SimpleNamespace(
        plugin_config=SimpleNamespace(categories_dir=tmp_path),
        update_config=lambda updates: saved.append(updates),
    ))

    async def data():
        return {"categories": ["happy", "../escape"]}

    monkeypatch.setattr(module, "request", SimpleNamespace(get_json=data))
    monkeypatch.setattr(module, "jsonify", lambda body: body)
    assert asyncio.run(api._categories_update())["success"] is False
    assert not saved and not list(tmp_path.iterdir())


def test_event_capacity_uses_configured_order_without_mutating_index():
    plugin = SimpleNamespace(plugin_config=SimpleNamespace(max_reg_num=1, capacity_eviction_strategy="weighted", capacity_usage_weight=0.7))
    idx = {"old": {"created_at": 1, "use_count": 10}, "new": {"created_at": 2, "use_count": 0}}
    handler = EventHandler.__new__(EventHandler)
    handler.plugin = plugin
    assert handler._select_items_for_removal(idx) == [("new", 2)]
    assert len(idx) == 2
