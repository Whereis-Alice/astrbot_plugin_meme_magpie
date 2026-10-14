"""备份导出（magpie-backup）与回环导入：完整元数据、只增不覆盖、自动建分类/角色。"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from core.db.database_service import DatabaseService
from core.maintenance.backup_service import BackupService
from core.sources.pack_source import PackSource
from core.sources.source_service import SourceService


def _png_bytes(color=(255, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


class _Config:
    """最小配置桩：SourceService / BackupService 真正会读的字段与方法。"""

    external_sources_enabled = True
    external_source_allow_http = False
    external_source_default_review = False
    external_source_max_items = 100
    external_source_max_image_bytes = 1024 * 1024
    external_source_max_archive_bytes = 10 * 1024 * 1024
    external_source_max_uncompressed_bytes = 20 * 1024 * 1024
    external_source_max_pixels = 1_000_000
    content_filtration = False
    steal_pool_capacity = 200
    max_reg_num = 100

    def __init__(self, data_dir: Path, categories=None):
        self.data_dir = data_dir
        self.categories_dir = data_dir / "categories"
        self.pending_dir = data_dir / "pending"
        self.categories = list(categories or [])
        self.category_info: dict[str, dict] = {}
        self.characters: list[str] = []
        self.character_info: dict[str, dict] = {}

    def get_categories(self):
        return list(self.categories) or ["happy", "sad", "confused"]

    def get_category_info(self):
        return [
            {
                "key": key,
                "name": str((self.category_info.get(key) or {}).get("name") or key),
                "desc": str((self.category_info.get(key) or {}).get("desc") or ""),
            }
            for key in self.get_categories()
        ]

    def get_characters(self):
        return list(self.characters)

    def get_character_info_list(self):
        return [
            {
                "key": key,
                "name": str((self.character_info.get(key) or {}).get("name") or key),
                "desc": str((self.character_info.get(key) or {}).get("desc") or ""),
            }
            for key in self.get_characters()
        ]

    def normalize_character_key(self, value):
        return str(value or "").strip().lower().replace(" ", "_")

    def normalize_category_strict(self, value):
        return value if value in self.get_categories() else None

    def closest_category(self, value):
        return self.get_categories()[0]

    def ensure_category_dir(self, category):
        target = self.categories_dir / str(category)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def ensure_category_dirs(self, categories):
        for category in categories or []:
            self.ensure_category_dir(category)

    def save_categories(self):
        return None

    def save_category_info(self):
        return None

    def save_characters(self):
        return None

    def save_character_info(self):
        return None


def _plugin(data_dir: Path, db: DatabaseService, categories=None) -> SimpleNamespace:
    config = _Config(data_dir, categories)

    def update_config(payload):
        if isinstance(payload, dict) and "categories" in payload:
            config.categories = list(payload["categories"])

    return SimpleNamespace(
        base_dir=data_dir,
        db_service=db,
        plugin_config=config,
        meme_selector=None,
        update_config=update_config,
    )


def _seed_source(tmp_path: Path) -> tuple[SimpleNamespace, DatabaseService]:
    """造一个带完整元数据的图库 + 待审核池 + 黑名单的源实例。"""
    data_dir = tmp_path / "src-data"
    data_dir.mkdir()
    db = DatabaseService(data_dir / "emoji.db")

    happy_dir = data_dir / "categories" / "开心"
    sad_dir = data_dir / "categories" / "难过"
    happy_dir.mkdir(parents=True)
    sad_dir.mkdir(parents=True)
    img1 = happy_dir / "a.png"
    img2 = sad_dir / "b.png"
    img1.write_bytes(_png_bytes((255, 0, 0)))
    img2.write_bytes(_png_bytes((0, 200, 0)))

    asyncio.run(
        db.insert_batch(
            [
                {
                    "path": str(img1),
                    "hash": "libhash1",
                    "phash": "ph1",
                    "category": "开心",
                    "desc": "开心表情",
                    "tags": ["开心", "大笑"],
                    "scenes": ["聊天"],
                    "emotions": ["开心"],
                    "overlay_text": "好耶",
                    "character": "neurosama",
                    "work": "孤独摇滚",
                    "scope_mode": "local",
                    "origin_target": "group:123",
                    "source": "auto",
                    "source_url": "https://example.org/a.png",
                    "original_name": "a.png",
                    "width": 12,
                    "height": 8,
                    "format": "png",
                    "bytes": img1.stat().st_size,
                    "add_method": "auto_steal",
                    "retention_class": "native",
                    "created_at": 1700000000,
                    "reviewed_at": 1700000100,
                    "use_count": 7,
                    "last_used_at": 1700000200,
                    "is_favorite": 1,
                },
                {
                    "path": str(img2),
                    "hash": "libhash2",
                    "category": "难过",
                    "desc": "难过表情",
                    "tags": ["难过"],
                    "created_at": 1700000300,
                },
            ]
        )
    )

    pending_dir = data_dir / "pending"
    pending_dir.mkdir()
    pending_img = pending_dir / "p.png"
    pending_img.write_bytes(_png_bytes((0, 0, 255)))
    asyncio.run(
        db.insert_pending(
            {
                "path": str(pending_img),
                "hash": "pendhash1",
                "phash": "pp1",
                "category": "开心",
                "desc": "待审核表情",
                "tags": ["待审"],
                "scenes": ["群聊"],
                "emotions": ["开心"],
                "character": "neurosama",
                "work": "孤独摇滚",
                "scope_mode": "public",
                "source": "auto",
                "origin_target": "group:555",
                "created_at": 1700000400,
            }
        )
    )
    asyncio.run(db.add_blacklist("blackhash1", 1700000500))
    return _plugin(data_dir, db, categories=["开心", "难过"]), db


def _export_zip(tmp_path: Path, *, include_pending=True, include_blacklist=True) -> tuple[Path, SimpleNamespace, DatabaseService]:
    plugin, db = _seed_source(tmp_path)
    service = BackupService(plugin)
    payload = service.collect(
        include_pending=include_pending, include_blacklist=include_blacklist
    )
    target = tmp_path / "backup.zip"
    stats = service.write_zip(
        target, payload, plugin_version=service.plugin_version(), filters={"scope": "all"}
    )
    assert stats["library"] == 2
    return target, plugin, db


class TestExport:
    def test_export_writes_full_metadata(self, tmp_path: Path):
        target, plugin, _db = _export_zip(tmp_path)
        service = BackupService(plugin)
        assert service.plugin_version()

        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            assert {"manifest.json", "magpie_export.json", "semantic_metadata.json"} <= names
            assert any(name.startswith("memes/开心/") for name in names)
            assert any(name.startswith("pending/") for name in names)

            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["format"] == "magpie-backup"
            assert manifest["schema"] == "magpie-backup/1"
            assert manifest["counts"] == {"library": 2, "pending": 1, "blacklist": 1}
            assert {item["key"] for item in manifest["categories"]} == {"开心", "难过"}

            export = json.loads(archive.read("magpie_export.json"))
            assert export["schema"] == "magpie-backup/1"
            assert len(export["library"]) == 2
            assert export["blacklist"] == [{"hash": "blackhash1", "created_at": 1700000500}]
            entry = next(item for item in export["library"] if item["hash"] == "libhash1")
            assert entry["is_favorite"] == 1
            assert entry["use_count"] == 7
            assert entry["last_used_at"] == 1700000200
            assert entry["created_at"] == 1700000000
            assert entry["phash"] == "ph1"
            assert entry["scope_mode"] == "local"
            assert entry["origin_target"] == "group:123"
            assert entry["character"] == "neurosama"
            assert entry["retention_class"] == "native"
            assert entry["file"].startswith("memes/开心/")

            semantic = json.loads(archive.read("semantic_metadata.json"))
            assert semantic[entry["file"]]["description"] == "开心表情"

    def test_export_filter_and_optional_sections(self, tmp_path: Path):
        plugin, _db = _seed_source(tmp_path)
        service = BackupService(plugin)
        payload = service.collect(hashes={"libhash1"})
        assert [entry["hash"] for entry in payload["library"]] == ["libhash1"]
        assert payload["pending"] == [] and payload["blacklist"] == []

        target = tmp_path / "filtered.zip"
        service.write_zip(target, payload, plugin_version="")
        with zipfile.ZipFile(target) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["counts"]["library"] == 1
            assert manifest["counts"]["pending"] == 0


class TestRoundTrip:
    def test_backup_inspects_as_magpie_backup(self, tmp_path: Path):
        target, _plugin_src, _db = _export_zip(tmp_path)
        inspection = PackSource(target, max_items=100).inspect()
        assert inspection.backup is True
        assert inspection.backup_info["blacklist_count"] == 1
        assert len(inspection.pending_items) == 1
        assert len(inspection.items) == 2
        entry = next(item for item in inspection.items if item.metadata.get("hash") == "libhash1")
        assert entry.metadata["is_favorite"] == 1
        assert entry.metadata["use_count"] == 7
        assert entry.category == "开心"

    def test_import_restores_metadata_and_auto_creates(self, tmp_path: Path):
        target, _src_plugin, _src_db = _export_zip(tmp_path)

        dest_dir = tmp_path / "dest-data"
        dest_dir.mkdir()
        dest_db = DatabaseService(dest_dir / "emoji.db")
        dest_plugin = _plugin(dest_dir, dest_db, categories=["happy"])
        service = SourceService(dest_plugin)

        job = asyncio.run(
            service.import_now(
                {
                    "source_type": "meme_pack",
                    "path": str(target),
                    "merge_existing": False,
                    "restore_pending": True,
                    "restore_blacklist": True,
                }
            )
        )

        assert job["status"] == "completed"
        assert job["imported"] == 2
        assert job["pending_restored"] == 1
        assert job["blacklist_restored"] == 1

        # 备份包里的分类 / 角色自动创建
        assert "开心" in dest_plugin.plugin_config.get_categories()
        assert "neurosama" in dest_plugin.plugin_config.get_characters()
        assert dest_db.blacklisted_hashes() == {"blackhash1"}
        assert dest_db.count_pending() == 1

        index = dest_db.get_index_cache_readonly()
        restored = next(
            meta for meta in index.values() if meta.get("desc") == "开心表情"
        )
        assert restored["tags"] == ["开心", "大笑"]
        assert restored["scenes"] == ["聊天"]
        assert restored["emotions"] == ["开心"]
        assert restored["overlay_text"] == "好耶"
        assert restored["character"] == "neurosama"
        assert restored["work"] == "孤独摇滚"
        assert restored["is_favorite"] == 1
        assert restored["use_count"] == 7
        assert restored["last_used_at"] == 1700000200
        assert restored["created_at"] == 1700000000
        assert restored["phash"] == "ph1"
        assert restored["scope_mode"] == "local"
        assert restored["origin_target"] == "group:123"
        assert restored["retention_class"] == "native"
        assert restored["add_method"] == "auto_steal"

        pending_rows, _total, _counts = dest_db.get_pending_paginated(page=1, page_size=10)
        assert pending_rows[0]["desc"] == "待审核表情"
        assert pending_rows[0]["character"] == "neurosama"

    def test_reimport_is_additive_and_merge_only_fills_blanks(self, tmp_path: Path):
        target, _src_plugin, _src_db = _export_zip(tmp_path)
        dest_dir = tmp_path / "dest-data"
        dest_dir.mkdir()
        dest_db = DatabaseService(dest_dir / "emoji.db")
        dest_plugin = _plugin(dest_dir, dest_db, categories=["happy"])
        service = SourceService(dest_plugin)
        spec = {
            "source_type": "meme_pack",
            "path": str(target),
            "restore_pending": True,
            "restore_blacklist": True,
        }

        first = asyncio.run(service.import_now(dict(spec)))
        assert first["imported"] == 2

        # 再导入一次：图片已存在 → 只算重复，不覆盖、不重复建
        second = asyncio.run(service.import_now(dict(spec)))
        assert second["imported"] == 0
        assert second["duplicates"] >= 2
        assert second["pending_restored"] == 0
        assert dest_db.count_total() == 2
        assert dest_db.count_pending() == 1

        path, meta = next(
            (path, meta)
            for path, meta in dest_db.get_index_cache_readonly().items()
            if meta.get("desc") == "开心表情"
        )

        # 清空描述 + 取消收藏：只补空字段模式应填回来
        asyncio.run(dest_db.update_path(path, {"desc": "", "is_favorite": 0}))
        merged_job = asyncio.run(
            service.import_now({**spec, "merge_existing": True})
        )
        assert merged_job["merged"] >= 1
        refreshed = dest_db.get_emoji(path)
        assert refreshed["desc"] == "开心表情"
        assert refreshed["is_favorite"] == 1

        # 已有内容（手写描述）绝不被覆盖
        asyncio.run(dest_db.update_path(path, {"desc": "手工描述"}))
        again = asyncio.run(service.import_now({**spec, "merge_existing": True}))
        assert again["duplicates"] >= 1
        assert dest_db.get_emoji(path)["desc"] == "手工描述"

    def test_pending_restore_respects_capacity(self, tmp_path: Path):
        target, _src_plugin, _src_db = _export_zip(tmp_path)
        dest_dir = tmp_path / "dest-data"
        dest_dir.mkdir()
        dest_db = DatabaseService(dest_dir / "emoji.db")
        dest_plugin = _plugin(dest_dir, dest_db, categories=["happy"])
        dest_plugin.plugin_config.steal_pool_capacity = 0
        service = SourceService(dest_plugin)

        job = asyncio.run(
            service.import_now(
                {
                    "source_type": "meme_pack",
                    "path": str(target),
                    "restore_pending": True,
                    "restore_blacklist": False,
                }
            )
        )

        assert job["status"] == "completed"
        assert job["pending_restored"] == 0
        assert job["pending_skipped"] == 1
        assert job["notes"]
