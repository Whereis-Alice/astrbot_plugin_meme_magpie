"""Forced capture admits whole bursts while bounding downloads and analysis."""

import asyncio
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.events.background_steal_queue import BackgroundStealQueue


def _plugin(tmp_path, *, concurrency=2):
    recorded = []
    current = {"existing.png": {"category": "happy"}}
    active = 0
    peak = 0

    async def load_index():
        await asyncio.sleep(0)
        return dict(current)

    async def save_index(value):
        await asyncio.sleep(0)
        current.clear()
        current.update(value)

    async def process(event, file_path, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.002)
            recorded.append({"event": event, "path": file_path, **kwargs})
            return True, {file_path: {"category": "happy"}}
        finally:
            active -= 1

    plugin = SimpleNamespace(
        base_dir=tmp_path,
        plugin_config=SimpleNamespace(
            batch_analyze_concurrency=concurrency,
            batch_analyze_rpm=0,
            batch_analyze_max_retries=0,
        ),
        _process_image=process,
        index_manager=SimpleNamespace(load_index=load_index, save_index=save_index),
    )
    return plugin, recorded, current, lambda: peak


async def _wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout=5)


def test_large_local_burst_is_snapshotted_and_not_dropped_at_queue_capacity(tmp_path):
    plugin, recorded, current, peak = _plugin(tmp_path)
    release = asyncio.Event()
    original_process = plugin._process_image

    async def process(*args, **kwargs):
        await release.wait()
        return await original_process(*args, **kwargs)

    plugin._process_image = process
    sources = []
    for i in range(70):
        source = tmp_path / f"original-{i}.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\n" + str(i).encode())
        sources.append(source)
    descriptors = [{
        "media_ref": str(source), "character": "艾拉", "work": "可塑性记忆",
        "extra_meta": {"origin_target": "group:42"}, "event": object(),
    } for source in sources]
    queue = BackgroundStealQueue(plugin, capacity=2, worker_count=2)

    async def run():
        await queue.start()
        try:
            assert await queue.submit_force_capture_async(descriptors) == 70
            assert queue.pending_count >= 68
            assert len(list(queue.staging_dir.iterdir())) == 70
            for source in sources:
                source.unlink()
            descriptors[0]["character"] = "错误角色"
            descriptors[0]["extra_meta"]["origin_target"] = "group:99"
            release.set()
            await _wait_until(lambda: len(current) == 71 and queue.pending_count == 0)
            assert len(recorded) == 70
            assert all(item["event"] is None for item in recorded)
            assert all(item["force_capture"] is True for item in recorded)
            assert all(item["to_pending"] is False for item in recorded)
            assert all(item["add_method"] == "force_capture" for item in recorded)
            assert all(item["llm_hints"].character == "艾拉" for item in recorded)
            assert all(item["llm_hints"].work == "可塑性记忆" for item in recorded)
            assert all(item["extra_meta"]["origin_target"] == "group:42" for item in recorded)
            assert peak() <= 2
        finally:
            release.set()
            await queue.stop()
        assert list(queue.staging_dir.iterdir()) == []

    asyncio.run(run())


def test_remote_burst_uses_two_downloaders_and_analysis_throttle(tmp_path):
    plugin, recorded, current, peak = _plugin(tmp_path, concurrency=1)
    release = asyncio.Event()
    both_started = asyncio.Event()
    download_active = 0
    download_peak = 0
    downloads = []

    async def download(url):
        nonlocal download_active, download_peak
        download_active += 1
        download_peak = max(download_peak, download_active)
        if download_active == 2:
            both_started.set()
        try:
            await release.wait()
            await asyncio.sleep(0.001)
            path = tmp_path / f"download-{len(downloads)}.gif"
            downloads.append(url)
            path.write_bytes(b"GIF89a" + url.encode())
            return str(path), True
        finally:
            download_active -= 1

    plugin.event_handler = SimpleNamespace(_download_url_to_temp=download)
    queue = BackgroundStealQueue(plugin, capacity=2, worker_count=2)

    async def run():
        await queue.start()
        try:
            assert await queue.submit_force_capture_async([
                {"media_ref": f"https://example.test/{i}.gif", "character": "角色"}
                for i in range(65)
            ]) == 65
            await asyncio.wait_for(both_started.wait(), timeout=1)
            assert download_peak == 2
            assert len(queue._force_tasks) == 3
            assert queue._capture_tasks == set()
            release.set()
            await _wait_until(lambda: len(current) == 66 and queue.pending_count == 0)
            assert len(downloads) == 65
            assert len(recorded) == 65
            assert peak() == 1
            assert all(Path(item["path"]).suffix == ".gif" for item in recorded)
            assert not list(tmp_path.glob("download-*.gif"))
        finally:
            release.set()
            await queue.stop()

    asyncio.run(run())


def test_stopping_force_backlogs_cleans_files_and_does_not_delete_sources(tmp_path):
    plugin, _recorded, _current, _peak = _plugin(tmp_path)
    started = asyncio.Event()

    async def processing(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def download(_url):
        await asyncio.Event().wait()

    plugin._process_image = processing
    plugin.event_handler = SimpleNamespace(_download_url_to_temp=download)
    queue = BackgroundStealQueue(plugin, capacity=1, worker_count=1)
    source = tmp_path / "owned-by-platform.png"
    source.write_bytes(b"image")

    async def run():
        await queue.start()
        assert await queue.submit_force_capture_async([
            *[{"media_ref": str(source)} for _ in range(40)],
            *[{"media_ref": f"https://example.test/{i}"} for i in range(40)],
        ]) == 80
        await asyncio.wait_for(started.wait(), timeout=1)
        await queue.stop()
        assert source.read_bytes() == b"image"
        assert queue.pending_count == 0
        assert not queue._force_tasks
        assert not queue._workers
        assert list(queue.staging_dir.iterdir()) == []
        assert await queue.submit_force_capture_async([{"media_ref": str(source)}]) == 0

    asyncio.run(run())


def test_failed_remote_image_does_not_block_other_admitted_images(tmp_path):
    plugin, recorded, current, _peak = _plugin(tmp_path)

    async def download(url):
        if url.endswith("bad"):
            raise RuntimeError("download failed")
        path = tmp_path / "download.png"
        path.write_bytes(b"image")
        return str(path), False

    plugin.event_handler = SimpleNamespace(_download_url_to_temp=download)
    queue = BackgroundStealQueue(plugin, capacity=1, worker_count=1)

    async def run():
        await queue.start()
        try:
            assert await queue.submit_force_capture_async([
                {"media_ref": "https://example.test/bad"},
                {"media_ref": "https://example.test/good"},
            ]) == 2
            await _wait_until(lambda: len(current) == 2 and queue.pending_count == 0)
            assert len(recorded) == 1
        finally:
            await queue.stop()

    asyncio.run(run())


def test_shutdown_waits_for_inflight_forced_local_snapshot(tmp_path):
    plugin, recorded, _current, _peak = _plugin(tmp_path)
    source = tmp_path / "owned-by-platform.png"
    source.write_bytes(b"image")
    queue = BackgroundStealQueue(plugin, capacity=1, worker_count=1)
    started = threading.Event()
    original_copy = shutil.copy2

    def slow_copy(src, dst):
        started.set()
        time.sleep(0.1)
        return original_copy(src, dst)

    async def run():
        await queue.start()
        with patch("shutil.copy2", slow_copy):
            admission = asyncio.create_task(queue.submit_force_capture_async([
                {"media_ref": str(source)}
            ]))
            assert await asyncio.to_thread(started.wait, 1)
            await queue.stop()
            assert await admission == 0
        assert recorded == []
        assert source.read_bytes() == b"image"
        assert list(queue.staging_dir.iterdir()) == []

    asyncio.run(run())


def test_cancelled_remote_copy_closes_handles_before_cleaning_source_and_staging(tmp_path):
    plugin, recorded, _current, _peak = _plugin(tmp_path)
    source = tmp_path / "downloaded-source.png"
    source.write_bytes(b"image")
    opened = threading.Event()
    release = threading.Event()
    copy_exited = asyncio.Event()
    queue = BackgroundStealQueue(plugin, capacity=1, worker_count=1)
    original_copy_file = queue._copy_file

    def blocking_copy(src, dst):
        with open(src, "rb") as source_file, open(dst, "wb") as staged_file:
            opened.set()
            assert release.wait(timeout=5)
            staged_file.write(source_file.read())

    async def tracked_copy_file(src, dst):
        try:
            await original_copy_file(src, dst)
        finally:
            copy_exited.set()

    async def download(_url):
        return str(source), False

    queue._copy_file = tracked_copy_file
    plugin.event_handler = SimpleNamespace(_download_url_to_temp=download)

    async def run():
        await queue.start()
        stopper = None
        try:
            with patch("shutil.copy2", blocking_copy):
                assert await queue.submit_force_capture_async([
                    {"media_ref": "https://example.test/image.png"}
                ]) == 1
                assert await asyncio.to_thread(opened.wait, 2)
                downloaders = [
                    task for task in queue._force_tasks
                    if task.get_name().startswith("force_capture_download_")
                ]
                stopper = asyncio.create_task(queue.stop())
                checkpoint = asyncio.Event()
                asyncio.get_running_loop().call_soon(checkpoint.set)
                await checkpoint.wait()
                assert all(task.cancelling() for task in downloaders)
                assert not copy_exited.is_set()
                assert not stopper.done()
                release.set()
                await asyncio.wait_for(stopper, timeout=3)
        finally:
            release.set()
            if stopper is not None:
                await stopper
            else:
                await queue.stop()
        assert copy_exited.is_set()
        assert not source.exists()
        assert recorded == []
        assert queue.pending_count == 0
        assert list(queue.staging_dir.iterdir()) == []

    asyncio.run(run())


def test_identical_bytes_remain_reserved_until_index_commit_finishes(tmp_path):
    plugin, _recorded, current, _peak = _plugin(tmp_path)
    first_save_started = asyncio.Event()
    commit_release = asyncio.Event()
    calls = []

    async def process(_event, path, **_kwargs):
        calls.append(path)
        if "one-image" in current:
            return False, None
        return True, {"one-image": {"hash": "same-content"}}

    original_save = plugin.index_manager.save_index

    async def save(value):
        first_save_started.set()
        await commit_release.wait()
        await original_save(value)

    plugin._process_image = process
    plugin.index_manager.save_index = save
    queue = BackgroundStealQueue(plugin, capacity=2, worker_count=2)
    sources = [tmp_path / "first.png", tmp_path / "second.png"]
    for source in sources:
        source.write_bytes(b"the same image bytes")

    async def run():
        await queue.start()
        try:
            assert await queue.submit_force_capture_async([
                {"media_ref": str(source)} for source in sources
            ]) == 2
            await asyncio.wait_for(first_save_started.wait(), timeout=1)
            await _wait_until(lambda: any(
                users == 2 for _lock, users in queue._force_content_locks.values()
            ))
            assert len(calls) == 1
            assert "one-image" not in current
            commit_release.set()
            await _wait_until(lambda: len(calls) == 2 and not queue._force_content_locks)
            assert len(current) == 2
            assert "one-image" in current
            assert list(queue.staging_dir.iterdir()) == []
        finally:
            commit_release.set()
            await queue.stop()

    asyncio.run(run())


@pytest.mark.parametrize("ordinary_result", ["failure", "pending"])
def test_forced_image_waits_for_same_hash_passive_job_and_still_enters_main_library(
    tmp_path, ordinary_result,
):
    from tests.test_force_capture_processing import _real_processor

    processor, source_path = _real_processor(tmp_path)
    plugin = processor.plugin
    plugin.base_dir = tmp_path / "plugin"
    plugin.plugin_config.batch_analyze_rpm = 0
    plugin.plugin_config.batch_analyze_max_retries = 0
    current = {}
    pending = {}
    classification_calls = []
    ordinary_started = asyncio.Event()
    ordinary_release = asyncio.Event()
    original_bytes = Path(source_path).read_bytes()

    async def classify(**kwargs):
        known = kwargs.get("known")
        is_forced = bool(known and known.character == "艾拉")
        classification_calls.append(is_forced)
        if not is_forced:
            ordinary_started.set()
            await ordinary_release.wait()
            if ordinary_result == "failure":
                raise RuntimeError("ordinary recognition failed")
        return "happy", ["笑"], "微笑的角色", "happy", [], "", []

    async def store_pending(file_path, _is_temp, category, hash_val, **_kwargs):
        pending_path = tmp_path / "pending" / Path(file_path).name
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(file_path, pending_path)
        pending[hash_val] = {"path": str(pending_path), "category": category}
        return True, None

    async def load_index():
        return dict(current)

    async def save_index(value):
        current.clear()
        current.update(value)

    plugin.db_service = SimpleNamespace(
        hash_exists=lambda image_hash: any(
            entry["hash"] == image_hash for entry in current.values()
        ),
        get_phash_map=lambda: {
            path: entry["phash"] for path, entry in current.items()
        },
    )
    plugin.index_manager = SimpleNamespace(load_index=load_index, save_index=save_index)
    plugin._process_image = processor.process_image
    processor.classify_image = classify
    processor._store_to_pending = store_pending
    queue = BackgroundStealQueue(plugin, capacity=2, worker_count=2)

    async def run():
        await queue.start()
        try:
            assert await queue.submit_capture_async([{
                "media_ref": source_path, "source": "automatic", "to_pending": True,
            }])
            await asyncio.wait_for(ordinary_started.wait(), timeout=2)
            assert await queue.submit_force_capture_async([{
                "media_ref": source_path, "character": "艾拉", "work": "可塑性记忆",
            }]) == 1
            await _wait_until(lambda: any(
                users == 2 for _lock, users in queue._force_content_locks.values()
            ))
            assert classification_calls == [False]
            assert len(processor._processing_hashes) == 1
            ordinary_release.set()
            await asyncio.wait_for(queue._queue.join(), timeout=3)
            assert classification_calls == [False, True]
            assert len(current) == 1
            saved_path, entry = next(iter(current.items()))
            assert Path(saved_path).read_bytes() == original_bytes
            assert Path(saved_path).parent == processor.categories_dir / "happy"
            assert entry["add_method"] == "force_capture"
            assert entry["character"] == "艾拉"
            assert entry["work"] == "可塑性记忆"
            assert {"艾拉", "可塑性记忆"}.issubset(entry["tags"])
            assert len(pending) == (1 if ordinary_result == "pending" else 0)
            assert processor._processing_hashes == set()
            assert queue._force_content_locks == {}
            assert list(queue.staging_dir.iterdir()) == []
            assert list(processor.raw_dir.iterdir()) == []
            assert Path(source_path).read_bytes() == original_bytes
        finally:
            ordinary_release.set()
            await queue.stop()

    asyncio.run(run())


def test_failed_identical_job_releases_reservation_for_next_job(tmp_path):
    plugin, _recorded, current, _peak = _plugin(tmp_path)
    calls = []

    async def process(_event, path, **_kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("per-image failure")
        return True, {"recovered.png": {"category": "happy"}}

    plugin._process_image = process
    queue = BackgroundStealQueue(plugin, capacity=1, worker_count=2)
    source = tmp_path / "source.png"
    source.write_bytes(b"same bytes")

    async def run():
        await queue.start()
        try:
            assert await queue.submit_force_capture_async([
                {"media_ref": str(source)}, {"media_ref": str(source)}
            ]) == 2
            await _wait_until(lambda: "recovered.png" in current and not queue._force_content_locks)
            assert len(calls) == 2
        finally:
            await queue.stop()

    asyncio.run(run())


def test_stop_cleans_same_hash_owner_and_waiter_during_commit(tmp_path):
    plugin, _recorded, _current, _peak = _plugin(tmp_path)
    commit_started = asyncio.Event()

    async def save(_value):
        commit_started.set()
        await asyncio.Event().wait()

    plugin.index_manager.save_index = save
    source = tmp_path / "source.png"
    source.write_bytes(b"same bytes")
    queue = BackgroundStealQueue(plugin, capacity=1, worker_count=2)

    async def run():
        await queue.start()
        assert await queue.submit_force_capture_async([
            {"media_ref": str(source)}, {"media_ref": str(source)}
        ]) == 2
        await asyncio.wait_for(commit_started.wait(), timeout=1)
        await _wait_until(lambda: any(
            users == 2 for _lock, users in queue._force_content_locks.values()
        ))
        await queue.stop()
        assert queue._force_content_locks == {}
        assert source.exists()
        assert list(queue.staging_dir.iterdir()) == []

    asyncio.run(run())


def test_distinct_contents_can_process_concurrently(tmp_path):
    plugin, _recorded, current, _peak = _plugin(tmp_path)
    active = 0
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def process(_event, path, **_kwargs):
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        try:
            await release.wait()
            return True, {path: {"category": "happy"}}
        finally:
            active -= 1

    plugin._process_image = process
    queue = BackgroundStealQueue(plugin, capacity=1, worker_count=2)
    sources = [tmp_path / "first.png", tmp_path / "second.png"]
    for i, source in enumerate(sources):
        source.write_bytes(f"different image {i}".encode())

    async def run():
        await queue.start()
        try:
            assert await queue.submit_force_capture_async([
                {"media_ref": str(source)} for source in sources
            ]) == 2
            await asyncio.wait_for(both_started.wait(), timeout=1)
            assert active == 2
            release.set()
            await _wait_until(lambda: len(current) == 3 and not queue._force_content_locks)
        finally:
            release.set()
            await queue.stop()

    asyncio.run(run())
