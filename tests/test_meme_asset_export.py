"""跨插件表情包资源导出接口测试。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from core.db.database_service import DatabaseService
from core.integration.meme_asset import (
    MemeAssetExportError,
    MemeAssetExportService,
)


class _State:
    def __init__(self, candidates=None):
        self.candidates = list(candidates or [])

    def get_candidates(self):
        return self.candidates

    def set_candidates(self, candidates):
        self.candidates = candidates


class _Event:
    def __init__(self, target="group:100"):
        self.target = target
        self.unified_msg_origin = target


class _Config:
    def __init__(self, data_dir: Path, target="group:100"):
        self.data_dir = data_dir
        self.target = target

    def get_event_target(self, _event):
        event_target = getattr(_event, "target", self.target)
        scope, _, target_id = event_target.partition(":")
        return scope, target_id


def _image(path: Path, color=(30, 90, 180)) -> tuple[bytes, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (12, 8), color).save(path, format="JPEG")
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _plugin(tmp_path: Path, *, entry=None, candidates=None, target="group:100"):
    data_dir = tmp_path / "plugin_data"
    db = DatabaseService(data_dir / "cache" / "emoji.db")
    state = _State(candidates)
    plugin = SimpleNamespace(
        base_dir=data_dir,
        plugin_config=_Config(data_dir, target),
        db_service=db,
        meme_selector=SimpleNamespace(
            is_path_allowed_for_event=lambda _path, _event: True
        ),
        _emoji_turn_state=lambda _event: state,
        is_send_enabled_for_event=lambda _event: True,
    )
    if entry:
        asyncio.run(db.insert_batch([entry]))
    return plugin, state, db


def _export(plugin, emoji_id, event=None):
    return asyncio.run(MemeAssetExportService(plugin).export_meme_asset(emoji_id, event))


def test_numeric_and_explicit_candidate_ids_export_bytes(tmp_path: Path):
    path = tmp_path / "plugin_data" / "categories" / "happy" / "one.jpg"
    data, digest = _image(path)
    entry = {
        "path": str(path),
        "hash": digest,
        "category": "happy",
        "desc": "开心",
        "tags": ["笑"],
        "scenes": ["庆祝"],
        "work": "示例作品",
        "character": "示例角色",
        "scope_mode": "public",
    }
    candidate = {
        "id": "emoji_1",
        "emoji_id": "emoji_1",
        "path": str(path),
        "hash": digest,
        "candidate_created_at": time.time(),
    }
    plugin, state, _ = _plugin(tmp_path, entry=entry, candidates=[candidate])

    first = _export(plugin, 1, _Event())
    assert first.emoji_id == "emoji_1"
    assert first.mime_type == "image/jpeg"
    assert first.size == len(data)
    assert first.sha256 == digest
    assert asyncio.run(first.read_bytes()) == data
    first.release()

    # A fresh service/lease verifies the explicit spelling independently.
    second = asyncio.run(MemeAssetExportService(plugin).export_meme_asset("emoji_1", _Event()))
    assert asyncio.run(second.read()) == data
    assert state.get_candidates()[0]["path"] == str(path)


def test_handle_metadata_is_safe_and_contains_known_fields(tmp_path: Path):
    path = tmp_path / "plugin_data" / "categories" / "happy" / "one.jpg"
    _, digest = _image(path)
    entry = {
        "path": str(path),
        "hash": digest,
        "category": "happy",
        "desc": "开心",
        "tags": ["笑"],
        "scenes": ["庆祝"],
        "work": "示例作品",
        "character": "示例角色",
        "scope_mode": "public",
    }
    candidate = {
        "id": "emoji_1",
        "path": str(path),
        "hash": digest,
        "candidate_created_at": time.time(),
    }
    plugin, _, _ = _plugin(tmp_path, entry=entry, candidates=[candidate])
    handle = _export(plugin, "1", _Event())
    payload = handle.to_dict()
    assert payload["mime"] == "image/jpeg"
    assert payload["size"] > 0
    assert payload["sha256"] == digest
    assert payload["metadata"]["work"] == "示例作品"
    assert payload["metadata"]["character"] == "示例角色"
    assert payload["metadata"]["tags"] == ["笑"]
    assert payload["metadata"]["scenes"] == ["庆祝"]
    assert "path" not in payload
    assert "path" not in payload["metadata"]


def test_release_and_expiry_have_stable_error_codes(tmp_path: Path):
    path = tmp_path / "plugin_data" / "categories" / "happy" / "one.jpg"
    data, digest = _image(path)
    entry = {"path": str(path), "hash": digest, "category": "happy", "scope_mode": "public"}
    candidate = {"id": "emoji_1", "path": str(path), "hash": digest}
    plugin, _, _ = _plugin(tmp_path, entry=entry, candidates=[candidate])
    service = MemeAssetExportService(plugin)

    released = asyncio.run(service.export_meme_asset(1, _Event()))
    assert released.release() is True
    with pytest.raises(MemeAssetExportError) as released_error:
        asyncio.run(released.read_bytes())
    assert released_error.value.code == "asset_released"

    expired = asyncio.run(service.export_meme_asset(1, _Event()))
    expired._deadline_monotonic = time.monotonic() - 1
    with pytest.raises(MemeAssetExportError) as expired_error:
        asyncio.run(expired.read_bytes())
    assert expired_error.value.code == "asset_expired"
    assert data


def test_invalid_or_missing_candidate_is_rejected(tmp_path: Path):
    plugin, _, _ = _plugin(tmp_path, candidates=[])
    service = MemeAssetExportService(plugin)
    with pytest.raises(MemeAssetExportError) as error:
        asyncio.run(service.export_meme_asset(1, _Event()))
    assert error.value.code == "candidate_expired"

    plugin, _, _ = _plugin(tmp_path, candidates=[{"id": "emoji_1", "path": "x"}])
    with pytest.raises(MemeAssetExportError) as error:
        asyncio.run(MemeAssetExportService(plugin).export_meme_asset("/outside", _Event()))
    assert error.value.code == "invalid_emoji_id"


def test_missing_file_and_unsafe_path_are_rejected(tmp_path: Path):
    missing = tmp_path / "plugin_data" / "categories" / "happy" / "missing.jpg"
    digest = "a" * 64
    entry = {"path": str(missing), "hash": digest, "category": "happy"}
    plugin, _, _ = _plugin(
        tmp_path,
        entry=entry,
        candidates=[{"id": "emoji_1", "path": str(missing), "hash": digest}],
    )
    with pytest.raises(MemeAssetExportError) as missing_error:
        _export(plugin, 1, _Event())
    assert missing_error.value.code == "file_missing"

    outside = tmp_path / "outside.jpg"
    _, outside_hash = _image(outside)
    entry = {"path": str(outside), "hash": outside_hash, "category": "happy"}
    plugin, _, _ = _plugin(
        tmp_path,
        entry=entry,
        candidates=[{"id": "emoji_1", "path": str(outside), "hash": outside_hash}],
    )
    with pytest.raises(MemeAssetExportError) as unsafe_error:
        _export(plugin, 1, _Event())
    assert unsafe_error.value.code == "unsafe_path"


def test_local_scope_requires_origin_session(tmp_path: Path):
    path = tmp_path / "plugin_data" / "categories" / "happy" / "one.jpg"
    _, digest = _image(path)
    entry = {
        "path": str(path),
        "hash": digest,
        "category": "happy",
        "scope_mode": "local",
        "origin_target": "group:100",
    }
    candidate = {"id": "emoji_1", "path": str(path), "hash": digest}
    plugin, _, _ = _plugin(tmp_path, entry=entry, candidates=[candidate])
    with pytest.raises(MemeAssetExportError) as denied:
        _export(plugin, 1, _Event("group:200"))
    assert denied.value.code == "scope_denied"
    handle = _export(plugin, 1, _Event("group:100"))
    assert asyncio.run(handle.read_bytes())


def test_stale_compat_path_recovers_actual_category_file(tmp_path: Path):
    actual = tmp_path / "plugin_data" / "categories" / "think" / "1788536201_99df0618.jpg"
    data, digest = _image(actual)
    stale = tmp_path / "plugin_data" / "plugin_stealer_split_compat" / actual.name
    entry = {
        "path": str(stale),
        "hash": digest,
        "category": "think",
        "desc": "思考",
        "scope_mode": "public",
    }
    candidate = {
        "id": "emoji_1",
        "path": str(stale),
        "hash": digest,
        "candidate_created_at": time.time(),
    }
    plugin, _, _ = _plugin(tmp_path, entry=entry, candidates=[candidate])
    handle = _export(plugin, 1, _Event())
    assert asyncio.run(handle.read_bytes()) == data


def test_symlink_is_rejected_when_supported(tmp_path: Path):
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlink unsupported")
    real = tmp_path / "real.jpg"
    _, digest = _image(real)
    linked = tmp_path / "plugin_data" / "categories" / "happy" / "linked.jpg"
    linked.parent.mkdir(parents=True, exist_ok=True)
    try:
        linked.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    entry = {"path": str(linked), "hash": digest, "category": "happy"}
    plugin, _, _ = _plugin(
        tmp_path,
        entry=entry,
        candidates=[{"id": "emoji_1", "path": str(linked), "hash": digest}],
    )
    with pytest.raises(MemeAssetExportError) as error:
        _export(plugin, 1, _Event())
    assert error.value.code == "unsafe_path"


def test_structured_search_filters_and_hides_path(tmp_path: Path):
    from astrbot_plugin_meme_magpie.main import Main
    from core.integration.candidate_search import MemeCandidateSearchService

    first = tmp_path / "plugin_data" / "categories" / "happy" / "one.jpg"
    second = tmp_path / "plugin_data" / "categories" / "sad" / "two.jpg"
    _, first_hash = _image(first)
    _, second_hash = _image(second, (180, 30, 40))
    entries = [
        {
            "path": str(first),
            "hash": first_hash,
            "category": "happy",
            "desc": "后藤一里开心",
            "work": "孤独摇滚",
            "character": "后藤一里",
            "tags": ["吉他", "社恐"],
            "scenes": ["庆祝"],
            "scope_mode": "public",
        },
        {
            "path": str(second),
            "hash": second_hash,
            "category": "sad",
            "desc": "难过",
            "work": "别的作品",
            "tags": ["哭"],
            "scope_mode": "public",
        },
    ]
    plugin, state, db = _plugin(tmp_path)
    asyncio.run(db.insert_batch(entries))
    service = MemeCandidateSearchService(plugin)
    plugin.get_meme_integration_service = lambda: service
    plugin._search_meme_candidates = lambda event, query, *, limit, idx: asyncio.sleep(0, result=[
        (str(first), "后藤一里开心", "happy", "吉他,社恐"),
        (str(second), "难过", "sad", "哭"),
    ])

    result = asyncio.run(
        Main.search_meme_candidates(
            plugin,
            _Event(),
            "孤独摇滚",
            limit=5,
            filters={"work": "孤独摇滚", "tag": "吉他"},
        )
    )
    assert len(result) == 1
    assert result[0]["emoji_id"] == "emoji_1"
    assert result[0]["work"] == "孤独摇滚"
    assert result[0]["tags"] == ["吉他", "社恐"]
    assert "path" not in result[0]
    assert state.get_candidates()[0]["path"] == str(first)

    assert asyncio.run(Main.search_meme_candidates(plugin, _Event(), "", limit=5)) == []
