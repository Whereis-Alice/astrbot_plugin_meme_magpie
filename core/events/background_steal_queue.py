"""Bounded background pipeline for passive meme stealing."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..util.safe_io import safe_remove_file
from ..processing.analysis_throttle import build_analysis_throttle
from ..processing.llm_meme_hints import LlmMemeHints
from .image_download_service import ImageDownloadService


@dataclass(slots=True)
class StealJob:
    """A serializable processing job; event objects never cross this boundary."""

    file_path: str
    extra_meta: dict[str, Any] = field(default_factory=dict)
    to_pending: bool = True
    source: str = "automatic"
    character: str = ""
    work: str = ""


class BackgroundStealQueue:
    """Bounded queue and workers for downloaded/staged meme files."""

    def __init__(self, plugin: Any, *, capacity: int = 32, worker_count: int = 2):
        self.plugin = plugin
        self.capacity = max(1, int(capacity))
        self.worker_count = max(1, int(worker_count))
        # The processing queue is bounded by `capacity`; cap detached
        # staging/download tasks as well. Without this cap a message flood
        # could create an unbounded number of remote download tasks before
        # they ever reach the queue-full check.
        self.capture_limit = max(4, min(32, self.capacity))
        base_dir = getattr(plugin, "base_dir", None)
        self.staging_dir = Path(base_dir or Path.cwd()) / "staging"
        self._queue: asyncio.Queue[StealJob] = asyncio.Queue(maxsize=self.capacity)
        self._workers: list[asyncio.Task[Any]] = []
        self._capture_tasks: set[asyncio.Task[Any]] = set()
        self._save_lock = asyncio.Lock()
        # Serializes "copy into staging + enqueue". stop() takes this lock
        # before draining/cleaning staging so a local snapshot cannot enqueue
        # after the cleanup pass.
        self._staging_lock = asyncio.Lock()
        self._copy_executor: ThreadPoolExecutor | None = None
        self._accepting = False
        # Forced captures keep lightweight references to staged files instead
        # of dropping them when the bounded processing queue is full.
        self._force_backlog: deque[StealJob] = deque()
        self._force_remote: deque[dict[str, Any]] = deque()
        self._force_ready = asyncio.Event()
        self._force_remote_ready = asyncio.Event()
        self._force_tasks: list[asyncio.Task[Any]] = []
        self._force_download_inflight = 0
        self._force_feed_inflight = 0
        self._force_content_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._force_throttle = build_analysis_throttle(
            getattr(plugin, "plugin_config", None), name="force_capture"
        )

    @property
    def pending_count(self) -> int:
        return (
            self._queue.qsize() + len(self._force_backlog) + len(self._force_remote)
            + self._force_download_inflight + self._force_feed_inflight
        )

    @staticmethod
    def _staging_suffix(file_path: str) -> str:
        """按文件魔数选择暂存后缀，避免“名字像 jpg、内容是 WebP”的图被误标。"""
        try:
            suffix, _ = ImageDownloadService.detect_local_file_type(file_path)
            return suffix
        except OSError:
            return Path(file_path).suffix.lower() or ".jpg"

    async def start(self) -> None:
        if self._accepting:
            return
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        if self._copy_executor is None:
            self._copy_executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="steal_copy"
            )
        self._accepting = True
        self._workers = [
            asyncio.create_task(self._worker_loop(), name=f"steal_worker_{i}")
            for i in range(self.worker_count)
        ]
        self._force_tasks = [
            asyncio.create_task(self._force_feeder(), name="force_capture_feeder"),
            *[
                asyncio.create_task(self._force_downloader(), name=f"force_capture_download_{i}")
                for i in range(2)
            ],
        ]
        logger.info(
            f"[MemeThief] 后台偷图队列已启动: capacity={self.capacity}, "
            f"workers={self.worker_count}, capture_limit={self.capture_limit}"
        )

    async def stop(self) -> None:
        self._accepting = False

        # Stop detached staging/download tasks first.
        for task in list(self._capture_tasks):
            if not task.done():
                task.cancel()
        if self._capture_tasks:
            await asyncio.gather(*self._capture_tasks, return_exceptions=True)
        self._capture_tasks.clear()

        for task in self._force_tasks:
            task.cancel()
        if self._force_tasks:
            await asyncio.gather(*self._force_tasks, return_exceptions=True)
        self._force_tasks.clear()
        self._force_remote.clear()

        # Stop processing workers.
        for task in self._workers:
            if not task.done():
                task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        # Cancelling an asyncio task does not stop an in-flight
        # run_in_executor(shutil.copy2) thread. Take the staging lock (which
        # also waits for a local snapshot currently in its critical section),
        # shut the copy executor down with wait=True, and only then remove
        # queued/staged files.
        async with self._staging_lock:
            if self._copy_executor is not None:
                await asyncio.to_thread(self._copy_executor.shutdown, wait=True)
                self._copy_executor = None

            while not self._queue.empty():
                try:
                    job = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await safe_remove_file(job.file_path)
                self._queue.task_done()

            while self._force_backlog:
                await safe_remove_file(self._force_backlog.popleft().file_path)

            if self.staging_dir.exists():
                for path in self.staging_dir.iterdir():
                    if path.is_file():
                        await safe_remove_file(str(path))

        logger.info("[MemeThief] 后台偷图队列已停止，暂存文件已清理")

    async def submit_force_capture_async(self, descriptors: list[dict[str, Any]]) -> int:
        """Admit every forced image, snapshotting event-owned files before returning.

        Remote URLs wait in a lightweight backlog serviced by two downloaders;
        staged files wait on disk until the bounded processing queue has room.
        Neither backlog holds an event or image component.
        """
        if not self._accepting:
            return 0
        accepted = 0
        for item in descriptors:
            if not self._accepting:
                break
            descriptor = {
                "media_ref": str(item.get("media_ref") or ""),
                "extra_meta": dict(item.get("extra_meta") or {}),
                "character": str(item.get("character") or ""),
                "work": str(item.get("work") or ""),
            }
            ref = descriptor["media_ref"]
            if ref.startswith(("http://", "https://")):
                self._force_remote.append(descriptor)
                self._force_remote_ready.set()
                accepted += 1
            elif await self._stage_force_local(descriptor):
                accepted += 1
        return accepted

    async def _stage_force_local(self, descriptor: dict[str, Any]) -> bool:
        ref = descriptor["media_ref"]
        if not ref or not os.path.isfile(ref):
            logger.warning(f"强制收录暂存失败：媒体不存在 ref={ref[:120]}")
            return False
        staged_path = ""
        async with self._staging_lock:
            if not self._accepting:
                return False
            try:
                suffix = self._staging_suffix(ref)
                staged_path = str(self.staging_dir / f"{uuid.uuid4().hex}{suffix.lower()}")
                await self._copy_file(ref, staged_path)
                if not self._accepting:
                    await safe_remove_file(staged_path)
                    return False
                self._force_backlog.append(StealJob(
                    file_path=staged_path,
                    extra_meta=dict(descriptor.get("extra_meta") or {}),
                    to_pending=False,
                    source="force_capture",
                    character=str(descriptor.get("character") or ""),
                    work=str(descriptor.get("work") or ""),
                ))
                self._force_ready.set()
                return True
            except asyncio.CancelledError:
                if staged_path:
                    await safe_remove_file(staged_path)
                raise
            except Exception as exc:
                logger.error(f"强制收录暂存失败: {exc!r}", exc_info=exc)
                if staged_path:
                    await safe_remove_file(staged_path)
                return False

    async def _force_feeder(self) -> None:
        while True:
            self._force_ready.clear()
            while self._force_backlog:
                job = self._force_backlog.popleft()
                self._force_feed_inflight = 1
                try:
                    await self._queue.put(job)
                except asyncio.CancelledError:
                    await safe_remove_file(job.file_path)
                    raise
                finally:
                    self._force_feed_inflight = 0
            await self._force_ready.wait()

    async def _force_downloader(self) -> None:
        while True:
            self._force_remote_ready.clear()
            while self._force_remote:
                descriptor = self._force_remote.popleft()
                source_path = ""
                self._force_download_inflight += 1
                try:
                    handler = getattr(self.plugin, "event_handler", None)
                    if handler is None:
                        logger.warning("强制收录下载失败：事件处理服务不可用")
                        continue
                    source_path, _ = await handler._download_url_to_temp(descriptor["media_ref"])
                    if not source_path:
                        logger.warning("强制收录下载失败：媒体未下载")
                        continue
                    local_descriptor = dict(descriptor, media_ref=str(source_path))
                    await self._stage_force_local(local_descriptor)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"强制收录下载失败: {exc!r}", exc_info=exc)
                finally:
                    self._force_download_inflight -= 1
                    if source_path:
                        await safe_remove_file(str(source_path))
            await self._force_remote_ready.wait()

    @staticmethod
    def _content_hash(file_path: str) -> str:
        digest = hashlib.sha256()
        with open(file_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @asynccontextmanager
    async def _force_content_reservation(self, file_path: str):
        """Keep duplicate bytes reserved through processing AND index commit."""
        content_hash = await asyncio.to_thread(self._content_hash, file_path)
        lock, users = self._force_content_locks.get(content_hash, (asyncio.Lock(), 0))
        self._force_content_locks[content_hash] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            current_lock, users = self._force_content_locks[content_hash]
            if users <= 1:
                self._force_content_locks.pop(content_hash, None)
            else:
                self._force_content_locks[content_hash] = (current_lock, users - 1)

    async def _save_result(self, index: dict[str, Any]) -> None:
        async with self._save_lock:
            current = await self.plugin.index_manager.load_index()
            current = dict(current or {})
            current.update(index)
            await self.plugin.index_manager.save_index(current)

    def submit_capture(self, descriptors: list[dict[str, Any]]) -> bool:
        """Schedule staging/download work and return without waiting for it."""
        if not self._accepting or not descriptors:
            return False
        if len(self._capture_tasks) >= self.capture_limit:
            logger.warning(
                f"[MemeThief] 后台偷图暂存任务已达上限 {self.capture_limit}，丢弃新任务"
            )
            return False
        task = asyncio.create_task(
            self._stage_and_enqueue(descriptors), name="steal_stage_enqueue"
        )
        self._capture_tasks.add(task)
        task.add_done_callback(self._capture_done)
        return True

    async def submit_capture_async(self, descriptors: list[dict[str, Any]]) -> bool:
        """Snapshot local media before the originating event is released.

        AstrBot owns local media paths only for the lifetime of the event. Local
        files are copied into plugin staging here; remote URLs remain detached
        background downloads so network latency never reaches the event handler.
        """
        if not self._accepting or not descriptors:
            return False
        local_descriptors: list[dict[str, Any]] = []
        remote_descriptors: list[dict[str, Any]] = []
        for descriptor in descriptors:
            ref = str(descriptor.get("media_ref") or "")
            if ref.startswith(("http://", "https://")):
                remote_descriptors.append(descriptor)
            else:
                local_descriptors.append(descriptor)

        accepted = False
        for descriptor in local_descriptors:
            staged = await self._stage_local_descriptor(descriptor)
            if staged:
                accepted = True
        if remote_descriptors:
            accepted = self.submit_capture(remote_descriptors) or accepted
        return accepted

    async def _copy_file(self, source_path: str, target_path: str) -> None:
        """Copy a file on a dedicated executor so shutdown can wait for it."""
        executor = self._copy_executor
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="steal_copy")
            self._copy_executor = executor
        copy_future = asyncio.get_running_loop().run_in_executor(
            executor, shutil.copy2, source_path, target_path
        )
        try:
            await asyncio.shield(copy_future)
        except asyncio.CancelledError:
            # Cancellation cannot stop the copy thread. Wait until its file
            # handles close before callers remove either source or staging.
            while not copy_future.done():
                try:
                    await asyncio.shield(copy_future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not copy_future.cancelled():
                copy_future.exception()
            raise

    async def _stage_local_descriptor(self, descriptor: dict[str, Any]) -> bool:
        ref = str(descriptor.get("media_ref") or "")
        if not ref or not os.path.exists(ref):
            logger.warning(f"后台偷图暂存失败：媒体不存在 ref={ref[:120]}")
            return False

        async with self._staging_lock:
            if not self._accepting:
                return False
            staged_path = ""
            try:
                suffix = self._staging_suffix(ref)
                staged = self.staging_dir / f"{uuid.uuid4().hex}{suffix.lower()}"
                await self._copy_file(ref, str(staged))
                staged_path = str(staged)

                # stop() may have started while the copy thread was running.
                if not self._accepting:
                    await safe_remove_file(staged_path)
                    return False

                job = StealJob(
                    file_path=staged_path,
                    extra_meta=dict(descriptor.get("extra_meta") or {}),
                    to_pending=bool(descriptor.get("to_pending", True)),
                    source=str(descriptor.get("source") or "automatic"),
                )
                if self._queue.full():
                    await safe_remove_file(staged_path)
                    logger.warning(
                        "[MemeThief] 后台偷图队列已满，丢弃最新任务 "
                        f"source={job.source} meta={job.extra_meta}"
                    )
                    return False
                self._queue.put_nowait(job)
                logger.info(
                    f"[MemeThief] 后台偷图已入队 source={job.source}, queued={self._queue.qsize()}"
                )
                return True
            except asyncio.CancelledError:
                if staged_path:
                    await safe_remove_file(staged_path)
                raise
            except Exception as exc:
                logger.error(f"后台偷图本地暂存失败 ref={ref[:120]}: {exc!r}", exc_info=exc)
                if staged_path:
                    await safe_remove_file(staged_path)
                return False

    def _capture_done(self, task: asyncio.Task[Any]) -> None:
        self._capture_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"后台偷图暂存任务异常: {exc!r}", exc_info=exc)

    async def _stage_and_enqueue(self, descriptors: list[dict[str, Any]]) -> None:
        handler = getattr(self.plugin, "event_handler", None)
        if handler is None:
            return
        for descriptor in descriptors:
            if not self._accepting:
                return
            source_path = ""
            source_is_temp = False
            staged_path = ""
            try:
                ref = str(descriptor.get("media_ref") or "")
                if ref.startswith(("http://", "https://")):
                    # Best-effort early rejection: don't download a remote file
                    # just to discover that the processing queue is already full.
                    if self._queue.full():
                        logger.warning(
                            f"[MemeThief] 后台偷图队列已满，下载前丢弃任务 ref={ref[:120]}"
                        )
                        continue
                    source_path, _ = await handler._download_url_to_temp(ref)
                    source_is_temp = True
                elif ref and os.path.exists(ref):
                    source_path = ref
                if not source_path or not os.path.exists(source_path):
                    logger.warning(f"后台偷图暂存失败：媒体不存在 ref={ref[:120]}")
                    continue

                async with self._staging_lock:
                    if not self._accepting:
                        return
                    suffix = self._staging_suffix(source_path)
                    staged_path = str(
                        self.staging_dir / f"{uuid.uuid4().hex}{suffix.lower()}"
                    )
                    await self._copy_file(source_path, staged_path)
                    if source_is_temp:
                        await safe_remove_file(source_path)
                        source_path = ""

                    job = StealJob(
                        file_path=staged_path,
                        extra_meta=dict(descriptor.get("extra_meta") or {}),
                        to_pending=bool(descriptor.get("to_pending", True)),
                        source=str(descriptor.get("source") or "automatic"),
                    )
                    if self._queue.full():
                        await safe_remove_file(staged_path)
                        staged_path = ""
                        logger.warning(
                            "[MemeThief] 后台偷图队列已满，丢弃最新任务 "
                            f"source={job.source} meta={job.extra_meta}"
                        )
                        continue
                    self._queue.put_nowait(job)
                    logger.info(
                        f"[MemeThief] 后台偷图已入队 source={job.source}, "
                        f"queued={self._queue.qsize()}"
                    )
            except asyncio.CancelledError:
                if staged_path:
                    await safe_remove_file(staged_path)
                if source_is_temp and source_path:
                    await safe_remove_file(source_path)
                raise
            except Exception as exc:
                logger.error(f"后台偷图暂存失败: {exc!r}", exc_info=exc)
                if staged_path:
                    await safe_remove_file(staged_path)
                if source_is_temp and source_path:
                    await safe_remove_file(source_path)

    async def _worker_loop(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                # For cooldown mode the interval must cover actual processing
                # time, not only the enqueue timestamp. The EventHandler gate
                # still consumes the window at enqueue; re-marking here pushes
                # the next eligible message to at least one cooldown after the
                # real processing start.
                event_handler = getattr(self.plugin, "event_handler", None)
                mark_started = getattr(event_handler, "mark_processing_started", None)
                if callable(mark_started):
                    mark_started()

                kwargs = {
                    "is_temp": True,
                    "is_platform_emoji": True,
                    "extra_meta": job.extra_meta,
                    "to_pending": job.to_pending,
                }
                async with self._force_content_reservation(job.file_path):
                    if job.source == "force_capture":
                        kwargs.update(
                            llm_hints=LlmMemeHints.from_tool_args(
                                getattr(self.plugin, "plugin_config", None),
                                character=job.character, work=job.work,
                            ),
                            force_capture=True,
                            add_method="force_capture",
                        )
                        success, index = await self._force_throttle.run(
                            self.plugin._process_image, None, job.file_path, **kwargs
                        )
                    else:
                        success, index = await self.plugin._process_image(
                            None, job.file_path, **kwargs
                        )
                    if success and isinstance(index, dict) and not job.to_pending:
                        await self._save_result(index)
                logger.info(
                    f"[MemeThief] 后台偷图处理完成 source={job.source}, "
                    f"success={success}, pending={job.to_pending}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    f"后台偷图任务失败 source={job.source} meta={job.extra_meta}: {exc!r}",
                    exc_info=exc,
                )
            finally:
                await safe_remove_file(job.file_path)
                self._queue.task_done()
