"""Bounded, sequential archive uploads through the plugin JSON bridge."""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ExternalSourceError, ExternalSourceSecurityError


@dataclass
class _Upload:
    path: Path
    size: int
    suffix: str
    received: int = 0
    touched: float = field(default_factory=time.monotonic)


class ArchiveUploadService:
    """Keep each bridge request below 1 MiB, including base64 overhead."""

    CHUNK_BYTES = 512 * 1024
    TTL_SECONDS = 30 * 60
    MAX_SESSIONS = 2

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._sessions: dict[str, _Upload] = {}
        self._completed: dict[str, tuple[Path, int, float]] = {}
        self._lock = asyncio.Lock()

    async def handle(
        self, payload: dict[str, Any], *, max_bytes: int
    ) -> dict[str, Any]:
        """Serialize disk operations and reject missing, oversized or reordered data."""
        async with self._lock:
            # Shield the disk operation so request cancellation cannot release the
            # lock while its worker is still appending to the same archive.
            task = asyncio.create_task(
                asyncio.to_thread(self._handle, payload, max_bytes)
            )
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def _handle(self, payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
        now = time.monotonic()
        self._completed = {
            key: value
            for key, value in self._completed.items()
            if now - value[2] <= self.TTL_SECONDS
        }
        for key, item in list(self._sessions.items()):
            if now - item.touched > self.TTL_SECONDS:
                item.path.unlink(missing_ok=True)
                del self._sessions[key]

        action = payload.get("action")
        if action == "start":
            suffix = Path(str(payload.get("filename") or "")).suffix.lower()
            if suffix not in {".zip", ".meme-pack"}:
                raise ExternalSourceError(
                    "only ZIP or .meme-pack archives are accepted"
                )
            size = payload.get("size")
            if type(size) is not int or size <= 0:
                raise ExternalSourceError("archive size must be a positive integer")
            if size > max_bytes:
                raise ExternalSourceSecurityError("pack archive exceeds the byte limit")
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise ExternalSourceError(
                    "too many archive uploads; finish or cancel an upload first"
                )
            self.directory.mkdir(parents=True, exist_ok=True)
            upload_id = uuid.uuid4().hex
            target = self.directory / f"{upload_id}.part"
            with target.open("xb"):
                pass
            self._sessions[upload_id] = _Upload(target, size, suffix)
            return {
                "upload_id": upload_id,
                "chunk_size": self.CHUNK_BYTES,
                "received": 0,
            }

        upload_id = str(payload.get("upload_id") or "")
        item = self._sessions.get(upload_id)
        if action == "cancel":
            if item is not None:
                item.path.unlink(missing_ok=True)
                del self._sessions[upload_id]
            return {"cancelled": True}
        if action == "finish" and upload_id in self._completed:
            path, size, _ = self._completed[upload_id]
            if not path.is_file() or path.stat().st_size != size:
                raise ExternalSourceError("completed archive is no longer available")
            return {"path": str(path), "received": size}
        if item is None:
            raise ExternalSourceError(
                "archive upload expired or was not found; select the file again"
            )
        item.touched = now

        if action == "chunk":
            offset = payload.get("offset")
            encoded = payload.get("data")
            if type(offset) is not int or offset < 0:
                raise ExternalSourceError("invalid archive chunk offset")
            if not isinstance(encoded, str) or len(encoded) > 4 * (
                (self.CHUNK_BYTES + 2) // 3
            ):
                raise ExternalSourceSecurityError(
                    "archive chunk exceeds the byte limit"
                )
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ExternalSourceError("invalid archive chunk encoding") from exc
            end = offset + len(chunk)
            if (
                not chunk
                or len(chunk) > self.CHUNK_BYTES
                or end > min(item.size, max_bytes)
            ):
                raise ExternalSourceSecurityError("invalid archive chunk size")
            with item.path.open("r+b") as output:
                output.seek(0, 2)
                if output.tell() != item.received:
                    raise ExternalSourceError(
                        "archive upload size changed; select the file again"
                    )
                if offset == item.received:
                    output.write(chunk)
                    item.received = end
                elif end <= item.received:
                    # A response can be lost after the chunk has been written.
                    # Retrying identical bytes must not append them twice.
                    output.seek(offset)
                    if output.read(len(chunk)) != chunk:
                        raise ExternalSourceError(
                            "archive retry does not match the uploaded data"
                        )
                else:
                    raise ExternalSourceError(
                        "archive chunks must be uploaded in order"
                    )
            return {"received": item.received}

        if action == "finish":
            if item.received != item.size or item.path.stat().st_size != item.size:
                raise ExternalSourceError("archive upload is incomplete")
            if item.size > max_bytes:
                raise ExternalSourceSecurityError("pack archive exceeds the byte limit")
            target = item.path.with_suffix(item.suffix)
            item.path.rename(target)
            self._completed[upload_id] = (target, item.size, now)
            if len(self._completed) > 32:
                del self._completed[next(iter(self._completed))]
            del self._sessions[upload_id]
            return {"path": str(target), "received": item.size}

        raise ExternalSourceError("unknown archive upload action")
