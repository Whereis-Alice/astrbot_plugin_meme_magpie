"""Prepare QQ-native Markdown images without changing ordinary image sends.

QQ Markdown material API reference:
https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/material-management.html
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import inspect
import math
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import aiohttp
from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from ..events.event_context import get_event_platform_name, unwrap_event
from ..util.normalization import canonicalize_path
from .meme_asset import MemeAssetExportService

QQ_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
EXTRA_KEY = "magpie_qq_markdown_images"
NATIVE_MAX_BYTES = 10 * 1024 * 1024
CALLBACK_TTL_SECONDS = 24 * 60 * 60


def normalize_markdown_image_url(value: Any) -> str:
    """Accept actual QQ material references and safe, Markdown-escaped URLs."""
    if not isinstance(value, str) or not value:
        return ""
    if any(ord(char) < 32 for char in value) or any(
        ord(char) < 32 for char in unquote(value)
    ):
        return ""
    value = value.strip()
    value = re.sub(r"%(?![0-9a-fA-F]{2})", "%25", value)
    try:
        parts = urlsplit(value)
        if parts.username or parts.password:
            return ""
        if parts.scheme == "qqbot-media":
            if parts.netloc != "image" or not re.fullmatch(r"/v1/[^\s/]+", parts.path):
                return ""
            if parts.query or parts.fragment:
                return ""
            if any(char in "()[]<>\\\"'" for char in parts.path):
                return ""
        elif parts.scheme in {"http", "https"}:
            if not parts.hostname:
                return ""
            if any(char.isspace() or ord(char) <= 32 or char in "()<>\\\"'" for char in parts.netloc):
                return ""
            _ = parts.port  # Validate malformed/non-numeric ports.
        else:
            return ""
    except (ValueError, TypeError):
        return ""
    # Escape image path/query syntax without encoding IPv6 host brackets.
    safe = "/:@!$&*+,;=%-._~"
    return urlunsplit((
        parts.scheme, parts.netloc, quote(parts.path, safe=safe),
        quote(parts.query, safe=safe + "?"), quote(parts.fragment, safe=safe + "?"),
    ))


def markdown_image_fragment(url: str, width: int, height: int) -> str:
    return f"\n\n![表情 #{width}px #{height}px]({url})\n"


class _SelectedAssetView:
    """Use the established exporter without changing the real tool candidates."""

    def __init__(self, plugin: Any, path: str):
        self._plugin = plugin
        self._candidate = {"id": "qqmd-selected", "path": path}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._plugin, name)

    def _emoji_turn_state(self, _event: Any) -> Any:
        return SimpleNamespace(get_candidates=lambda: [dict(self._candidate)])


class _UploadAsset:
    """A verified byte snapshot, retaining its original controlled lease."""

    protocol = "astrbot-meme-asset/v1"
    api_version = 1
    provider = "astrbot_plugin_meme_magpie"
    source = provider

    def __init__(self, handle: Any, data: bytes, mime: str, extension: str):
        self._handle = handle
        self._data = data
        self.asset_id = handle.asset_id
        self.filename = Path(handle.filename).stem + extension
        self.mime_type = self.mime = self.content_type = mime
        self.size = len(data)
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.created_at = handle.created_at
        self.expires_at = handle.expires_at
        self.metadata = dict(handle.metadata)

    async def read_bytes(self) -> bytes:
        # A cached encoding is not permission to read a changed/released source.
        await self._handle.read_bytes()
        return self._data


class QQMarkdownDeliveryService:
    """Register selected images for QQ-native Markdown, preserving the source."""

    MAX_CACHE_ENTRIES = 256
    MAX_WIDTH = 320

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}
        self._uploads: set[asyncio.Task] = set()
        self._preparing: set[asyncio.Task] = set()
        self._closed = False
        self._kv_lock = asyncio.Lock()
        self._persistent_loaded = False

    def _enabled(self, event: Any) -> bool:
        config = getattr(self.plugin, "plugin_config", None)
        return (
            not self._closed
            and event is not None
            and get_event_platform_name(event) in QQ_PLATFORMS
            and bool(getattr(config, "qqofficial_markdown_images", True))
        )

    @staticmethod
    def _bot_identity(event: Any) -> str:
        getter = getattr(event, "get_self_id", None)
        try:
            identity = str(getter() or "") if callable(getter) else ""
        except Exception:
            identity = ""
        bot = getattr(event, "bot", None)
        return identity or f"instance:{id(bot)}"

    @classmethod
    def _dimensions(cls, data: bytes) -> tuple[int, int, str, str]:
        with PILImage.open(io.BytesIO(data)) as image:
            width, height = image.size
            formats = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"),
                       "GIF": ("image/gif", ".gif"), "WEBP": ("image/webp", ".webp"),
                       "BMP": ("image/bmp", ".bmp")}
            mime, extension = formats[image.format]
        if width < 1 or height < 1:
            raise ValueError("invalid image dimensions")
        displayed_width = min(width, cls.MAX_WIDTH)
        displayed_height = max(1, round(height * displayed_width / width))
        return displayed_width, displayed_height, mime, extension

    async def prepare_attachment(
        self, event: Any, original_path: str, attached_path: str,
    ) -> bool:
        """Own the preparation task so unload can finish its cleanup."""
        if self._closed:
            return False
        task = asyncio.create_task(self._prepare_attachment(event, original_path, attached_path))
        self._preparing.add(task)
        try:
            return await task
        finally:
            self._preparing.discard(task)

    async def _prepare_attachment(
        self, event: Any, original_path: str, attached_path: str,
    ) -> bool:
        """Prepare a URL for an admitted local image, before appending it."""
        event = unwrap_event(event)
        if not self._enabled(event) or not original_path or not attached_path:
            return False
        setter = getattr(event, "set_extra", None)
        if not callable(setter):
            return False
        exporter = MemeAssetExportService(_SelectedAssetView(self.plugin, original_path))
        handle = None
        try:
            handle = await exporter.export_meme_asset("qqmd-selected", event)
            data = await handle.read_bytes()
            config = getattr(self.plugin, "plugin_config", None)
            send_gif = bool(getattr(self.plugin, "send_meme_as_gif", getattr(config, "send_meme_as_gif", False)))
            if send_gif:
                processor = getattr(self.plugin, "image_processor_service", None)
                encoder = getattr(processor, "_file_to_gif_base64", None)
                if not callable(encoder):
                    return False
                encoded = await encoder(original_path)
                data = base64.b64decode(encoded, validate=True)
            width, height, mime, extension = await asyncio.to_thread(self._dimensions, data)
            asset = _UploadAsset(handle, data, mime, extension)
            await self._load_native_cache()
            key = (self._bot_identity(event), asset.sha256)
            lock, users = self._locks.get(key, (asyncio.Lock(), 0))
            self._locks[key] = (lock, users + 1)
            try:
                async with lock:
                    if self._closed:
                        return False
                    cached = self._cache.get(key)
                    url = cached[0] if cached and cached[1] > time.time() else ""
                    if not url:
                        upload = asyncio.create_task(self._resolve_url(event, asset, original_path))
                        self._uploads.add(upload)
                        try:
                            url, lifetime = await upload
                        finally:
                            self._uploads.discard(upload)
                        if not url:
                            return False
                        self._cache[key] = (url, time.time() + max(1, lifetime))
                        while len(self._cache) > self.MAX_CACHE_ENTRIES:
                            self._cache.pop(next(iter(self._cache)))
                        await self._save_native_cache()
            finally:
                current_lock, users = self._locks[key]
                if users <= 1:
                    self._locks.pop(key, None)
                else:
                    self._locks[key] = (current_lock, users - 1)
            # Re-read the controlled lease after network waits before publishing.
            if self._closed:
                return False
            await handle.read_bytes()
            current_handle = await exporter.export_meme_asset("qqmd-selected", event)
            try:
                await current_handle.read_bytes()
            finally:
                current_handle.release()
            getter = getattr(event, "get_extra", None)
            registered = getter(EXTRA_KEY) if callable(getter) else None
            images = list(registered.get("images", [])) if isinstance(registered, dict) else []
            image = {"path": str(attached_path), "url": url, "width": width, "height": height}
            images = [item for item in images if isinstance(item, dict) and canonicalize_path(item.get("path", "")) != canonicalize_path(attached_path)]
            images.append(image)
            setter(EXTRA_KEY, {"version": 1, "images": images})
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # API errors may contain signed URLs; log only the failure type.
            logger.debug("[MemeThief] QQ Markdown image preparation failed (%s)", type(exc).__name__)
            return False
        finally:
            if handle is not None:
                handle.release()
            exporter.close()

    async def _load_native_cache(self) -> None:
        async with self._kv_lock:
            if self._persistent_loaded:
                return
            self._persistent_loaded = True
            getter = getattr(self.plugin, "get_kv_data", None)
            if not callable(getter):
                return
            try:
                stored = getter("qq_markdown_material_cache_v1", {})
                stored = await stored if inspect.isawaitable(stored) else stored
                if not isinstance(stored, dict):
                    return
                for key, item in list(stored.items())[:self.MAX_CACHE_ENTRIES]:
                    if not isinstance(key, str) or "|" not in key or not isinstance(item, dict):
                        continue
                    url = normalize_markdown_image_url(item.get("url"))
                    deadline = item.get("expires_at")
                    identity, digest = key.rsplit("|", 1)
                    if urlsplit(url).scheme == "qqbot-media" and re.fullmatch(r"[a-f0-9]{64}", digest) and isinstance(deadline, (int, float)) and math.isfinite(deadline) and deadline > time.time():
                        self._cache[(identity, digest)] = (url, float(deadline))
            except Exception as exc:
                logger.debug("[MemeThief] QQ material cache read failed (%s)", type(exc).__name__)

    async def _save_native_cache(self) -> None:
        setter = getattr(self.plugin, "put_kv_data", None)
        if not callable(setter):
            return
        async with self._kv_lock:
            stored = {f"{identity}|{digest}": {"url": url, "expires_at": deadline}
                      for (identity, digest), (url, deadline) in self._cache.items()
                      if urlsplit(url).scheme == "qqbot-media" and deadline > time.time()}
            try:
                result = setter("qq_markdown_material_cache_v1", stored)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug("[MemeThief] QQ material cache save failed (%s)", type(exc).__name__)

    async def aclose(self) -> None:
        """Stop owned network work before plugin unload; never delete QQ assets."""
        self._closed = True
        uploads = tuple(self._preparing | self._uploads)
        for task in uploads:
            task.cancel()
        if uploads:
            await asyncio.gather(*uploads, return_exceptions=True)
        self._uploads.clear()
        self._preparing.clear()
        self._cache.clear()

    async def _resolve_url(self, event: Any, asset: _UploadAsset, original_path: str) -> tuple[str, float]:
        try:
            url, lifetime = await self._upload_native(event, asset)
            if url:
                return url, lifetime
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[MemeThief] QQ native material upload unavailable (%s)", type(exc).__name__)
        try:
            context = getattr(self.plugin, "context", None)
            getter = getattr(context, "get_registered_star", None)
            meta = getter("astrbot_plugin_imgbed_ferry") if callable(getter) else None
            ferry = (getattr(meta, "star_cls", None) or getattr(meta, "star", None)) if meta else None
            uploader = getattr(ferry, "upload_asset", None)
            if callable(uploader) and getattr(meta, "activated", True):
                result = await uploader(event, asset, folder="qqbot/memes", compress=False, output_format="markdown")
                url = normalize_markdown_image_url(result.get("url")) if isinstance(result, dict) and result.get("success") else ""
                if url and urlsplit(url).scheme in {"http", "https"}:
                    return url, 24 * 60 * 60
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[MemeThief] QQ image-host fallback unavailable (%s)", type(exc).__name__)
        return await self._callback_url(event, asset, original_path)

    async def _native_request(self, event: Any, method: str, path: str, payload: dict) -> dict:
        from botpy.http import Route

        bot = getattr(event, "bot", None)
        http = getattr(getattr(bot, "api", None), "_http", None)
        request = getattr(http, "request", None)
        if not callable(request):
            raise RuntimeError("QQ upload transport unavailable")
        result = await request(Route(method, path), json=payload)
        if not isinstance(result, dict):
            raise RuntimeError("invalid QQ material response")
        return result

    async def _put_native(self, url: str, data: bytes, mime: str) -> str:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(url, data=data, headers={"Content-Type": mime}, allow_redirects=False) as response:
                if response.status not in {200, 201, 204}:
                    raise RuntimeError("QQ material binary upload failed")
                return str(response.headers.get("ETag", "")).strip('"')

    async def _upload_native(self, event: Any, asset: _UploadAsset) -> tuple[str, float]:
        if asset.size > NATIVE_MAX_BYTES or asset.mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            return "", 0
        data = await asset.read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        prepared = await self._native_request(event, "POST", "/v2/media-images/prepare", {
            "file_name": asset.filename, "file_size": len(data), "content_type": asset.mime,
            "md5": md5, "sha1": hashlib.sha1(data).hexdigest(),
        })
        upload_id = prepared.get("upload_id")
        presigned = normalize_markdown_image_url(prepared.get("presigned_url"))
        if not isinstance(upload_id, str) or not upload_id or urlsplit(presigned).scheme != "https":
            raise RuntimeError("invalid QQ material preparation")
        etag = await self._put_native(presigned, data, asset.mime)
        if etag.lower() != md5:
            raise RuntimeError("QQ material digest mismatch")
        completed = await self._native_request(event, "POST", "/v2/media-images/complete", {"upload_id": upload_id, "md5": etag})
        url = normalize_markdown_image_url(completed.get("url"))
        if urlsplit(url).scheme != "qqbot-media":
            raise RuntimeError("invalid QQ material reference")
        ttl = completed.get("ttl", 90 * 24 * 60 * 60)
        if isinstance(ttl, bool) or not math.isfinite(float(ttl)) or float(ttl) <= 0:
            raise RuntimeError("invalid QQ material lifetime")
        return url, max(60, min(float(ttl), 90 * 24 * 60 * 60) - 60)

    async def _callback_url(self, event: Any, asset: _UploadAsset, original_path: str) -> tuple[str, float]:
        try:
            from astrbot.core import file_token_service

            context = getattr(self.plugin, "context", None)
            getter = getattr(context, "get_config", None)
            config = getter(getattr(event, "unified_msg_origin", None)) if callable(getter) else {}
            callback = normalize_markdown_image_url(str(config.get("callback_api_base") or "").rstrip("/"))
            if urlsplit(callback).scheme not in {"http", "https"}:
                return "", 0
            source = original_path
            original = await asset._handle.read_bytes()
            if hashlib.sha256(original).hexdigest() != asset.sha256:
                cache_dir = Path(self.plugin.base_dir) / "cache" / "qq_markdown_media"
                cache_dir.mkdir(parents=True, exist_ok=True)
                inspector = MemeAssetExportService(self.plugin)
                inspector._validate_path(cache_dir / "placeholder", allow_missing=True)
                for cached_file in cache_dir.iterdir():
                    if cached_file.is_file() and re.fullmatch(r"[a-f0-9]{64}\.(png|jpg|gif|webp|bmp)", cached_file.name) and cached_file.stat().st_mtime + CALLBACK_TTL_SECONDS < time.time():
                        await asyncio.to_thread(cached_file.unlink)
                source = str(cache_dir / f"{asset.sha256}{Path(asset.filename).suffix}")
                data = await asset.read_bytes()
                existed = Path(source).exists()
                write = asyncio.create_task(asyncio.to_thread(Path(source).write_bytes, data))
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    while not write.done():
                        try:
                            await asyncio.shield(write)
                        except asyncio.CancelledError:
                            continue
                    write.result()
                    if not existed:
                        await asyncio.to_thread(Path(source).unlink, missing_ok=True)
                    raise
            token = await file_token_service.register_file(source, timeout=CALLBACK_TTL_SECONDS, single_use=False)
            url = normalize_markdown_image_url(f"{callback}/api/file/{token}")
            return url, CALLBACK_TTL_SECONDS - 60
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[MemeThief] QQ callback fallback unavailable (%s)", type(exc).__name__)
            return "", 0

    def convert_result(self, event: Any, result: Any) -> bool:
        """Convert registered images only, after any reply-splitting hook."""
        event = unwrap_event(event)
        if not self._enabled(event) or result is None:
            return False
        getter = getattr(event, "get_extra", None)
        registered = getter(EXTRA_KEY) if callable(getter) else None
        chain = getattr(result, "chain", None)
        if not isinstance(registered, dict) or registered.get("version") != 1 or not isinstance(chain, list):
            return False
        images = registered.get("images", [])
        if not isinstance(images, list):
            return False
        converted = []
        changed = False
        for component in chain:
            replacement = None
            if isinstance(component, Image):
                refs = {canonicalize_path(str(getattr(component, attr, "") or "")) for attr in ("path", "file", "url") if getattr(component, attr, None)}
                for item in images:
                    if not isinstance(item, dict) or canonicalize_path(str(item.get("path") or "")) not in refs:
                        continue
                    url = normalize_markdown_image_url(item.get("url"))
                    width, height = item.get("width"), item.get("height")
                    if url and type(width) is int and type(height) is int and width > 0 and height > 0:
                        replacement = Plain(markdown_image_fragment(url, width, height))
                        break
            if replacement is not None:
                changed = True
                converted.append(replacement)
            else:
                converted.append(component)
        if changed:
            chain[:] = converted
            result.use_markdown_ = True
            result.use_t2i_ = False
        return changed
