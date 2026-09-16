"""Controlled meme-library asset export for cooperating AstrBot plugins.

This module intentionally exposes a handle instead of a raw local path.  A
consumer can read the bytes for an upload, inspect the already-known metadata,
and release the handle, while the provider retains control over path
validation, size limits, integrity checks, and expiry.

The public surface is intentionally dependency-light.  In particular, this
module does not import AstrBook or imgbed_ferry; those plugins can consume the
handle through the small duck-typed contract documented in ``docs/integration``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from astrbot.api import logger

from ..events.event_context import unwrap_event
from ..util.normalization import canonicalize_path, normalize_label_list, normalize_scope_mode

MEME_ASSET_API_VERSION = 1


class MemeAssetExportError(RuntimeError):
    """A controlled failure while selecting or reading an exported asset.

    ``code`` is stable for callers; the human-readable message is allowed to
    change with translations and diagnostics.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "asset_error")
        self.message = str(message or self.code)
        super().__init__(f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {"success": False, "error": self.code, "message": self.message}


# A short alias is convenient for consumers that do not care about the more
# precise class name.  Keep the canonical name above for documentation.
MemeAssetError = MemeAssetExportError


@dataclass(slots=True)
class MemeAssetHandle:
    """A short-lived, integrity-checked reference to one library image.

    Consumers should use ``await handle.read_bytes()`` rather than trying to
    discover a local path.  ``release()`` is synchronous because it only
    invalidates the in-memory lease; ``await handle.arelease()`` is provided as
    a convenience for uniformly asynchronous callers.
    """

    _service: "MemeAssetExportService" = field(repr=False, compare=False)
    _path: Path = field(repr=False, compare=False)
    _deadline_monotonic: float = field(repr=False, compare=False)
    token: str
    emoji_id: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    metadata: dict[str, Any]
    created_at: float
    expires_at: float
    _released: bool = field(default=False, repr=False, compare=False)

    @property
    def asset_id(self) -> str:
        """Opaque lease identifier; it is not a filesystem path."""

        return self.token

    @property
    def provider(self) -> str:
        """Stable provider id used by cooperating uploaders for validation."""

        return "astrbot_plugin_meme_magpie"

    @property
    def api_version(self) -> int:
        return MEME_ASSET_API_VERSION

    @property
    def mime(self) -> str:
        """Short alias for ``mime_type`` used by a few upload abstractions."""

        return self.mime_type

    @property
    def content_type(self) -> str:
        """HTTP-oriented alias for upload adapters."""

        return self.mime_type

    @property
    def length(self) -> int:
        """Byte-length alias used by generic resource consumers."""

        return self.size

    @property
    def expired(self) -> bool:
        return self._released or time.monotonic() >= self._deadline_monotonic

    @property
    def released(self) -> bool:
        return self._released

    @property
    def ttl_seconds(self) -> float:
        return max(0.0, self.expires_at - self.created_at)

    def to_dict(self) -> dict[str, Any]:
        """Return safe descriptive data without exposing the local path."""

        return {
            "provider": self.provider,
            "api_version": self.api_version,
            "asset_id": self.asset_id,
            "emoji_id": self.emoji_id,
            "filename": self.filename,
            "mime": self.mime_type,
            "mime_type": self.mime_type,
            "content_type": self.mime_type,
            "size": self.size,
            "length": self.size,
            "sha256": self.sha256,
            "metadata": deepcopy(self.metadata),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
        }

    def _assert_usable(self) -> None:
        if self._released:
            raise MemeAssetExportError("asset_released", "资源句柄已经释放")
        if time.monotonic() >= self._deadline_monotonic:
            self._released = True
            self._service._forget_handle(self)
            raise MemeAssetExportError("asset_expired", "资源句柄已过期，请重新搜索并导出")
        if not self._service._owns_handle(self):
            self._released = True
            raise MemeAssetExportError("asset_invalid", "资源句柄无效或并非由当前插件签发")

    async def read_bytes(self) -> bytes:
        """Read the validated image bytes, enforcing the lease and hash again."""

        self._assert_usable()
        return await self._service._read_handle(self)

    async def read(self) -> bytes:
        """Alias for ``read_bytes`` for generic resource consumers."""

        return await self.read_bytes()

    def read_bytes_sync(self) -> bytes:
        """Synchronous counterpart for non-async upload adapters."""

        self._assert_usable()
        return self._service._read_handle_sync(self)

    def release(self) -> bool:
        """Release this lease.  Releasing twice is harmless and returns False."""

        if self._released:
            return False
        self._released = True
        return self._service._forget_handle(self)

    async def arelease(self) -> bool:
        """Async spelling of :meth:`release`."""

        return self.release()

    async def __aenter__(self) -> "MemeAssetHandle":
        self._assert_usable()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class MemeAssetExportService:
    """Resolve current search candidates and issue controlled asset leases."""

    # Keep this below the existing VLM input limit and compatible with the
    # default imgbed limits.  It is deliberately a hard safety ceiling rather
    # than a user-controlled value, because this API may be called by another
    # plugin without going through the WebUI configuration form.
    MAX_FILE_BYTES = 25 * 1024 * 1024
    DEFAULT_TTL_SECONDS = 120.0
    MAX_TTL_SECONDS = 10 * 60.0
    MAX_ACTIVE_HANDLES = 256
    CANDIDATE_TTL_SECONDS = 10 * 60.0

    ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
    MIME_BY_FORMAT = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "GIF": "image/gif",
        "WEBP": "image/webp",
        "BMP": "image/bmp",
    }
    EXT_BY_FORMAT = {
        "PNG": ".png",
        "JPEG": ".jpg",
        "GIF": ".gif",
        "WEBP": ".webp",
        "BMP": ".bmp",
    }

    def __init__(
        self,
        plugin: Any,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.plugin = plugin
        self.ttl_seconds = max(1.0, min(float(ttl_seconds), self.MAX_TTL_SECONDS))
        self.max_file_bytes = max(1024, min(int(max_file_bytes), self.MAX_FILE_BYTES))
        self._handles: dict[str, MemeAssetHandle] = {}
        self._closed = False

    @property
    def data_dir(self) -> Path:
        raw = getattr(self.plugin, "base_dir", None)
        if raw is None:
            config = getattr(self.plugin, "plugin_config", None)
            raw = getattr(config, "data_dir", None)
        if raw is None or not str(raw).strip():
            raise MemeAssetExportError("provider_unavailable", "插件数据目录尚未初始化")
        try:
            return Path(raw).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise MemeAssetExportError("provider_unavailable", f"无法定位插件数据目录: {exc}") from exc

    async def export_meme_asset(
        self,
        emoji_id: str | int,
        event: Any | None = None,
    ) -> MemeAssetHandle:
        """Export the selected current-turn candidate as a short-lived handle.

        ``emoji_id`` accepts the numeric id shown by ``magpie_search_meme``
        (for example ``1``) or its explicit form (``emoji_1``).  It never
        accepts a path; callers must go through the candidate list.
        """

        if self._closed:
            raise MemeAssetExportError("provider_unavailable", "插件正在关闭")

        event = unwrap_event(event)
        candidate = self._select_candidate(event, emoji_id)
        stored_path, entry = self._resolve_entry(candidate)
        path = await asyncio.to_thread(self._locate_and_inspect, candidate, stored_path, entry)

        # Scope and action checks happen after resolving the current DB row,
        # not against a stale path captured by an earlier search.
        self._check_permission(entry, event)

        try:
            inspection = await asyncio.to_thread(self._inspect_file, path)
        except MemeAssetExportError:
            raise
        except Exception as exc:
            raise MemeAssetExportError("file_unreadable", f"无法读取表情包: {exc}") from exc

        expected_hash = str(entry.get("hash") or candidate.get("hash") or "").strip().lower()
        if expected_hash and inspection["sha256"] != expected_hash:
            raise MemeAssetExportError(
                "asset_changed",
                "表情包文件内容已变化，索引中的校验值不再匹配",
            )

        now = time.time()
        handle = MemeAssetHandle(
            _service=self,
            _path=path,
            _deadline_monotonic=time.monotonic() + self.ttl_seconds,
            token=uuid.uuid4().hex,
            emoji_id=self._candidate_display_id(candidate),
            filename=self._safe_filename(path, inspection["format"]),
            mime_type=inspection["mime_type"],
            size=inspection["size"],
            sha256=inspection["sha256"],
            metadata=self._public_metadata(candidate, entry, inspection),
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )

        self._prune_handles()
        while len(self._handles) >= self.MAX_ACTIVE_HANDLES:
            oldest_token = next(iter(self._handles), None)
            if oldest_token is None:
                break
            old = self._handles.pop(oldest_token)
            old._released = True
        self._handles[handle.token] = handle
        return handle

    async def try_export_meme_asset(
        self,
        emoji_id: str | int,
        event: Any | None = None,
    ) -> dict[str, Any]:
        """Non-throwing convenience wrapper for plugin-to-plugin callers."""

        try:
            handle = await self.export_meme_asset(emoji_id, event)
        except MemeAssetExportError as exc:
            return exc.as_dict()
        return {"success": True, "asset": handle, **handle.to_dict()}

    def release_meme_asset(self, handle_or_token: MemeAssetHandle | str) -> bool:
        """Release a handle or opaque token issued by this service."""

        if isinstance(handle_or_token, MemeAssetHandle):
            return handle_or_token.release()
        token = str(handle_or_token or "").strip()
        handle = self._handles.get(token)
        if handle is None:
            return False
        return handle.release()

    def owns_handle(self, handle: Any) -> bool:
        """Public ownership check for uploaders validating a duck-typed handle."""

        return self._owns_handle(handle)

    def close(self) -> None:
        """Invalidate all outstanding leases during plugin shutdown."""

        self._closed = True
        for handle in self._handles.values():
            handle._released = True
        self._handles.clear()

    # ---------- handle internals ----------

    def _owns_handle(self, handle: Any) -> bool:
        return (
            isinstance(handle, MemeAssetHandle)
            and handle._service is self
            and self._handles.get(handle.token) is handle
            and not self._closed
        )

    def _forget_handle(self, handle: MemeAssetHandle) -> bool:
        current = self._handles.get(handle.token)
        if current is not handle:
            return False
        self._handles.pop(handle.token, None)
        return True

    def _prune_handles(self) -> None:
        now = time.monotonic()
        for token, handle in list(self._handles.items()):
            if handle._released or now >= handle._deadline_monotonic:
                handle._released = True
                self._handles.pop(token, None)

    async def _read_handle(self, handle: MemeAssetHandle) -> bytes:
        handle._assert_usable()
        data = await asyncio.to_thread(self._read_handle_sync, handle)
        handle._assert_usable()
        return data

    def _read_handle_sync(self, handle: MemeAssetHandle) -> bytes:
        handle._assert_usable()
        path = self._validate_path(handle._path)
        try:
            stat = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise MemeAssetExportError("file_missing", f"表情包文件不可用: {exc}") from exc
        if int(stat.st_size) != int(handle.size):
            raise MemeAssetExportError("asset_changed", "表情包文件大小已变化")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MemeAssetExportError("file_unreadable", f"读取表情包失败: {exc}") from exc
        if len(data) != handle.size:
            raise MemeAssetExportError("asset_changed", "表情包文件在读取过程中发生变化")
        digest = hashlib.sha256(data).hexdigest()
        if digest != handle.sha256:
            raise MemeAssetExportError("asset_changed", "表情包校验值已变化")
        return data

    # ---------- candidate and DB resolution ----------

    def _select_candidate(self, event: Any | None, emoji_id: str | int) -> dict[str, Any]:
        if event is None:
            raise MemeAssetExportError(
                "candidate_expired",
                "缺少原始事件，候选列表只在当前对话事件中有效",
            )
        getter = getattr(self.plugin, "_emoji_turn_state", None)
        if not callable(getter):
            raise MemeAssetExportError("candidate_expired", "当前插件没有可用的候选列表")
        try:
            state = getter(event)
            candidates = state.get_candidates() if state is not None else []
        except Exception as exc:
            raise MemeAssetExportError("candidate_expired", f"读取候选列表失败: {exc}") from exc
        if not isinstance(candidates, list) or not candidates:
            raise MemeAssetExportError(
                "candidate_expired", "候选列表为空或已失效，请先重新调用 magpie_search_meme"
            )

        text = str(emoji_id or "").strip()
        if not text or isinstance(emoji_id, bool):
            raise MemeAssetExportError("invalid_emoji_id", "emoji_id 必须是候选编号或 emoji_N")

        # Prefer an exact explicit id when present.  This also supports future
        # candidate identifiers without changing the consumer contract.
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            ids = {
                str(candidate.get(key) or "").strip().casefold()
                for key in ("id", "emoji_id", "candidate_id")
                if str(candidate.get(key) or "").strip()
            }
            if text.casefold() in ids:
                self._check_candidate_age(candidate)
                return candidate

        ordinal = self._parse_ordinal(text)
        if ordinal is None or ordinal < 1 or ordinal > len(candidates):
            raise MemeAssetExportError(
                "invalid_emoji_id",
                f"没有找到候选 {text!r}，可用编号为 1-{len(candidates)}",
            )
        candidate = candidates[ordinal - 1]
        if not isinstance(candidate, dict):
            raise MemeAssetExportError("candidate_expired", "候选项格式无效")
        self._check_candidate_age(candidate)
        return candidate

    @staticmethod
    def _parse_ordinal(value: str) -> int | None:
        text = value.strip().casefold()
        if text.startswith("emoji_"):
            text = text[6:]
        if not text.isdigit():
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _check_candidate_age(self, candidate: dict[str, Any]) -> None:
        raw = candidate.get("candidate_created_at")
        if raw in (None, ""):
            return
        try:
            created = float(raw)
        except (TypeError, ValueError):
            return
        if created > 0 and time.time() - created > self.CANDIDATE_TTL_SECONDS:
            raise MemeAssetExportError(
                "candidate_expired", "候选列表已过期，请重新调用 magpie_search_meme"
            )

    @staticmethod
    def _candidate_display_id(candidate: dict[str, Any]) -> str:
        for key in ("emoji_id", "id", "candidate_id"):
            value = str(candidate.get(key) or "").strip()
            if value:
                return value
        return ""

    def _resolve_entry(self, candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        candidate_path = str(candidate.get("path") or "").strip()
        candidate_hash = str(candidate.get("hash") or "").strip()
        db = getattr(self.plugin, "db_service", None)
        if db is None:
            raise MemeAssetExportError("provider_unavailable", "表情索引数据库不可用")

        entry: dict[str, Any] | None = None
        stored_path = candidate_path
        get_emoji = getattr(db, "get_emoji", None)
        if callable(get_emoji) and candidate_path:
            try:
                entry = get_emoji(candidate_path)
            except Exception:
                entry = None

        # Windows path case/separator differences and a moved category can
        # make the literal candidate key stale.  Match the DB key canonically.
        if entry is None and candidate_path:
            get_all_paths = getattr(db, "get_all_paths", None)
            if callable(get_all_paths) and callable(get_emoji):
                try:
                    wanted = canonicalize_path(candidate_path)
                    for raw_path in get_all_paths() or []:
                        if canonicalize_path(raw_path) == wanted:
                            stored_path = str(raw_path)
                            entry = get_emoji(stored_path)
                            if entry is not None:
                                break
                except Exception:
                    pass

        # The hash is the stable identity used by the image pipeline and lets
        # us recover from a stale category/compatibility path.
        if entry is None and candidate_hash:
            by_hash = getattr(db, "get_emoji_by_hash", None)
            if callable(by_hash):
                try:
                    found = by_hash(candidate_hash)
                except Exception:
                    found = None
                if found:
                    stored_path, entry = found

        if not isinstance(entry, dict):
            raise MemeAssetExportError(
                "candidate_expired", "候选对应的正式表情记录已不存在，请重新搜索"
            )
        return str(stored_path or entry.get("path") or candidate_path), entry

    # ---------- path and image validation ----------

    def _locate_and_inspect(
        self,
        candidate: dict[str, Any],
        stored_path: str,
        entry: dict[str, Any],
    ) -> Path:
        expected_hash = str(entry.get("hash") or candidate.get("hash") or "").strip().lower()
        raw_paths = [str(candidate.get("path") or "").strip(), str(stored_path or "").strip()]
        seen: set[str] = set()
        paths: list[Path] = []
        for raw in raw_paths:
            if not raw:
                continue
            path = self._validate_path(raw, allow_missing=True)
            key = canonicalize_path(path)
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)

        # First try the exact path(s); this is the common case.
        for path in paths:
            if not self._is_regular_nonlink(path):
                continue
            try:
                inspected = self._inspect_file(path)
            except MemeAssetExportError:
                continue
            if not expected_hash or inspected["sha256"] == expected_hash:
                return path

        # A known basename often survives when a stale split-compat path or a
        # manual category move left the DB key behind.  Search only under the
        # plugin's categories tree, never arbitrary system directories.  Do
        # not use basename-only recovery: without a hash, two files with the
        # same name could make the caller receive the wrong asset.
        basenames = {path.name for path in paths if path.name}
        categories_dir = self.data_dir / "categories"
        if expected_hash and categories_dir.is_dir() and not self._contains_link(categories_dir):
            try:
                for path in categories_dir.rglob("*"):
                    if not path.is_file() or path.name not in basenames:
                        continue
                    try:
                        path = self._validate_path(path)
                        if not self._is_regular_nonlink(path):
                            continue
                        inspected = self._inspect_file(path)
                    except (MemeAssetExportError, OSError):
                        continue
                    if not expected_hash or inspected["sha256"] == expected_hash:
                        logger.warning(
                            "[MemeAsset] 候选路径已过期，已按文件名/哈希恢复实际分类文件: %s -> %s",
                            stored_path,
                            path,
                        )
                        return path
            except OSError:
                pass

        if any(path.exists() for path in paths):
            raise MemeAssetExportError("asset_changed", "候选文件存在，但内容与索引不匹配")
        raise MemeAssetExportError("file_missing", "表情包文件不存在或已被删除")

    def _validate_path(self, raw_path: str | Path, *, allow_missing: bool = False) -> Path:
        root = self.data_dir
        try:
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = root / path
            # Normalize ``..`` without following symlinks first, so each
            # lexical component can be checked for links below.
            path = Path(os.path.abspath(os.fspath(path)))
            if not self._path_is_under(path, root):
                raise MemeAssetExportError("unsafe_path", "资源路径不在插件数据目录内")
            if self._contains_link(path, root=root):
                raise MemeAssetExportError("unsafe_path", "拒绝通过符号链接导出资源")
            resolved = path.resolve(strict=False)
            if not self._path_is_under(resolved, root):
                raise MemeAssetExportError("unsafe_path", "资源路径解析后越出插件数据目录")
            if not allow_missing and not resolved.is_file():
                raise MemeAssetExportError("file_missing", "表情包文件不存在")
            return resolved
        except MemeAssetExportError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise MemeAssetExportError("unsafe_path", f"资源路径无效: {exc}") from exc

    @staticmethod
    def _path_is_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            # pathlib is case-sensitive on POSIX and can be surprising for
            # Windows-style paths in tests; canonical string comparison is a
            # safe fallback because both values are already absolute.
            try:
                path_key = os.path.normcase(os.path.abspath(str(path)))
                root_key = os.path.normcase(os.path.abspath(str(root)))
                return os.path.commonpath([path_key, root_key]) == root_key
            except (OSError, ValueError):
                return False

    @classmethod
    def _is_link(cls, path: Path) -> bool:
        try:
            if os.path.islink(path) or path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            return bool(is_junction()) if callable(is_junction) else False
        except OSError:
            return True

    @classmethod
    def _contains_link(cls, path: Path, *, root: Path | None = None) -> bool:
        try:
            if root is None:
                # ``Path.anchor`` is the correct filesystem root on both
                # Windows (``C:\\``) and POSIX (``/``).  The cwd fallback is
                # only for the unusual case of a path object without an
                # anchor; it keeps this helper useful in isolated tests.
                root = Path(path.anchor or Path.cwd().anchor or os.sep)
            current = root
            relative = path.relative_to(root)
        except (ValueError, OSError, RuntimeError):
            return True
        for part in relative.parts:
            current = current / part
            if cls._is_link(current):
                return True
        return False

    @classmethod
    def _is_regular_nonlink(cls, path: Path) -> bool:
        try:
            return path.is_file() and not cls._is_link(path)
        except OSError:
            return False

    def _inspect_file(self, path: Path) -> dict[str, Any]:
        path = self._validate_path(path)
        if path.suffix.casefold() not in self.ALLOWED_EXTENSIONS:
            raise MemeAssetExportError("unsupported_format", "只允许导出 PNG/JPEG/GIF/WebP/BMP 图片")
        try:
            stat = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise MemeAssetExportError("file_missing", f"无法读取表情包文件: {exc}") from exc
        size = int(stat.st_size)
        if size <= 0:
            raise MemeAssetExportError("file_unreadable", "表情包文件为空")
        if size > self.max_file_bytes:
            raise MemeAssetExportError(
                "file_too_large",
                f"表情包文件超过安全上限（{self.max_file_bytes} bytes）",
            )
        try:
            with Image.open(path) as image:
                image.verify()
                image_format = str(image.format or "").upper()
                width, height = image.size
        except Exception as exc:
            raise MemeAssetExportError("invalid_image", f"图片格式校验失败: {exc}") from exc
        mime_type = self.MIME_BY_FORMAT.get(image_format)
        if not mime_type:
            raise MemeAssetExportError("unsupported_format", f"不支持导出的图片格式: {image_format or 'unknown'}")
        try:
            digest = self._sha256_file(path)
        except OSError as exc:
            raise MemeAssetExportError("file_unreadable", f"计算图片校验值失败: {exc}") from exc
        try:
            after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise MemeAssetExportError("file_missing", f"校验后图片不可用: {exc}") from exc
        if int(after.st_size) != size:
            raise MemeAssetExportError("asset_changed", "图片在校验过程中发生变化")
        return {
            "size": size,
            "sha256": digest,
            "mime_type": mime_type,
            "format": image_format,
            "width": int(width),
            "height": int(height),
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _safe_filename(path: Path, image_format: str) -> str:
        name = path.name.replace("\\", "_").replace("/", "_").replace("\x00", "_").strip()
        if not name:
            name = "meme"
        expected_ext = MemeAssetExportService.EXT_BY_FORMAT.get(image_format, ".img")
        if Path(name).suffix.casefold() not in MemeAssetExportService.ALLOWED_EXTENSIONS:
            name += expected_ext
        elif Path(name).suffix.casefold() != expected_ext:
            # A mislabeled file is still safe to upload; make its content type
            # and filename agree so remote servers do not misinterpret it.
            name = Path(name).stem + expected_ext
        return name[:180]

    # ---------- metadata and permissions ----------

    def _check_permission(self, entry: dict[str, Any], event: Any | None) -> None:
        if event is not None:
            checker = getattr(self.plugin, "is_send_enabled_for_event", None)
            if callable(checker):
                try:
                    if not checker(event):
                        raise MemeAssetExportError("send_disabled", "当前会话已禁用表情包发送")
                except MemeAssetExportError:
                    raise
                except Exception:
                    # Keep compatibility with lightweight test/adaptor event
                    # objects; the scope check below remains mandatory.
                    pass

        scope = normalize_scope_mode(entry.get("scope_mode")) or "public"
        if scope != "local":
            return
        if event is None:
            raise MemeAssetExportError("scope_denied", "仅来源会话可使用该表情包")

        origin = str(entry.get("origin_target") or "").strip()
        current = ""
        # Main exposes this as a small compatibility wrapper; prefer it so
        # adapters/tests that customize event-target resolution do not get
        # bypassed by reaching into PluginConfig directly.
        getter = getattr(self.plugin, "get_event_target", None)
        if not callable(getter):
            cfg = getattr(self.plugin, "plugin_config", None)
            getter = getattr(cfg, "get_event_target", None)
        if callable(getter):
            try:
                scope_name, target_id = getter(event)
                if scope_name and target_id:
                    current = f"{scope_name}:{target_id}"
            except Exception:
                current = ""
        if not origin or not current or origin.casefold() != current.casefold():
            raise MemeAssetExportError("scope_denied", "该表情包只允许在来源会话使用")

        # Also honor the selector's established policy when available.  The
        # direct check above is stricter for missing target metadata, while
        # this call preserves any future policy extensions in the selector.
        selector = getattr(self.plugin, "meme_selector", None)
        checker = getattr(selector, "is_path_allowed_for_event", None)
        if callable(checker):
            try:
                stored_path = str(entry.get("path") or "")
                if stored_path and not checker(stored_path, event):
                    raise MemeAssetExportError("scope_denied", "当前事件无权使用该表情包")
            except MemeAssetExportError:
                raise
            except Exception:
                pass

    @staticmethod
    def _clean_text(value: Any, limit: int = 500) -> str:
        return " ".join(str(value or "").split())[:limit]

    @classmethod
    def _clean_list(cls, value: Any, *, max_count: int = 32, item_limit: int = 120) -> list[str]:
        return [cls._clean_text(item, item_limit) for item in normalize_label_list(value)[:max_count]]

    def _public_metadata(
        self,
        candidate: dict[str, Any],
        entry: dict[str, Any],
        inspection: dict[str, Any],
    ) -> dict[str, Any]:
        def _entry_or_candidate(key: str, default: Any = "") -> Any:
            value = entry.get(key)
            if value in (None, "", [], ()):
                value = candidate.get(key, default)
            return value

        character = self._clean_text(_entry_or_candidate("character"), 120)
        cfg = getattr(self.plugin, "plugin_config", None)
        if character and cfg is not None:
            try:
                info_map = getattr(cfg, "character_info", {}) or {}
                info = info_map.get(character) if isinstance(info_map, dict) else None
                if isinstance(info, dict) and info.get("name"):
                    character = self._clean_text(info["name"], 120)
            except Exception:
                pass
        category = self._clean_text(
            _entry_or_candidate("category") or candidate.get("emotion"), 80
        )
        emotions = self._clean_list(_entry_or_candidate("emotions"), max_count=16)
        if not emotions and category:
            emotions = [category]
        try:
            use_count = max(0, int(_entry_or_candidate("use_count", 0) or 0))
        except (TypeError, ValueError):
            use_count = 0
        return {
            "emoji_id": self._candidate_display_id(candidate),
            "hash": self._clean_text(_entry_or_candidate("hash"), 128),
            "category": category,
            "emotion": category,
            "emotions": emotions,
            "work": self._clean_text(_entry_or_candidate("work"), 120),
            "character": character,
            "action": self._clean_text(_entry_or_candidate("action"), 160),
            "overlay_text": self._clean_text(_entry_or_candidate("overlay_text"), 500),
            "desc": self._clean_text(_entry_or_candidate("desc"), 500),
            "tags": self._clean_list(_entry_or_candidate("tags")),
            "scenes": self._clean_list(_entry_or_candidate("scenes")),
            "scope_mode": normalize_scope_mode(_entry_or_candidate("scope_mode")) or "public",
            "origin_target": self._clean_text(_entry_or_candidate("origin_target"), 160),
            "source": self._clean_text(_entry_or_candidate("source"), 120),
            "original_name": self._clean_text(_entry_or_candidate("original_name"), 180),
            "width": inspection["width"],
            "height": inspection["height"],
            "format": inspection["format"].lower(),
            "bytes": inspection["size"],
            "use_count": use_count,
        }


class MemeMagpieIntegrationAPI:
    """Small facade recommended for other plugins.

    It deliberately delegates to the owning ``Main`` instance so consumers do
    not depend on internal service attributes.  ``Main`` also exposes the two
    primary methods directly for backwards-friendly discovery.
    """

    api_version = MEME_ASSET_API_VERSION
    provider = "astrbot_plugin_meme_magpie"

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def get_capabilities(self) -> dict[str, Any]:
        """Return a small discovery document for loosely-coupled consumers."""

        return {
            "provider": self.provider,
            "api_version": self.api_version,
            "features": [
                "structured_search",
                "candidate_export",
                "short_lived_handle",
                "integrity_sha256",
                "scope_enforcement",
            ],
            "supported_mime_types": sorted(MemeAssetExportService.MIME_BY_FORMAT.values()),
            "max_asset_bytes": MemeAssetExportService.MAX_FILE_BYTES,
            "default_ttl_seconds": MemeAssetExportService.DEFAULT_TTL_SECONDS,
        }

    async def search_meme_candidates(
        self,
        event: Any,
        query: str = "",
        *,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        finder = getattr(self.plugin, "search_meme_candidates", None)
        if not callable(finder):
            return []
        return await finder(event, query, limit=limit, filters=filters)

    async def search_candidates(
        self,
        event: Any,
        query: str = "",
        *,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Generic alias for consumers that do not use meme-specific names."""

        return await self.search_meme_candidates(event, query, limit=limit, filters=filters)

    async def export_meme_asset(
        self,
        emoji_id: str | int,
        event: Any | None = None,
    ) -> MemeAssetHandle:
        exporter = getattr(self.plugin, "export_meme_asset", None)
        if not callable(exporter):
            raise MemeAssetExportError("provider_unavailable", "meme神偷资源接口不可用")
        return await exporter(emoji_id, event)

    async def export_asset(
        self,
        emoji_id: str | int,
        event: Any | None = None,
    ) -> MemeAssetHandle:
        """Generic alias for :meth:`export_meme_asset`."""

        return await self.export_meme_asset(emoji_id, event)

    async def try_export_meme_asset(
        self,
        emoji_id: str | int,
        event: Any | None = None,
    ) -> dict[str, Any]:
        exporter = getattr(self.plugin, "try_export_meme_asset", None)
        if not callable(exporter):
            return {
                "success": False,
                "error": "provider_unavailable",
                "message": "meme神偷资源接口不可用",
            }
        return await exporter(emoji_id, event)

    def release_meme_asset(self, handle_or_token: MemeAssetHandle | str) -> bool:
        releaser = getattr(self.plugin, "release_meme_asset", None)
        return bool(releaser(handle_or_token)) if callable(releaser) else False

    def release_asset(self, handle_or_token: MemeAssetHandle | str) -> bool:
        """Generic alias for :meth:`release_meme_asset`."""

        return self.release_meme_asset(handle_or_token)
