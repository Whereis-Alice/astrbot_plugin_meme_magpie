"""表情库备份导出（magpie-backup 包）。

导出端的产物与 `core/sources` 的外部源导入管线对接：同一个包可以被
「外部源 → 上传压缩包」重新导入，完整回填分析好的元数据（描述 / 标签 /
场景 / 情绪 / 角色 / 作品 / 收藏 / 使用统计等），待审核池与黑名单可选。
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

BACKUP_FORMAT = "magpie-backup"
BACKUP_SCHEMA = "magpie-backup/1"
PLUGIN_NAME = "astrbot_plugin_meme_magpie"

_UNSAFE_SEGMENT_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_segment(value: Any, *, fallback: str = "unknown", limit: int = 60) -> str:
    """把分类名等文本清洗成安全的 zip 目录段。"""
    cleaned = _UNSAFE_SEGMENT_RE.sub("_", str(value or "").strip()).strip(" .")
    if cleaned in {"", ".", ".."}:
        cleaned = fallback
    return cleaned[:limit] or fallback


ProgressCallback = Callable[[int, str], None]


class BackupService:
    """收集图库数据并打成 magpie-backup 压缩包。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @property
    def _db(self):
        return getattr(self.plugin, "db_service", None)

    def plugin_version(self) -> str:
        """从 metadata.yaml 读当前版本号，写进备份清单方便排查。"""
        try:
            text = (
                Path(__file__).resolve().parents[2] / "metadata.yaml"
            ).read_text(encoding="utf-8")
        except OSError:
            return ""
        for line in text.splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip()[:20]
        return ""

    # ── 收集 ──────────────────────────────────────────────────

    def collect(
        self,
        *,
        hashes: set[str] | None = None,
        include_pending: bool = False,
        include_blacklist: bool = False,
    ) -> dict[str, Any]:
        """按筛选收集待导出的条目（只读元数据，图片字节在打包时再读）。"""
        db = self._db
        library: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        missing = 0

        index: dict[str, Any] = {}
        if db is not None and hasattr(db, "get_index_cache_readonly"):
            index = db.get_index_cache_readonly() or {}
        for path, meta in index.items():
            if not isinstance(meta, dict):
                continue
            img_hash = str(meta.get("hash") or "")
            if not img_hash or (hashes is not None and img_hash not in hashes):
                continue
            file_path = Path(str(path))
            if not file_path.is_file():
                missing += 1
                continue
            library.append(self._library_entry(file_path, meta))

        if (
            include_pending
            and db is not None
            and hasattr(db, "count_pending")
            and hasattr(db, "get_pending_paginated")
        ):
            total = int(db.count_pending() or 0)
            if total > 0:
                rows, _, _ = db.get_pending_paginated(page=1, page_size=total)
                for row in rows:
                    file_path = Path(str(row.get("path") or ""))
                    if not row.get("hash") or not file_path.is_file():
                        missing += 1
                        continue
                    pending.append(self._pending_entry(file_path, row))

        blacklist: list[dict[str, Any]] = []
        if include_blacklist and db is not None and hasattr(db, "get_blacklist_entries"):
            blacklist = list(db.get_blacklist_entries() or [])

        library.sort(key=lambda entry: (int(entry.get("created_at") or 0), entry.get("hash") or ""))
        return {
            "library": library,
            "pending": pending,
            "blacklist": blacklist,
            "missing": missing,
        }

    def _library_entry(self, file_path: Path, meta: dict[str, Any]) -> dict[str, Any]:
        img_hash = str(meta.get("hash") or "")
        suffix = file_path.suffix.lower() or ".png"
        category = str(meta.get("category") or "unknown")
        entry = {
            "id": img_hash,
            "file": f"memes/{safe_segment(category)}/{img_hash}{suffix}",
            "hash": img_hash,
            "phash": str(meta.get("phash") or ""),
            "category": category,
            "tags": [str(tag) for tag in (meta.get("tags") or []) if str(tag).strip()],
            "desc": str(meta.get("desc") or ""),
            "scenes": [str(scene) for scene in (meta.get("scenes") or []) if str(scene).strip()],
            "overlay_text": str(meta.get("overlay_text") or ""),
            "emotions": [str(item) for item in (meta.get("emotions") or []) if str(item).strip()],
            "character": str(meta.get("character") or ""),
            "work": str(meta.get("work") or ""),
            "scope_mode": str(meta.get("scope_mode") or "public"),
            "origin_target": str(meta.get("origin_target") or ""),
            "source": str(meta.get("source") or ""),
            "source_url": str(meta.get("source_url") or ""),
            "original_name": str(meta.get("original_name") or file_path.name),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "format": str(meta.get("format") or suffix.lstrip(".")),
            "bytes": int(meta.get("bytes") or 0) or file_path.stat().st_size,
            "add_method": str(meta.get("add_method") or ""),
            "retention_class": str(meta.get("retention_class") or "native"),
            "created_at": int(meta.get("created_at") or 0),
            "reviewed_at": int(meta.get("reviewed_at") or 0),
            "use_count": int(meta.get("use_count") or 0),
            "last_used_at": int(meta.get("last_used_at") or 0),
            "is_favorite": 1 if meta.get("is_favorite") else 0,
            "_local_path": str(file_path),
        }
        return entry

    def _pending_entry(self, file_path: Path, row: dict[str, Any]) -> dict[str, Any]:
        img_hash = str(row.get("hash") or "")
        suffix = file_path.suffix.lower() or ".png"
        category = str(row.get("category") or "unknown")
        return {
            "id": img_hash,
            "file": f"pending/{img_hash}{suffix}",
            "hash": img_hash,
            "phash": str(row.get("phash") or ""),
            "category": category,
            "tags": [str(tag) for tag in (row.get("tags") or []) if str(tag).strip()],
            "desc": str(row.get("desc") or ""),
            "scenes": [str(scene) for scene in (row.get("scenes") or []) if str(scene).strip()],
            "overlay_text": str(row.get("overlay_text") or ""),
            "emotions": [str(item) for item in (row.get("emotions") or []) if str(item).strip()],
            "character": str(row.get("character") or ""),
            "work": str(row.get("work") or ""),
            "scope_mode": str(row.get("scope_mode") or "public"),
            "origin_target": str(row.get("origin_target") or ""),
            "source": str(row.get("source") or ""),
            "source_url": str(row.get("source_url") or ""),
            "original_name": str(row.get("original_name") or file_path.name),
            "width": row.get("width"),
            "height": row.get("height"),
            "format": str(row.get("format") or suffix.lstrip(".")),
            "bytes": int(row.get("bytes") or 0) or file_path.stat().st_size,
            "add_method": str(row.get("add_method") or ""),
            "retention_class": str(row.get("retention_class") or "native"),
            "created_at": int(row.get("created_at") or 0),
            "_local_path": str(file_path),
        }

    # ── 打包 ──────────────────────────────────────────────────

    def write_zip(
        self,
        dest: Path,
        payload: dict[str, Any],
        *,
        plugin_version: str = "",
        filters: dict[str, Any] | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """把收集好的条目写成 zip；图片原样存放，元数据走 JSON。"""
        library = list(payload.get("library") or [])
        pending = list(payload.get("pending") or [])
        blacklist = list(payload.get("blacklist") or [])
        total = len(library) + len(pending)
        processed = 0
        written = {"library": 0, "pending": 0, "missing": 0}

        export_json: dict[str, Any] = {
            "schema": BACKUP_SCHEMA,
            "plugin": PLUGIN_NAME,
            "plugin_version": plugin_version,
            "exported_at": int(time.time()),
            "filters": dict(filters or {}),
            "library": [],
            "pending": [],
            "blacklist": blacklist,
        }
        semantic: dict[str, dict[str, Any]] = {}
        seen_names: set[str] = set()

        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", allowZip64=True) as archive:
            for bucket, entries in (("library", library), ("pending", pending)):
                for entry in entries:
                    local_path = Path(str(entry.get("_local_path") or ""))
                    record = {key: value for key, value in entry.items() if not key.startswith("_")}
                    try:
                        data = local_path.read_bytes()
                    except OSError as exc:
                        logger.warning(f"[Backup] 导出跳过无法读取的文件 {local_path}: {exc}")
                        written["missing"] += 1
                        processed += 1
                        if progress:
                            progress(processed, record.get("file") or "")
                        continue
                    member = self._unique_member(str(record.get("file") or ""), seen_names)
                    record["file"] = member
                    archive.writestr(member, data, compress_type=zipfile.ZIP_STORED)
                    export_json[bucket].append(record)
                    semantic[member] = {
                        "id": record.get("hash"),
                        "category": record.get("category"),
                        "description": record.get("desc"),
                        "visible_text": record.get("overlay_text"),
                        "tags": record.get("tags"),
                        "scenes": record.get("scenes"),
                        "emotions": record.get("emotions"),
                        "character": record.get("character"),
                        "work": record.get("work"),
                        "source_url": record.get("source_url"),
                    }
                    written[bucket] += 1
                    processed += 1
                    if progress:
                        progress(processed, member)

            # 顺序很重要：读取器按 zip 成员顺序建元数据索引，后者覆盖前者。
            # 先写兼容子集，再写完整记录，保证 magpie_export.json 是最终生效的那份。
            archive.writestr(
                "semantic_metadata.json",
                json.dumps(semantic, ensure_ascii=False, indent=2),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            manifest = self._build_manifest(
                library_count=written["library"],
                pending_count=written["pending"],
                blacklist_count=len(blacklist),
                plugin_version=plugin_version,
                filters=filters,
                export_json=export_json,
            )
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            archive.writestr(
                "magpie_export.json",
                json.dumps(export_json, ensure_ascii=False, indent=2),
                compress_type=zipfile.ZIP_DEFLATED,
            )

        return {
            "library": written["library"],
            "pending": written["pending"],
            "blacklist": len(blacklist),
            "missing": written["missing"],
            "total": total,
            "bytes": dest.stat().st_size if dest.is_file() else 0,
        }

    @staticmethod
    def _unique_member(member: str, seen: set[str]) -> str:
        candidate = member or "memes/unknown/image.png"
        if candidate not in seen:
            seen.add(candidate)
            return candidate
        stem, suffix = posixpath_split(candidate)
        index = 1
        while True:
            candidate = f"{stem}_{index}{suffix}"
            if candidate not in seen:
                seen.add(candidate)
                return candidate
            index += 1

    def _build_manifest(
        self,
        *,
        library_count: int,
        pending_count: int,
        blacklist_count: int,
        plugin_version: str,
        filters: dict[str, Any] | None,
        export_json: dict[str, Any],
    ) -> dict[str, Any]:
        stamp = int(time.time())
        name_stamp = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
        categories: list[dict[str, str]] = []
        characters: list[dict[str, str]] = []
        config = getattr(self.plugin, "plugin_config", None)
        if config is not None and hasattr(config, "get_category_info"):
            try:
                categories = [
                    {
                        "key": str(item.get("key") or ""),
                        "name": str(item.get("name") or item.get("key") or ""),
                        "desc": str(item.get("desc") or ""),
                    }
                    for item in (config.get_category_info() or [])
                    if item.get("key")
                ]
            except Exception:
                categories = []
        if config is not None and hasattr(config, "get_character_info_list"):
            try:
                characters = [
                    {
                        "key": str(item.get("key") or ""),
                        "name": str(item.get("name") or item.get("key") or ""),
                        "desc": str(item.get("desc") or ""),
                    }
                    for item in (config.get_character_info_list() or [])
                    if item.get("key")
                ]
            except Exception:
                characters = []
        return {
            "id": f"magpie-backup-{stamp}",
            "name": f"meme神偷备份 {name_stamp}",
            "format": BACKUP_FORMAT,
            "schema": BACKUP_SCHEMA,
            "plugin": PLUGIN_NAME,
            "plugin_version": plugin_version,
            "exported_at": stamp,
            "counts": {
                "library": library_count,
                "pending": pending_count,
                "blacklist": blacklist_count,
            },
            "filters": dict(filters or {}),
            "categories": categories,
            "characters": characters,
            "license": "plugin-local-backup",
            "attribution": "meme神偷 (astrbot_plugin_meme_magpie)",
        }


def posixpath_split(member: str) -> tuple[str, str]:
    """把 zip 成员名拆成 (stem, suffix)，只按 / 处理，不碰盘符。"""
    index = member.rfind(".")
    slash = member.rfind("/")
    if index <= slash:
        return member, ""
    return member[:index], member[index:]
