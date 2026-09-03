"""旧插件（astrbot_plugin_stealer）数据迁移服务。

把原插件的数据目录整体接管到本插件：图片文件、SQLite 记录、标签 / 场景、
语义向量、黑名单、自定义分类与角色，以及可安全沿用的插件配置项。

三条设计原则：

1. **默认预演**：不带 ``apply`` 时只统计"会发生什么"，不写入任何数据；
2. **非破坏性**：默认复制文件而非移动，旧插件数据保持原样，可随时回滚；
3. **幂等**：重复执行不会产生重复记录（按图片 hash 与目标路径双重去重）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any, ClassVar

from astrbot.api import logger

from ..util.command_hint import DEFAULT_WAKE_PREFIX, format_command

LEGACY_PLUGIN_NAME = "astrbot_plugin_stealer"

#: 自动探测旧插件数据目录时尝试的目录名（按优先级）。
LEGACY_DIR_CANDIDATES: tuple[str, ...] = (
    "astrbot_plugin_stealer",
    "astrbot_plugin_emoji_stealer",
    "astrbot_plugin_meme_stealer",
)

#: 建立"文件名 -> 实际位置"索引时扫描的子目录（顺序即优先级）。
_SCAN_SUBDIRS: tuple[str, ...] = ("categories", "pending", "raw")

#: 需要合并的 JSON 资产：(文件名, PluginConfig 属性名)。
_JSON_ASSETS: tuple[tuple[str, str], ...] = (
    ("categories.json", "categories"),
    ("category_info.json", "category_info"),
    ("characters.json", "characters"),
    ("character_info.json", "character_info"),
)

_INSERT_CHUNK = 200
_MAX_ERRORS = 50
_DEFAULT_CATEGORY = "其他"
_MULTI_SPLIT_RE = re.compile(r"[,，、|/;；\s]+")


@dataclass
class MigrationReport:
    """迁移结果报告，预演与实际执行共用同一结构。"""

    dry_run: bool = True
    source_dir: str = ""
    target_dir: str = ""
    move: bool = False

    emojis_imported: int = 0
    emojis_skipped_duplicate: int = 0
    emojis_skipped_blacklisted: int = 0
    pending_imported: int = 0
    pending_skipped_duplicate: int = 0
    tags_imported: int = 0
    scenes_imported: int = 0
    embeddings_imported: int = 0
    blacklist_imported: int = 0
    files_copied: int = 0
    missing_files: int = 0
    categories_added: int = 0
    characters_added: int = 0
    config_keys_imported: int = 0

    errors: list[str] = field(default_factory=list)
    added_category_names: list[str] = field(default_factory=list)
    added_character_names: list[str] = field(default_factory=list)
    imported_config_keys: list[str] = field(default_factory=list)

    _COUNTER_FIELDS: ClassVar[tuple[str, ...]] = (
        "emojis_imported",
        "emojis_skipped_duplicate",
        "emojis_skipped_blacklisted",
        "pending_imported",
        "pending_skipped_duplicate",
        "tags_imported",
        "scenes_imported",
        "embeddings_imported",
        "blacklist_imported",
        "files_copied",
        "missing_files",
        "categories_added",
        "characters_added",
        "config_keys_imported",
    )

    @property
    def total_imported(self) -> int:
        """成功（或预计）迁入的图片总数。"""
        return self.emojis_imported + self.pending_imported

    def note(self, message: str) -> None:
        """记录一条问题说明，超过上限后静默丢弃，避免报告爆炸。"""
        text = str(message).strip()
        if text and len(self.errors) < _MAX_ERRORS:
            self.errors.append(text)

    def as_dict(self) -> dict[str, Any]:
        """转成 JSON 友好的字典，供 WebUI / 日志使用。"""
        data: dict[str, Any] = {
            "dry_run": self.dry_run,
            "source_dir": self.source_dir,
            "target_dir": self.target_dir,
            "move": self.move,
            "total_imported": self.total_imported,
        }
        for key in self._COUNTER_FIELDS:
            data[key] = int(getattr(self, key))
        data["errors"] = list(self.errors)
        data["added_category_names"] = list(self.added_category_names)
        data["added_character_names"] = list(self.added_character_names)
        data["imported_config_keys"] = list(self.imported_config_keys)
        return data

    def summary(self, cmd: Callable[[str], str] | None = None) -> str:
        """生成给聊天窗口看的中文摘要（控制在 20 行内）。

        Args:
            cmd: 把子命令渲染成"可直接复制发送的完整命令"的函数，通常传
                `plugin.cmd`。不传时按 AstrBot 出厂默认唤醒前缀 `/` 渲染。
        """

        def _default_cmd(sub: str) -> str:
            return format_command(sub, DEFAULT_WAKE_PREFIX)

        render = cmd or _default_cmd
        verb = "待迁入" if self.dry_run else "已迁入"
        lines = [
            "【迁移预演】下面是「如果执行」会发生的变化。这一步只读不写，"
            "旧插件和本插件的数据都没有被改动。"
            if self.dry_run
            else "【迁移完成】以下是实际写入的结果。",
            f"来源：{self.source_dir or '未找到旧插件数据目录'}",
        ]
        if self.target_dir:
            lines.append(f"去向：{self.target_dir}")
        if not self.source_dir:
            if self.errors:
                lines.extend(f"  · {item}" for item in self.errors[:3])
            return "\n".join(lines)

        lines.append(
            f"{verb}表情包：{self.emojis_imported} 张"
            f"（跳过重复 {self.emojis_skipped_duplicate}，黑名单 {self.emojis_skipped_blacklisted}）"
        )
        lines.append(
            f"{verb}待审核：{self.pending_imported} 张"
            f"（跳过重复 {self.pending_skipped_duplicate}）"
        )
        lines.append(f"标签 {self.tags_imported} 个 / 场景 {self.scenes_imported} 个")
        lines.append(
            f"语义向量 {self.embeddings_imported} 条 / 黑名单 {self.blacklist_imported} 条"
        )
        lines.append(
            f"图片文件：{self.files_copied} 个（{'移动' if self.move else '复制'}）"
            f"，源文件缺失 {self.missing_files} 个"
        )
        lines.append(
            f"新增分类 {self.categories_added} 个 / 新增角色 {self.characters_added} 个"
            f" / 沿用旧插件设置 {self.config_keys_imported} 项"
        )
        if self.pending_imported > 0:
            lines.append(
                "（待审核 = 旧插件里还没分类的图，迁移后可在 WebUI 里补分类和标签）"
            )
        if self.errors:
            lines.append(f"另有 {len(self.errors)} 处提示，前 3 条：")
            lines.extend(f"  · {item}" for item in self.errors[:3])
        if self.dry_run:
            lines.append(f"确认无误后执行：{render('migrate apply')}")
            lines.append(f"想省一份磁盘、直接搬走旧文件：{render('migrate move')}")
        elif self.move:
            lines.append(
                "已选择移动模式，旧插件的图片文件已被搬走（数据库记录仍保留）。"
                f"建议接着执行 {render('rebuild_index')}。"
            )
        else:
            lines.append(
                "旧插件数据一个字节都没动，可随时回滚。"
                f"建议接着执行 {render('rebuild_index')}。"
            )
        return "\n".join(lines)


@dataclass
class _PlannedImage:
    """一条已规划好的图片迁移动作（预演阶段即完全确定）。"""

    legacy_path: str
    src_file: Path
    dst_file: Path
    row: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    scenes: list[str] = field(default_factory=list)
    pending: bool = False


class MigrationService:
    """从旧插件 ``astrbot_plugin_stealer`` 迁移数据到本插件。"""

    #: 这些键由 JSON 资产合并流程单独处理，不走配置沿用。
    _CONFIG_SKIP_KEYS = frozenset(
        {"categories", "category_info", "characters", "character_info"}
    )

    #: 旧记录里不能直接照搬的列。
    _ROW_DROP_KEYS = frozenset({"id", "path", "tags_text", "scenes_text", "review_status"})

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ── 基础属性 ─────────────────────────────────────────────

    @property
    def config(self) -> Any:
        return self.plugin.plugin_config

    @property
    def target_dir(self) -> Path:
        """本插件的数据目录。"""
        return Path(self.config.data_dir)

    @property
    def plugin_root(self) -> Path:
        """插件代码根目录（用于读取 _conf_schema.json）。"""
        return Path(__file__).resolve().parents[2]

    def candidate_source_dirs(self) -> list[Path]:
        """自动探测时的候选目录：与本插件数据目录同级。"""
        parent = self.target_dir.parent
        return [parent / name for name in LEGACY_DIR_CANDIDATES]

    # ── 旧数据探测 ───────────────────────────────────────────

    @staticmethod
    def _legacy_db_path(source: Path) -> Path:
        return source / "cache" / "emoji.db"

    def _looks_like_legacy_dir(self, candidate: Path) -> bool:
        """判定一个目录是否像旧插件的数据目录。"""
        try:
            if not candidate.is_dir():
                return False
            if candidate.resolve() == self.target_dir.resolve():
                return False
        except OSError:
            return False
        return self._legacy_db_path(candidate).is_file() or (candidate / "categories").is_dir()

    def _resolve_source(self, source: str | Path | None = None) -> Path | None:
        """把用户输入（或空值）解析成真实的旧数据目录。"""
        if source:
            raw = Path(str(source).strip().strip('"').strip("'")).expanduser()
            if self._looks_like_legacy_dir(raw):
                return raw
            for name in LEGACY_DIR_CANDIDATES:
                nested = raw / name
                if self._looks_like_legacy_dir(nested):
                    return nested
            return None
        for candidate in self.candidate_source_dirs():
            if self._looks_like_legacy_dir(candidate):
                return candidate
        return None

    def detect_legacy(self, source: str | Path | None = None) -> dict[str, Any]:
        """探测旧插件数据，返回概况（不做任何写入）。"""
        resolved = self._resolve_source(source)
        if resolved is None:
            searched = "、".join(name for name in LEGACY_DIR_CANDIDATES)
            return {
                "found": False,
                "source_dir": "",
                "db_path": "",
                "emoji_count": 0,
                "pending_count": 0,
                "file_count": 0,
                "reason": (
                    f"未找到旧插件数据目录。已在 {self.target_dir.parent} 下查找：{searched}；"
                    "也可以手动指定目录，把路径直接写在命令末尾，"
                    "例如：mp migrate check D:/astrbot/data/plugin_data/xxx"
                ),
            }
        db_path = self._legacy_db_path(resolved)
        emoji_count, pending_count = self._count_legacy_rows(db_path)
        return {
            "found": True,
            "source_dir": str(resolved),
            "db_path": str(db_path) if db_path.is_file() else "",
            "emoji_count": emoji_count,
            "pending_count": pending_count,
            "file_count": sum(1 for _ in self._iter_legacy_files(resolved)),
            "reason": "",
        }

    @staticmethod
    def _iter_legacy_files(source: Path):
        """遍历旧目录里所有图片候选文件。"""
        for sub in _SCAN_SUBDIRS:
            root = source / sub
            if not root.is_dir():
                continue
            try:
                for item in root.rglob("*"):
                    if item.is_file():
                        yield item
            except OSError:
                continue

    def _index_legacy_files(self, source: Path) -> dict[str, list[Path]]:
        """建立"文件名 -> 实际位置"索引。

        旧库里的 ``path`` 存的是绝对路径，用户整体搬过目录后就失效了；
        改用文件名定位可以抗这种情况。
        """
        index: dict[str, list[Path]] = {}
        for item in self._iter_legacy_files(source):
            index.setdefault(item.name, []).append(item)
        return index

    # ── SQLite 读取 ──────────────────────────────────────────

    @staticmethod
    @contextmanager
    def _open_legacy_db(db_path: Path):
        """把旧库复制到临时目录后再打开。

        直接以只读方式打开带 WAL 的库会失败（需要写 -wal），
        复制一份既安全又不会污染旧插件数据。
        """
        tmp_dir = Path(tempfile.mkdtemp(prefix="magpie_migrate_"))
        conn: sqlite3.Connection | None = None
        try:
            target = tmp_dir / "legacy.db"
            shutil.copy2(db_path, target)
            for suffix in ("-wal", "-shm"):
                side = Path(str(db_path) + suffix)
                if side.is_file():
                    shutil.copy2(side, Path(str(target) + suffix))
            conn = sqlite3.connect(str(target))
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {str(row[0]) for row in rows}

    @staticmethod
    def _scalar(conn: sqlite3.Connection, sql: str) -> int:
        try:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            return 0

    def _count_legacy_rows(self, db_path: Path) -> tuple[int, int]:
        if not db_path.is_file():
            return 0, 0
        try:
            with self._open_legacy_db(db_path) as conn:
                names = self._table_names(conn)
                emoji = self._scalar(conn, "SELECT COUNT(*) FROM emoji") if "emoji" in names else 0
                pending = (
                    self._scalar(conn, "SELECT COUNT(*) FROM emoji_pending")
                    if "emoji_pending" in names
                    else 0
                )
                return emoji, pending
        except Exception as exc:
            logger.warning(f"[迁移] 读取旧数据库失败: {exc}")
            return 0, 0
    def _read_legacy_snapshot(self, db_path: Path) -> dict[str, Any]:
        """一次性把旧库读进内存，兼容 v4 ~ v6 各版本表结构。"""
        snapshot: dict[str, Any] = {
            "emoji": [],
            "emoji_tags": {},
            "emoji_scenes": {},
            "emoji_pending": [],
            "pending_tags": {},
            "pending_scenes": {},
            "blacklist": {},
            "embeddings": {},
        }
        if not db_path.is_file():
            return snapshot

        with self._open_legacy_db(db_path) as conn:
            names = self._table_names(conn)
            if "emoji" in names:
                snapshot["emoji"] = [dict(row) for row in conn.execute("SELECT * FROM emoji")]
            if "emoji_pending" in names:
                snapshot["emoji_pending"] = [
                    dict(row) for row in conn.execute("SELECT * FROM emoji_pending")
                ]
            if "emoji_tag" in names:
                snapshot["emoji_tags"] = self._collect_pairs(conn, "SELECT path, tag FROM emoji_tag")
            if "emoji_scene" in names:
                snapshot["emoji_scenes"] = self._collect_pairs(
                    conn, "SELECT path, scene FROM emoji_scene"
                )
            if "emoji_pending_tag" in names:
                snapshot["pending_tags"] = self._collect_pairs(
                    conn, "SELECT path, tag FROM emoji_pending_tag"
                )
            if "emoji_pending_scene" in names:
                snapshot["pending_scenes"] = self._collect_pairs(
                    conn, "SELECT path, scene FROM emoji_pending_scene"
                )
            if "blacklist" in names:
                for row in conn.execute("SELECT hash, created_at FROM blacklist"):
                    hash_val = str(row["hash"] or "").strip()
                    if hash_val:
                        snapshot["blacklist"][hash_val] = int(row["created_at"] or 0)
            if "emoji_embedding" in names:
                for row in conn.execute(
                    "SELECT path, vector, dim, model_sig FROM emoji_embedding"
                ):
                    path = str(row["path"] or "")
                    vector = row["vector"]
                    if path and vector:
                        snapshot["embeddings"][path] = (
                            bytes(vector),
                            int(row["dim"] or 0),
                            str(row["model_sig"] or ""),
                        )
        return snapshot

    @staticmethod
    def _collect_pairs(conn: sqlite3.Connection, sql: str) -> dict[str, list[str]]:
        """把 (path, value) 关联表折叠成 path -> [value, ...]（去重保序）。"""
        out: dict[str, list[str]] = {}
        try:
            for row in conn.execute(sql):
                key = str(row[0] or "")
                value = str(row[1] or "").strip()
                if not key or not value:
                    continue
                bucket = out.setdefault(key, [])
                if value not in bucket:
                    bucket.append(value)
        except Exception as exc:
            logger.warning(f"[迁移] 读取关联表失败: {exc}")
        return out

    # ── 规划阶段（dry-run 与 apply 共用） ───────────────────

    def _plan_images(
        self,
        snapshot: dict[str, Any],
        file_index: dict[str, list[Path]],
        report: MigrationReport,
    ) -> list[_PlannedImage]:
        """把旧库每一行映射成一次明确的"从哪来、到哪去"动作。"""
        db = getattr(self.plugin, "db_service", None)
        source_root = Path(report.source_dir)
        seen_emoji = set(db.get_all_hashes()) if db else set()
        seen_pending = set(db.get_all_pending_hashes()) if db else set()
        blacklisted = set(db.blacklisted_hashes()) if db else set()
        blacklisted |= {h for h in snapshot["blacklist"] if h}
        taken = {str(path).lower() for path in (db.get_all_paths() if db else [])}
        plan: list[_PlannedImage] = []

        for pending in (False, True):
            rows = snapshot["emoji_pending" if pending else "emoji"]
            seen = seen_pending if pending else seen_emoji
            tag_map = snapshot["pending_tags" if pending else "emoji_tags"]
            scene_map = snapshot["pending_scenes" if pending else "emoji_scenes"]
            for row in rows:
                legacy_path = str(row.get("path") or "").strip()
                if not legacy_path:
                    continue
                hash_val = str(row.get("hash") or "").strip()
                if hash_val and hash_val in blacklisted:
                    report.emojis_skipped_blacklisted += 1
                    continue
                if hash_val and hash_val in seen:
                    if pending:
                        report.pending_skipped_duplicate += 1
                    else:
                        report.emojis_skipped_duplicate += 1
                    continue
                src_file = self._locate_source_file(legacy_path, file_index)
                if src_file is None:
                    report.missing_files += 1
                    report.note(f"源文件缺失：{legacy_path}")
                    continue
                category = self._normalize_category(row.get("category"))
                dst_file = self._dedupe_target(
                    self.target_dir
                    / self._target_relpath(src_file, source_root, category, pending=pending),
                    taken,
                )
                tags = self._merge_multi(tag_map.get(legacy_path), row.get("tags_text"))
                scenes = self._merge_multi(scene_map.get(legacy_path), row.get("scenes_text"))
                plan.append(
                    _PlannedImage(
                        legacy_path=legacy_path,
                        src_file=src_file,
                        dst_file=dst_file,
                        row=self._normalize_row(
                            row, category=category, path=str(dst_file), pending=pending
                        ),
                        tags=tags,
                        scenes=scenes,
                        pending=pending,
                    )
                )
                if hash_val:
                    seen.add(hash_val)
                if pending:
                    report.pending_imported += 1
                else:
                    report.emojis_imported += 1
                report.tags_imported += len(tags)
                report.scenes_imported += len(scenes)
        return plan
    @staticmethod
    def _locate_source_file(legacy_path: str, file_index: dict[str, list[Path]]) -> Path | None:
        """定位旧记录对应的真实文件：先信绝对路径，再按文件名回退。"""
        raw = Path(legacy_path)
        try:
            if raw.is_file():
                return raw
        except OSError:
            pass
        candidates = file_index.get(raw.name) or []
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        parent_name = raw.parent.name
        for item in candidates:
            if item.parent.name == parent_name:
                return item
        return candidates[0]

    @staticmethod
    def _target_relpath(src_file: Path, source: Path, category: str, *, pending: bool) -> Path:
        """决定文件在新数据目录下的相对位置。"""
        name = src_file.name
        if pending:
            return Path("pending") / name
        try:
            parts = src_file.relative_to(source).parts
        except ValueError:
            parts = (name,)
        if len(parts) >= 2 and parts[0] == "categories":
            return Path(*parts)
        return Path("categories") / category / name

    @staticmethod
    def _dedupe_target(dst: Path, taken: set[str]) -> Path:
        """目标路径冲突时追加 _mig1 ~ _mig999 后缀。"""
        key = str(dst).lower()
        if key not in taken:
            taken.add(key)
            return dst
        for index in range(1, 1000):
            candidate = dst.with_name(f"{dst.stem}_mig{index}{dst.suffix}")
            key = str(candidate).lower()
            if key not in taken:
                taken.add(key)
                return candidate
        taken.add(str(dst).lower())
        return dst

    def _normalize_category(self, value: Any) -> str:
        """归一化分类名；无法识别时保留原值，空值落到"其他"。"""
        raw = str(value or "").strip()
        if not raw:
            return _DEFAULT_CATEGORY
        try:
            normalized = self.config.normalize_category_strict(raw)
        except Exception:
            normalized = None
        return normalized or raw

    @staticmethod
    def _merge_multi(values: list[str] | None, text: Any) -> list[str]:
        """合并关联表与旧版 tags_text / scenes_text 两种存法。"""
        out: list[str] = []
        for item in values or []:
            item = str(item).strip()
            if item and item not in out:
                out.append(item)
        raw = str(text or "").strip()
        if raw:
            for piece in _MULTI_SPLIT_RE.split(raw):
                piece = piece.strip()
                if piece and piece not in out:
                    out.append(piece)
        return out

    def _normalize_row(
        self, row: dict[str, Any], *, category: str, path: str, pending: bool
    ) -> dict[str, Any]:
        """把旧行整理成本插件 DatabaseService 能直接吃的 meta。"""
        meta: dict[str, Any] = {
            key: value
            for key, value in row.items()
            if key not in self._ROW_DROP_KEYS and value is not None
        }
        meta["path"] = path
        meta["category"] = category
        meta["hash"] = str(row.get("hash") or "").strip()
        meta["scope_mode"] = str(row.get("scope_mode") or "").strip() or "public"
        meta["add_method"] = str(row.get("add_method") or "").strip() or "migrated"
        meta["source"] = str(row.get("source") or "").strip() or "migrated"
        meta["character"] = str(row.get("character") or "").strip()
        meta["work"] = str(row.get("work") or "").strip()
        meta["created_at"] = int(row.get("created_at") or int(time.time()))
        if not pending:
            meta["use_count"] = int(row.get("use_count") or 0)
            meta["last_used_at"] = int(row.get("last_used_at") or 0)
            meta["is_favorite"] = 1 if row.get("is_favorite") else 0
        return meta

    # ── JSON 资产与配置 ─────────────────────────────────────

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            if not path.is_file():
                return None
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            logger.warning(f"[迁移] 读取 {path.name} 失败: {exc}")
            return None

    def _load_schema(self) -> dict[str, Any]:
        data = self._read_json(self.plugin_root / "_conf_schema.json")
        return data if isinstance(data, dict) else {}

    def _plan_json_assets(self, source: Path, report: MigrationReport) -> dict[str, Any]:
        """合并自定义分类 / 角色：列表取并集，字典只补缺失键。"""
        planned: dict[str, Any] = {}
        for filename, attr in _JSON_ASSETS:
            data = self._read_json(source / filename)
            if data is None:
                continue
            current = getattr(self.config, attr, None)
            if isinstance(data, list) and isinstance(current, list):
                merged = list(current)
                added: list[str] = []
                for item in data:
                    text = str(item).strip()
                    if text and text not in merged:
                        merged.append(text)
                        added.append(text)
                if not added:
                    continue
                planned[attr] = merged
                if attr == "categories":
                    report.categories_added += len(added)
                    report.added_category_names.extend(added)
                elif attr == "characters":
                    report.characters_added += len(added)
                    report.added_character_names.extend(added)
            elif isinstance(data, dict) and isinstance(current, dict):
                merged_map = dict(current)
                changed = False
                for key, value in data.items():
                    if key not in merged_map:
                        merged_map[key] = value
                        changed = True
                if changed:
                    planned[attr] = merged_map
        return planned
    def _read_legacy_plugin_config(self, source: Path) -> dict[str, Any]:
        """读取旧插件的 AstrBotConfig 文件。

        标准位置是 ``<astrbot>/data/config/<plugin_name>_config.json``，
        ``source`` 形如 ``<astrbot>/data/plugin_data/<plugin_name>``。
        """
        data_root = source.parent.parent
        filenames = [f"{name}_config.json" for name in LEGACY_DIR_CANDIDATES]
        for base in (data_root / "config", data_root, source):
            for filename in filenames:
                data = self._read_json(base / filename)
                if isinstance(data, dict) and data:
                    return data
        return {}

    @staticmethod
    def _same_kind(value: Any, default: Any) -> bool:
        """判断旧值与新默认值类型是否兼容（int/float 互通，bool 独立）。"""
        if isinstance(default, bool) or isinstance(value, bool):
            return isinstance(default, bool) and isinstance(value, bool)
        if isinstance(default, (int, float)):
            return isinstance(value, (int, float))
        return isinstance(value, type(default))

    def _plan_config(self, source: Path, report: MigrationReport) -> dict[str, Any]:
        """挑出可以安全沿用的旧插件设置。

        只沿用同时满足以下条件的键，避免把旧插件的实现细节带进来：
        schema 里存在、配置模型里存在、类型兼容、旧值不等于默认值、
        且本插件当前仍是默认值（用户没自己改过）。
        """
        legacy = self._read_legacy_plugin_config(source)
        if not legacy:
            return {}
        schema = self._load_schema()
        fields = getattr(type(self.config), "model_fields", None) or {}
        planned: dict[str, Any] = {}
        for key, value in legacy.items():
            if key.startswith("_") or key in self._CONFIG_SKIP_KEYS:
                continue
            if key not in schema or key not in fields:
                continue
            entry = schema.get(key)
            if not isinstance(entry, dict) or "default" not in entry:
                continue
            default = entry["default"]
            if default is None or not self._same_kind(value, default):
                continue
            if value == default:
                continue
            if getattr(self.config, key, default) != default:
                continue
            planned[key] = value
        if planned:
            report.config_keys_imported = len(planned)
            report.imported_config_keys = sorted(planned)
        return planned

    # ── 主流程 ───────────────────────────────────────────────

    async def migrate(
        self,
        *,
        source: str | Path | None = None,
        apply: bool = False,
        move: bool = False,
        with_config: bool = True,
    ) -> MigrationReport:
        """执行迁移（``apply=False`` 时只预演）。"""
        report = MigrationReport(dry_run=not apply, move=bool(move and apply))
        resolved = self._resolve_source(source)
        if resolved is None:
            report.note(str(self.detect_legacy(source).get("reason") or "未找到旧插件数据目录"))
            return report

        report.source_dir = str(resolved)
        report.target_dir = str(self.target_dir)
        try:
            snapshot = self._read_legacy_snapshot(self._legacy_db_path(resolved))
        except Exception as exc:
            report.note(f"读取旧数据库失败：{exc}")
            return report

        file_index = self._index_legacy_files(resolved)
        json_updates = self._plan_json_assets(resolved, report)
        config_updates = self._plan_config(resolved, report) if with_config else {}
        plan = self._plan_images(snapshot, file_index, report)

        db = getattr(self.plugin, "db_service", None)
        known_black = set(db.blacklisted_hashes()) if db else set()
        report.blacklist_imported = sum(
            1 for hash_val in (snapshot.get("blacklist") or {}) if hash_val not in known_black
        )
        embeddings = snapshot.get("embeddings") or {}
        report.embeddings_imported = sum(1 for item in plan if item.legacy_path in embeddings)
        report.files_copied = len(plan)

        if not apply:
            return report

        await self._apply(snapshot, plan, json_updates, config_updates, report)
        return report

    async def _apply(
        self,
        snapshot: dict[str, Any],
        plan: list[_PlannedImage],
        json_updates: dict[str, Any],
        config_updates: dict[str, Any],
        report: MigrationReport,
    ) -> None:
        """把规划结果真正落盘（顺序：分类 -> 文件 -> 记录 -> 附属数据）。"""
        cfg = self.config
        db = getattr(self.plugin, "db_service", None)

        for attr, value in json_updates.items():
            try:
                setattr(cfg, attr, value)
            except Exception as exc:
                report.note(f"写入 {attr} 失败：{exc}")
        try:
            cfg.ensure_category_dirs(cfg.get_categories())
        except Exception as exc:
            report.note(f"创建分类目录失败：{exc}")

        report.files_copied = await self._move_files(plan, move=report.move, report=report)
        if db is None:
            report.note("数据库服务不可用，仅完成文件搬运")
            return

        rows = [
            {**item.row, "tags": item.tags, "scenes": item.scenes}
            for item in plan
            if not item.pending and item.dst_file.is_file()
        ]
        inserted = 0
        for start in range(0, len(rows), _INSERT_CHUNK):
            chunk = rows[start : start + _INSERT_CHUNK]
            try:
                inserted += await db.insert_batch(chunk)
            except Exception as exc:
                report.note(f"写入表情包记录失败：{exc}")
        report.emojis_imported = inserted

        pending_ok = 0
        pending_dupe = 0
        for item in plan:
            if not item.pending or not item.dst_file.is_file():
                continue
            try:
                meta = {**item.row, "tags": item.tags, "scenes": item.scenes}
                if await db.insert_pending(meta):
                    pending_ok += 1
                else:
                    pending_dupe += 1
            except Exception as exc:
                report.note(f"写入待审核记录失败：{exc}")
        report.pending_imported = pending_ok
        report.pending_skipped_duplicate += pending_dupe

        legacy_black = snapshot.get("blacklist") or {}
        if legacy_black:
            try:
                report.blacklist_imported = await db.add_blacklist_batch(dict(legacy_black))
            except Exception as exc:
                report.note(f"写入黑名单失败：{exc}")
                report.blacklist_imported = 0

        embeddings = snapshot.get("embeddings") or {}
        embedded = 0
        for item in plan:
            record = embeddings.get(item.legacy_path)
            if not record or not item.dst_file.is_file():
                continue
            vector, dim, model_sig = record
            try:
                db.upsert_embedding(str(item.dst_file), vector, dim, model_sig)
                embedded += 1
            except Exception as exc:
                report.note(f"写入语义向量失败：{exc}")
        report.embeddings_imported = embedded

        if config_updates:
            try:
                updater = getattr(self.plugin, "update_config", None)
                if callable(updater):
                    updater(dict(config_updates))
                else:
                    cfg.update_config(dict(config_updates))
            except Exception as exc:
                report.note(f"沿用旧插件设置失败：{exc}")
                report.config_keys_imported = 0
                report.imported_config_keys = []

        await self._invalidate_caches()
        logger.info(
            f"[迁移] 完成：表情包 {report.emojis_imported}，待审核 {report.pending_imported}，"
            f"文件 {report.files_copied}，来源 {report.source_dir}"
        )
    async def _move_files(
        self, plan: list[_PlannedImage], *, move: bool, report: MigrationReport
    ) -> int:
        """搬运图片文件。目标已存在视为"上次已迁好"，直接计入成功。"""
        done = 0
        for item in plan:
            dst = item.dst_file
            try:
                if dst.is_file():
                    done += 1
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                if move:
                    await asyncio.to_thread(shutil.move, str(item.src_file), str(dst))
                else:
                    await asyncio.to_thread(shutil.copy2, str(item.src_file), str(dst))
                done += 1
            except Exception as exc:
                report.note(f"搬运失败 {item.src_file.name}：{exc}")
        return done

    async def _invalidate_caches(self) -> None:
        """迁移后让检索侧的各级缓存过期，避免搜不到新数据。"""
        selector = getattr(self.plugin, "meme_selector", None)
        targets: list[tuple[Any, str]] = [
            (selector, "_invalidate_bm25_index"),
            (selector, "_invalidate_embedding_index"),
            (getattr(selector, "_smart_select_service", None), "_invalidate_embedding_index"),
            (getattr(self.plugin, "cache_service", None), "persist_all"),
        ]
        for owner, method_name in targets:
            if owner is None:
                continue
            method = getattr(owner, method_name, None)
            if not callable(method):
                continue
            try:
                result = method()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning(f"[迁移] 刷新缓存失败 {method_name}: {exc}")
