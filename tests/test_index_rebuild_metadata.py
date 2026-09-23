"""重建元数据回归：同路径、改路径/hash、旧 JSON，均不触发容量删图。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.commands.index_rebuild_command import IndexRebuildCommand
from core.db.database_service import DatabaseService


def _protected_metadata():
    return {
        "hash": "content-hash",
        "category": "old-category",
        "desc": "角色在挥手",
        "phash": "11223344",
        "tags": ["角色", "挥手"],
        "scenes": ["打个招呼"],
        "is_favorite": True,
        "character": "alice",
        "work": "known-work",
        "retention_class": "external",
        "use_count": 17,
        "last_used_at": 456,
        "created_at": 123,
        "reviewed_at": 234,
        "overlay_text": "你好",
        "emotions": ["happy", "shy"],
        "source": "imported",
        "origin_target": "group:123",
        "scope_mode": "local",
        "source_url": "https://example.org/image.png",
        "original_name": "original.png",
        "width": 20,
        "height": 30,
        "format": "png",
        "bytes": 120,
        "add_method": "manual",
    }


@pytest.mark.parametrize("source", ["same_path", "moved_hash", "legacy_json"])
def test_rebuild_preserves_protection_and_semantics_in_real_database(tmp_path, source):
    async def run():
        db = DatabaseService(tmp_path / "emoji.db")
        new_path = str(tmp_path / "categories" / "happy" / "new.png")
        old_path = new_path if source == "same_path" else str(tmp_path / "old" / "different.png")
        original = _protected_metadata()
        if source == "legacy_json":
            (tmp_path / "index.json").write_text(
                json.dumps({old_path: original}), encoding="utf-8"
            )
        else:
            await db.insert_batch([{"path": old_path, **original}])
        rebuilt = {
            new_path: {"hash": original["hash"], "category": "happy", "created_at": 999},
            str(tmp_path / "second.png"): {"hash": "second", "category": "happy", "created_at": 999},
        }
        manager = SimpleNamespace(
            rebuild_index_from_files=AsyncMock(return_value=rebuilt),
            save_index=AsyncMock(side_effect=db.sync_index),
        )
        plugin = SimpleNamespace(
            plugin_config=SimpleNamespace(categories_dir=tmp_path / "categories"),
            db_service=db,
            index_manager=manager,
            cache_dir=None,
            base_dir=tmp_path,
            max_reg_num=1,
            cmd=lambda command, event: f"!mp {command}",
            event_handler=SimpleNamespace(_enforce_capacity=AsyncMock()),
        )
        event = SimpleNamespace(plain_result=lambda text: text)
        results = [text async for text in IndexRebuildCommand(plugin).rebuild_index(event)]
        assert any("索引重建完成" in text for text in results)
        saved = db.get_emoji(new_path)
        for key, value in original.items():
            if key != "category":
                assert saved[key] == value, (source, key, saved)
        assert saved["category"] == "happy"
        manager.save_index.assert_awaited_once()
        plugin.event_handler._enforce_capacity.assert_not_awaited()
        assert any("不会删图" in text for text in results)

    asyncio.run(run())


def test_restoration_keeps_false_zero_and_legacy_action_values(tmp_path):
    async def run():
        old = _protected_metadata()
        old.update(is_favorite=False, use_count=0, last_used_at=0, action="挥手")
        new_path = str(tmp_path / "new.png")
        rebuilt = {new_path: {"hash": old["hash"], "category": "happy", "created_at": 999}}
        manager = SimpleNamespace(
            rebuild_index_from_files=AsyncMock(return_value=rebuilt), save_index=AsyncMock()
        )
        plugin = SimpleNamespace(
            plugin_config=SimpleNamespace(categories_dir=tmp_path),
            db_service=SimpleNamespace(count_total=lambda: 1, get_index_cache_readonly=lambda: {"old.png": old}),
            index_manager=manager, cache_dir=None, base_dir=None, max_reg_num=0,
        )
        event = SimpleNamespace(plain_result=lambda text: text)
        results = [text async for text in IndexRebuildCommand(plugin).rebuild_index(event)]
        assert any("索引重建完成" in text for text in results)
        restored = manager.save_index.call_args.args[0][new_path]
        assert restored["is_favorite"] is False
        assert restored["use_count"] == 0
        assert restored["last_used_at"] == 0
        assert restored["action"] == "挥手"

    asyncio.run(run())
