"""跨页全选 / 清空图库的 Web API：/images/hashes、/images/clear、/pending/ids。"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db.database_service import DatabaseService
from astrbot_plugin_meme_magpie import plugin_api as plugin_api_module
from astrbot_plugin_meme_magpie.plugin_api import PluginAPI


@pytest.fixture()
def quart_stub(monkeypatch):
    """把 quart 的 request/jsonify 换成可控桩：jsonify 直接回传 dict。"""
    monkeypatch.setattr(plugin_api_module, "jsonify", lambda payload: payload)

    def _set(*, args=None, payload=None):
        async def get_json():
            return payload

        monkeypatch.setattr(
            plugin_api_module,
            "request",
            types.SimpleNamespace(
                args=args or {},
                get_json=get_json,
                method="POST" if payload is not None else "GET",
            ),
        )

    return _set


def _api(*, db=None, index=None, base_dir=None, processor=None):
    if db is None and index is not None:
        # 有 DB 但库里是空的：走 _get_index() 的索引回退分支
        db = types.SimpleNamespace(
            count_total=lambda: 0,
            get_index_cache_readonly=lambda: dict(index),
        )
    cfg = types.SimpleNamespace(categories=[], get_category_info=lambda: [])
    plugin = types.SimpleNamespace(
        plugin_config=cfg,
        db_service=db,
        base_dir=base_dir or Path("."),
        cache_service=None,
    )
    if processor is not None:
        plugin.image_processor_service = processor
    return PluginAPI(plugin)


class TestImageHashes:
    @pytest.mark.asyncio
    async def test_uses_db_filters(self, quart_stub):
        calls: dict = {}

        def get_emoji_hashes(**kwargs):
            calls.update(kwargs)
            return ["h1", "h2"]

        db = types.SimpleNamespace(count_total=lambda: 2, get_emoji_hashes=get_emoji_hashes)
        api = _api(db=db)
        quart_stub(
            args={
                "q": "Cat",
                "category": "happy",
                "favorite_only": "true",
                "character": "neurosama",
            }
        )

        result = await api.handle_image_hashes()

        assert result == {"success": True, "hashes": ["h1", "h2"], "total": 2}
        assert calls == {
            "category": "happy",
            "search_query": "cat",
            "favorite_only": True,
            "character": "neurosama",
            "work": None,
        }

    @pytest.mark.asyncio
    async def test_falls_back_to_index_scan(self, quart_stub, tmp_path):
        keep = tmp_path / "a.gif"
        other = tmp_path / "b.gif"
        keep.write_bytes(b"x")
        other.write_bytes(b"x")
        index = {
            str(keep): {"hash": "h1", "category": "happy", "desc": "cat"},
            str(other): {"hash": "h2", "category": "sad", "desc": "dog"},
            str(tmp_path / "missing.gif"): {"hash": "h3", "category": "happy"},
        }
        api = _api(index=index)

        quart_stub(args={})
        assert sorted((await api.handle_image_hashes())["hashes"]) == ["h1", "h2"]

        quart_stub(args={"category": "happy"})
        assert (await api.handle_image_hashes())["hashes"] == ["h1"]

        quart_stub(args={"q": "dog"})
        assert (await api.handle_image_hashes())["hashes"] == ["h2"]


class TestPendingIds:
    @pytest.mark.asyncio
    async def test_uses_db_filters(self, quart_stub):
        calls: dict = {}

        def get_pending_ids(category=None, search_query=None):
            calls.update({"category": category, "search_query": search_query})
            return [3, 1]

        api = _api(db=types.SimpleNamespace(get_pending_ids=get_pending_ids))
        quart_stub(args={"q": "Cat", "category": "happy"})

        result = await api.handle_pending_ids()

        assert result == {"success": True, "ids": [3, 1], "total": 2}
        assert calls == {"category": "happy", "search_query": "cat"}

    @pytest.mark.asyncio
    async def test_without_db_returns_empty(self, quart_stub):
        api = _api(db=None)
        quart_stub(args={})

        assert await api.handle_pending_ids() == {"success": True, "ids": [], "total": 0}


class TestClearImages:
    @pytest.mark.asyncio
    async def test_requires_confirmation(self, quart_stub):
        api = _api(db=types.SimpleNamespace(get_index_cache_readonly=lambda: {}))
        quart_stub(payload={})

        result = await api.handle_clear_images()

        assert result["success"] is False
        assert result["error"] == "缺少确认标记"

    @pytest.mark.asyncio
    async def test_deletes_files_index_and_thumbnails(self, quart_stub, tmp_path):
        img_a = tmp_path / "categories" / "happy" / "a.gif"
        img_b = tmp_path / "categories" / "sad" / "b.gif"
        img_a.parent.mkdir(parents=True)
        img_b.parent.mkdir(parents=True)
        img_a.write_bytes(b"image-a")
        img_b.write_bytes(b"image-b")
        thumbs = tmp_path / "thumb_cache"
        thumbs.mkdir()
        (thumbs / "h1_300.jpg").write_bytes(b"thumb-a")
        (thumbs / "h2_300.jpg").write_bytes(b"thumb-b")

        index = {
            str(img_a): {"hash": "h1", "category": "happy"},
            str(img_b): {"hash": "h2", "category": "sad"},
        }
        deleted_paths: list[str] = []

        async def delete_paths(paths):
            deleted_paths.extend(paths)

        db = types.SimpleNamespace(
            get_index_cache_readonly=lambda: dict(index),
            delete_paths=delete_paths,
        )

        class _Processor:
            def __init__(self):
                self.invalidated: list[str] = []

            def invalidate_cache(self, image_hash):
                self.invalidated.append(image_hash)

        proc = _Processor()
        api = _api(db=db, base_dir=tmp_path, processor=proc)
        quart_stub(payload={"confirm": True})

        result = await api.handle_clear_images()

        assert result["success"] is True
        assert result["deleted"] == 2
        assert result["index_removed"] == 2
        assert result["failed"] == 0
        assert not img_a.exists() and not img_b.exists()
        assert not list(thumbs.iterdir())
        assert sorted(deleted_paths) == sorted(index.keys())
        assert sorted(proc.invalidated) == ["h1", "h2"]

    @pytest.mark.asyncio
    async def test_missing_files_still_drop_index_rows(self, quart_stub, tmp_path):
        """文件已被外部删除时，索引行也要顺手清掉（幂等）。"""
        index = {
            str(tmp_path / "gone.gif"): {"hash": "h-old", "category": "happy"},
        }
        deleted_paths: list[str] = []

        async def delete_paths(paths):
            deleted_paths.extend(paths)

        db = types.SimpleNamespace(
            get_index_cache_readonly=lambda: dict(index),
            delete_paths=delete_paths,
        )
        api = _api(db=db, base_dir=tmp_path)
        quart_stub(payload={"confirm": True})

        result = await api.handle_clear_images()

        assert result["success"] is True
        assert result["deleted"] == 0
        assert result["index_removed"] == 1
        assert deleted_paths == list(index.keys())


def _png_bytes(color=(255, 0, 0)) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


async def _export_plugin(tmp_path: Path):
    """真实 DatabaseService + 磁盘文件，供导出任务跑通。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = DatabaseService(data_dir / "emoji.db")
    cat_dir = data_dir / "categories" / "happy"
    cat_dir.mkdir(parents=True)
    img_a = cat_dir / "a.png"
    img_b = cat_dir / "b.png"
    img_a.write_bytes(_png_bytes((255, 0, 0)))
    img_b.write_bytes(_png_bytes((0, 255, 0)))
    await db.insert_batch(
        [
            {"path": str(img_a), "hash": "h1", "category": "happy", "desc": "A"},
            {"path": str(img_b), "hash": "h2", "category": "sad", "desc": "B"},
        ]
    )
    return _api(db=db, base_dir=data_dir), db, data_dir


class TestExportEndpoints:
    @pytest.mark.asyncio
    async def test_start_status_download_round(self, quart_stub, tmp_path, monkeypatch):
        import asyncio
        import zipfile

        api, _db, data_dir = await _export_plugin(tmp_path)
        quart_stub(payload={"scope": "all", "include_pending": False, "include_blacklist": False})

        started = await api.handle_export_start()
        assert started["success"] is True
        task_id = started["task"]["task_id"]

        task = None
        for _ in range(200):
            quart_stub(args={"task_id": task_id})
            status = await api.handle_export_status()
            assert status["success"] is True
            task = status["task"]
            if task["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)

        assert task is not None
        assert task["status"] == "completed", task.get("error")
        assert task["library"] == 2
        assert task["bytes"] > 0
        assert task["download_url"].endswith(f"task_id={task_id}")

        archive_path = data_dir / "exports" / f"{task_id}.zip"
        assert archive_path.is_file()
        with zipfile.ZipFile(archive_path) as archive:
            assert "manifest.json" in archive.namelist()

        sent: dict = {}

        async def fake_send_file(path, **kwargs):
            sent["path"] = path
            sent["kwargs"] = kwargs
            return {"sent": path}

        monkeypatch.setattr(plugin_api_module, "send_file", fake_send_file)
        quart_stub(args={"task_id": task_id})
        download = await api.handle_export_download()
        assert download == {"sent": str(archive_path)}
        assert sent["kwargs"]["as_attachment"] is True
        assert sent["kwargs"]["attachment_filename"].endswith(".zip")

    @pytest.mark.asyncio
    async def test_filtered_scope_requires_matches(self, quart_stub, tmp_path):
        api, _db, _data_dir = await _export_plugin(tmp_path)
        quart_stub(payload={"scope": "filtered", "q": "no-such-description"})

        result = await api.handle_export_start()

        assert result["success"] is False
        assert "没有可导出" in result["error"]

    @pytest.mark.asyncio
    async def test_status_unknown_task_returns_error(self, quart_stub):
        api = _api(db=None)
        quart_stub(args={"task_id": "nope"})

        result = await api.handle_export_status()

        assert result["success"] is False
