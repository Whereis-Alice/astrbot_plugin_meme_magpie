"""跨页全选 / 清空图库的 Web API：/images/hashes、/images/clear、/pending/ids。"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
