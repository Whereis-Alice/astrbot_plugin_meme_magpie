"""强制收录的真实图片落盘、失败保留与去重边界。"""

import asyncio
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from core.processing.llm_meme_hints import LlmMemeHints
from tests.test_vlm_response_validation import _make_processor


def _real_processor(tmp_path):
    processor, _llm, _framework, path = _make_processor(tmp_path)
    config = processor.plugin_config

    def ensure_raw_dir():
        config.raw_dir.mkdir(parents=True, exist_ok=True)
        return config.raw_dir

    def ensure_category_dir(category):
        directory = config.categories_dir / category
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    config.ensure_raw_dir = ensure_raw_dir
    config.ensure_category_dir = ensure_category_dir
    processor.plugin.enable_embedding_search = False
    Image.new("RGB", (48, 48), (100, 150, 200)).save(path)
    return processor, path


def test_failed_force_analysis_keeps_real_image_and_manual_labels_without_cache(tmp_path):
    processor, path = _real_processor(tmp_path)
    processor.classify_image = AsyncMock(side_effect=RuntimeError("upstream 502"))
    hints = LlmMemeHints.from_tool_args(character="艾拉", work="可塑性记忆")
    original = Path(path).read_bytes()
    index = {}

    succeeded, returned_index = asyncio.run(processor.process_image(
        None, path, is_temp=True, idx=index, force_capture=True,
        llm_hints=hints, add_method="force_capture",
    ))

    assert succeeded
    assert returned_index is index
    assert len(index) == 1
    saved_path, entry = next(iter(index.items()))
    assert Path(saved_path).read_bytes() == original
    assert Path(saved_path).parent == processor.categories_dir / "confused"
    assert entry["character"] == "艾拉"
    assert entry["work"] == "可塑性记忆"
    assert {"艾拉", "可塑性记忆"}.issubset(entry["tags"])
    assert entry.get("desc", "") == ""
    assert entry["add_method"] == "force_capture"
    assert entry["width"] == entry["height"] == 48
    assert processor._image_cache == {}
    assert not Path(path).exists()
    assert not list(processor.raw_dir.iterdir())
    assert hints.desc == "《可塑性记忆》艾拉"


def test_regular_analysis_failure_does_not_store_or_cache_image(tmp_path):
    processor, path = _real_processor(tmp_path)
    processor.classify_image = AsyncMock(side_effect=RuntimeError("upstream 502"))
    processor._store_and_index_image = AsyncMock(wraps=processor._store_and_index_image)
    index = {}

    with pytest.raises(RuntimeError, match="upstream 502"):
        asyncio.run(processor.process_image(None, path, is_temp=True, idx=index))

    assert index == {}
    assert processor._image_cache == {}
    processor._store_and_index_image.assert_not_awaited()
    assert not list(processor.categories_dir.rglob("*.png"))
    assert not Path(path).exists()
    assert not list(processor.raw_dir.iterdir())


def test_force_capture_keeps_similar_image_but_rejects_exact_duplicate(tmp_path):
    processor, path = _real_processor(tmp_path)
    existing = tmp_path / "existing.png"
    Image.new("RGB", (48, 48), (200, 150, 100)).save(existing)
    processor.classify_image = AsyncMock(return_value=(
        "happy", ["笑"], "微笑的角色", "happy", [], "", [],
    ))
    index = {}

    async def run():
        existing_phash = await processor._phash_service.compute_phash(str(existing))
        incoming_phash = await processor._phash_service.compute_phash(path)
        assert existing_phash and existing_phash == incoming_phash
        assert existing.read_bytes() != Path(path).read_bytes()
        processor.plugin.db_service = SimpleNamespace(
            hash_exists=lambda _hash: False,
            get_phash_map=lambda: {str(existing): existing_phash},
        )

        assert await processor.process_image(None, path, idx=index) == (False, None)
        processor.classify_image.assert_not_awaited()
        succeeded, returned_index = await processor.process_image(
            None, path, idx=index, force_capture=True,
        )
        assert succeeded and returned_index is index
        saved_path = next(iter(index))
        assert Path(saved_path).exists()
        assert Path(saved_path).read_bytes() == Path(path).read_bytes()

        assert await processor.process_image(
            None, path, idx=index, force_capture=True,
        ) == (False, None)
        assert len(index) == 1
        processor.classify_image.assert_awaited_once()

    asyncio.run(run())


def test_external_cancellation_does_not_fallback_or_store_force_image(tmp_path):
    processor, path = _real_processor(tmp_path)
    processor._store_and_index_image = AsyncMock(wraps=processor._store_and_index_image)
    index = {}

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def classify(**_kwargs):
            started.set()
            await release.wait()
            raise RuntimeError("would otherwise trigger forced fallback")

        processor.classify_image = AsyncMock(side_effect=classify)
        task = asyncio.create_task(processor.process_image(
            None, path, is_temp=True, idx=index, force_capture=True,
        ))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert index == {}
    assert processor._image_cache == {}
    assert processor._processing_hashes == set()
    processor._store_and_index_image.assert_not_awaited()
    assert not list(processor.categories_dir.rglob("*.png"))
    assert not Path(path).exists()
    assert not list(processor.raw_dir.iterdir())


@pytest.mark.parametrize("is_temp", [True, False])
@pytest.mark.parametrize("pause_after_transfer", [True, False])
def test_cancel_during_raw_transfer_waits_for_thread_and_cleans_destination(
    tmp_path, monkeypatch, is_temp, pause_after_transfer,
):
    processor, path = _real_processor(tmp_path)
    processor.classify_image = AsyncMock()
    processor._store_and_index_image = AsyncMock(wraps=processor._store_and_index_image)
    index = {}
    release = threading.Event()
    original_transfer = shutil.move if is_temp else shutil.copy2

    async def run():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocked_transfer(source, destination):
            if pause_after_transfer:
                original_transfer(source, destination)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "file-operation thread was not released"
            if not pause_after_transfer:
                original_transfer(source, destination)

        monkeypatch.setattr(shutil, "move" if is_temp else "copy2", blocked_transfer)
        task = asyncio.create_task(processor.process_image(
            None, path, is_temp=is_temp, idx=index, force_capture=True,
        ))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            # Reload and an outer timeout may both cancel the same operation.
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert index == {}
    assert processor._image_cache == {}
    assert processor._processing_hashes == set()
    assert Path(path).exists() is not is_temp
    assert not list(processor.raw_dir.iterdir())
    assert not list(processor.categories_dir.rglob("*.png"))
    processor.classify_image.assert_not_awaited()
    processor._store_and_index_image.assert_not_awaited()
