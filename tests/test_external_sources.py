"""外部表情包源：协议解析、安全边界、导入落库与容量豁免的回归测试。

外部源会把别人打包好的整套表情直接写进本机表情库，所以三条底线必须一直成立：

1. **解析层不越界**：压缩包成员不能穿越目录，远端地址不能指向内网或本地文件；
2. **凭据不外泄**：跨域跳转后必须丢掉 Authorization / X-API-Key，回给前端的
   源记录与溯源信息里不能残留 token；
3. **结果可解释**：重复图算「重复」不算「失败」，角色名只认已注册角色，
   导入进来的托管副本不参与容量淘汰（否则导入一个大包就会被后台悄悄啃掉）。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from core.db.database_service import DatabaseService
from core.events.event_handler import EventHandler
from core.sources.github_source import GitHubSource, _parse_repository
from core.sources.http_source import HTTPSource, validate_source_url
from core.sources.models import (
    ExternalSourceError,
    ExternalSourceSecurityError,
    SourceItem,
)
from core.sources.pack_source import PackSource, safe_member_path
from core.sources.source_service import SourceService
from core.util.retention import (
    count_capacity_managed,
    is_capacity_exempt,
    retention_class_of,
)


def _png_bytes(color=(255, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


class _Config:
    """最小配置桩：只提供 SourceService 真正会读的字段与方法。"""

    external_sources_enabled = True
    external_source_allow_http = False
    external_source_default_review = False
    external_source_max_items = 100
    external_source_max_image_bytes = 1024 * 1024
    external_source_max_archive_bytes = 10 * 1024 * 1024
    external_source_max_uncompressed_bytes = 20 * 1024 * 1024
    external_source_max_pixels = 1_000_000
    content_filtration = False
    steal_pool_capacity = 200
    max_reg_num = 100

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.categories_dir = data_dir / "categories"
        self.pending_dir = data_dir / "pending"
        # 实例属性，避免不同用例之间互相污染
        self.characters: list[str] = []
        self.character_info: dict[str, dict] = {}

    def get_categories(self):
        return ["happy", "sad", "confused"]

    def get_characters(self):
        return list(self.characters)

    def normalize_category_strict(self, value):
        return value if value in self.get_categories() else None

    def closest_category(self, value):
        return "happy" if value in {"开心", "joy"} else "confused"

    def ensure_category_dir(self, category):
        target = self.categories_dir / category
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_characters(self):
        return None

    def save_character_info(self):
        return None


def _service(tmp_path: Path) -> tuple[SourceService, DatabaseService]:
    data_dir = tmp_path / "magpie-data"
    data_dir.mkdir()
    db = DatabaseService(data_dir / "emoji.db")
    config = _Config(data_dir)
    plugin = SimpleNamespace(
        base_dir=data_dir,
        db_service=db,
        plugin_config=config,
        meme_selector=None,
    )
    return SourceService(plugin), db


def _pack(tmp_path: Path) -> Path:
    """造一个符合公开协议的 Meme Pack 目录（manifest + memes_data + 图片）。"""

    root = tmp_path / "manager-pack"
    image_dir = root / "memes" / "happy"
    image_dir.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "demo-pack",
                "name": "Demo Pack",
                "version": "1.0.0",
                "categories": ["happy"],
                "license": "CC0-1.0",
            }
        ),
        encoding="utf-8",
    )
    (root / "memes_data.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "smile-1",
                        "relative_path": "memes/happy/smile.png",
                        "category": "happy",
                        "caption": "角色开心挥手",
                        "visible_text": "你好",
                        "tags": ["挥手", "问候", "category:happy"],
                        "scenes": ["打招呼"],
                        "character": "未注册角色",
                        "work": "示例作品",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (image_dir / "smile.png").write_bytes(_png_bytes())
    return root


# ---------------------------------------------------------------------------
# 1) 安全边界：路径穿越与 SSRF
# ---------------------------------------------------------------------------


def test_pack_member_rejects_path_traversal():
    with pytest.raises(ExternalSourceSecurityError):
        safe_member_path("../../outside.png")
    with pytest.raises(ExternalSourceSecurityError):
        safe_member_path("C:\\outside.png")
    assert safe_member_path("memes/happy/a.png") == "memes/happy/a.png"


def test_http_source_blocks_local_and_non_http_urls():
    with pytest.raises(ExternalSourceSecurityError):
        validate_source_url("http://127.0.0.1/catalog", allow_http=True)
    with pytest.raises(ExternalSourceSecurityError):
        validate_source_url("file:///tmp/catalog.json")
    with pytest.raises(ExternalSourceSecurityError):
        validate_source_url("https://example.com:99999/catalog.json", resolve_dns=False)
    with pytest.raises(ExternalSourceSecurityError):
        validate_source_url("https://user:pass@example.com/c.json", resolve_dns=False)
    # 明文 HTTP 默认关闭，必须显式开配置才放行
    with pytest.raises(ExternalSourceSecurityError):
        validate_source_url("http://example.com/catalog.json", resolve_dns=False)
    assert validate_source_url(
        "https://example.com/catalog.json#frag", resolve_dns=False
    ) == "https://example.com/catalog.json"


def test_github_repository_rejects_non_github_hosts():
    with pytest.raises(ExternalSourceSecurityError):
        _parse_repository({"repository": "https://evil.example/owner/repo"})


def test_github_repository_descriptor_supports_tree_and_query_ref():
    owner, repo, ref, subpath = _parse_repository(
        {"repository": "https://github.com/Example/Meme-Pack/tree/feature%2Fv3/memes"}
    )
    assert (owner, repo, ref, subpath) == ("Example", "Meme-Pack", "feature/v3", "memes")

    owner, repo, ref, subpath = _parse_repository(
        {"repository": "owner/repo?ref=release%2F2026&subpath=packs/main"}
    )
    assert (owner, repo, ref, subpath) == ("owner", "repo", "release/2026", "packs/main")


# ---------------------------------------------------------------------------
# 2) 凭据隔离：跨域跳转后必须丢掉 Authorization / X-API-Key
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, *, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {}
        self.content = self
        self._chunks = list(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeSession:
    closed = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, headers, allow_redirects):
        self.calls.append((url, dict(headers), allow_redirects))
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_http_source_drops_credentials_after_cross_origin_redirect():
    source = HTTPSource(
        "https://api.example/catalog.json",
        headers={
            "Authorization": "Bearer secret",
            "X-API-Key": "key",
            "User-Agent": "test",
        },
    )
    session = _FakeSession(
        [
            _FakeResponse(302, headers={"Location": "https://cdn.example/meme.png"}),
            _FakeResponse(200, chunks=[b"image"]),
        ]
    )
    source._session = session

    assert await source._request_bytes(source.endpoint, max_bytes=1024) == b"image"
    assert "Authorization" in session.calls[0][1]
    assert "X-API-Key" in session.calls[0][1]
    assert "Authorization" not in session.calls[1][1]
    assert "X-API-Key" not in session.calls[1][1]
    # 非敏感头照常带上，别把 UA 也一起丢了
    assert session.calls[1][1].get("User-Agent") == "test"


def test_github_source_drops_credentials_after_cross_origin_redirect(tmp_path):
    source = GitHubSource(
        {"repository": "https://github.com/example/demo-pack", "ref": "v1"},
        cache_dir=tmp_path / "cache",
        headers={"Authorization": "Bearer secret", "X-API-Key": "key"},
    )
    initial = source._headers_for_url(
        "https://api.github.com/repos/example/demo-pack",
        initial_origin=("https", "api.github.com", 443),
        accept_json=True,
    )
    redirected = source._headers_for_url(
        "https://downloads.example/archive.zip",
        initial_origin=("https", "api.github.com", 443),
    )
    assert "Authorization" in initial
    assert "X-API-Key" in initial
    assert "Authorization" not in redirected
    assert "X-API-Key" not in redirected


def test_public_source_redacts_credentials():
    source = SourceService._public_source(
        {
            "source_id": "http:test",
            "source_type": "http_json",
            "endpoint": "https://example.com/catalog?token=secret&page=1",
            "config": {
                "endpoint": "https://example.com/catalog?api_key=secret",
                "repository": "https://github.com/example/repo?token=secret&ref=v1",
                "headers": {"Authorization": "Bearer secret"},
            },
        }
    )
    assert "secret" not in source["endpoint"]
    assert "secret" not in str(source["config"]["endpoint"])
    assert "secret" not in str(source["config"]["repository"])
    assert source["config"]["headers"]["Authorization"] == "********"


def test_item_provenance_redacts_credentials_in_image_url():
    item = SourceItem(
        external_id="one",
        source_url="https://cdn.example/meme.png?token=secret&size=small",
        metadata={"url": "https://cdn.example/meme.png?api_key=secret", "id": "one"},
    )
    public = SourceService._public_item_metadata(item)
    assert "secret" not in str(public)
    assert "size=small" in public["source_url"]


def test_inspection_manifest_redacts_nested_credentials(tmp_path):
    service, _db = _service(tmp_path)
    safe = service._json_safe(
        {
            "name": "Public pack",
            "license": "CC-BY-4.0",
            "source": {
                "repository": "https://github.com/example/repo?token=secret&ref=v1",
                "api_key": "secret",
            },
        }
    )
    assert safe["name"] == "Public pack"
    assert safe["license"] == "CC-BY-4.0"
    assert "secret" not in str(safe)
    assert "ref=v1" in safe["source"]["repository"]


# ---------------------------------------------------------------------------
# 3) 协议解析：Meme Pack 目录 / 压缩包 / JSON 目录 / GitHub 仓库
# ---------------------------------------------------------------------------


def test_pack_inspection_reads_public_protocol_metadata(tmp_path):
    inspection = PackSource(_pack(tmp_path), max_items=10).inspect()
    assert inspection.ok
    assert inspection.source_id == "pack:demo-pack"
    assert inspection.name == "Demo Pack"
    assert len(inspection.items) == 1
    item = inspection.items[0]
    assert item.external_id == "smile-1"
    assert item.text_description == "角色开心挥手"
    assert item.visible_text == "你好"
    assert item.license == "CC0-1.0"
    assert item.work == "示例作品"


def test_pack_inspection_reads_semantic_metadata_v2(tmp_path):
    root = _pack(tmp_path)
    (root / "semantic_metadata.json").write_text(
        json.dumps(
            {
                "pack_id": "demo-pack",
                "schema_version": "2.0",
                "images": {
                    "entry-1": {
                        "relative_path": "memes/happy/smile.png",
                        "entry_id": "semantic-entry-1",
                        "content_sha256": "remote-sha",
                        "caption": "来自语义包的描述",
                        "tags": ["category:happy", "语义标签"],
                        "visible_text": "语义文字",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    item = PackSource(root).inspect().items[0]
    assert item.external_id == "semantic-entry-1"
    assert item.text_description == "来自语义包的描述"
    assert item.tags == ["category:happy", "语义标签"]
    assert item.visible_text == "语义文字"
    assert item.metadata["content_sha256"] == "remote-sha"


def test_pack_export_descriptor_supplies_nested_pack_identity(tmp_path):
    root = tmp_path / "pack"
    (root / "memes" / "happy").mkdir(parents=True)
    (root / "memes" / "happy" / "one.png").write_bytes(_png_bytes())
    (root / "meme_pack_export.json").write_text(
        json.dumps({"format": "astrbot-meme-pack", "pack": {"id": "nested-id"}}),
        encoding="utf-8",
    )
    assert PackSource(root).inspect().source_id == "pack:nested-id"


def test_pack_rejects_invalid_or_empty_archive(tmp_path):
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(ExternalSourceError, match="invalid or unsupported"):
        PackSource(invalid).inspect()
    empty = tmp_path / "empty"
    empty.mkdir()
    inspection = PackSource(empty).inspect()
    assert not inspection.ok
    assert "no supported images" in inspection.errors[0]


@pytest.mark.asyncio
async def test_http_catalog_follows_cursor_and_normalizes_items(monkeypatch):
    source = HTTPSource("https://8.8.8.8/catalog.json", max_items=3)

    async def fake_json(url):
        if "cursor=page-2" in url:
            return {
                "items": [
                    {
                        "id": "two",
                        "url": "/two.png",
                        "category": "sad",
                        "caption": "second",
                    }
                ]
            }
        return {
            "id": "remote-demo",
            "name": "Remote Demo",
            "license": "CC-BY-4.0",
            "items": [
                {
                    "id": "one",
                    "image_url": "/one.png",
                    "emotion": "happy",
                    "tags": ["hello"],
                }
            ],
            "next_cursor": "page-2",
        }

    monkeypatch.setattr(source, "_request_json", fake_json)
    inspection = await source.inspect()
    assert inspection.source_id == "http:remote-demo"
    assert [item.external_id for item in inspection.items] == ["one", "two"]
    assert inspection.items[0].source_url == "https://8.8.8.8/one.png"
    assert inspection.categories == ["happy", "sad"]
    # 目录级 license 会补给没有自带 license 的条目
    assert inspection.items[1].license == "CC-BY-4.0"


@pytest.mark.asyncio
async def test_github_repository_archive_strips_archive_root(tmp_path, monkeypatch):
    source_pack = _pack(tmp_path)
    archive_path = tmp_path / "github.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in source_pack.rglob("*"):
            if path.is_file():
                archive.write(path, Path("demo-pack-v1") / path.relative_to(source_pack))
    source = GitHubSource(
        {"repository": "https://github.com/example/demo-pack", "ref": "v1"},
        cache_dir=tmp_path / "cache",
    )

    async def fake_download():
        return archive_path

    monkeypatch.setattr(source, "_download_archive", fake_download)
    inspection = await source.inspect()
    assert inspection.source_type == "github"
    assert inspection.source_id == "github:example/demo-pack@v1"
    assert inspection.items[0].relative_path == "memes/happy/smile.png"
    assert inspection.items[0].text_description == "角色开心挥手"
    assert inspection.manifest["source"]["repo"] == "example/demo-pack"
    assert source.read_item(inspection.items[0]) == (
        source_pack / "memes" / "happy" / "smile.png"
    ).read_bytes()
    await source.close()


@pytest.mark.asyncio
async def test_service_infers_github_from_url_without_explicit_type(tmp_path, monkeypatch):
    service, _db = _service(tmp_path)

    class _Reader:
        async def inspect(self):
            return PackSource(_pack(tmp_path)).inspect()

        async def close(self):
            return None

    monkeypatch.setattr(service, "_github_reader", lambda _spec: _Reader())
    inspection, reader = await service.inspect(
        {"url": "https://github.com/example/demo-pack"}
    )
    assert inspection.source_type == "meme_pack"
    assert reader is not None


@pytest.mark.asyncio
async def test_duplicate_item_ids_are_reported_as_an_error(tmp_path, monkeypatch):
    """两条同 ID 会让溯源表互相覆盖，必须在导入前就拦下来。"""

    service, _db = _service(tmp_path)
    inspection = PackSource(_pack(tmp_path)).inspect()
    inspection.items.append(inspection.items[0])
    monkeypatch.setattr(
        service,
        "_pack_reader",
        lambda _path: SimpleNamespace(inspect=lambda: inspection, close=lambda: None),
    )
    result, _reader = await service.inspect({"source_type": "meme_pack", "path": "x"})
    assert not result.ok
    assert "duplicate item IDs" in result.errors[0]


@pytest.mark.asyncio
async def test_inspect_is_refused_when_the_feature_is_switched_off(tmp_path):
    service, _db = _service(tmp_path)
    service.plugin.plugin_config.external_sources_enabled = False
    with pytest.raises(ExternalSourceError, match="disabled"):
        await service.inspect({"source_type": "meme_pack", "path": str(tmp_path)})


@pytest.mark.asyncio
async def test_inspect_dict_reports_capacity_without_counting_external_items(tmp_path):
    service, _db = _service(tmp_path)
    payload = await service.inspect_dict(
        {"source_type": "meme_pack", "path": str(_pack(tmp_path))}
    )
    assert payload["item_count"] == 1
    assert payload["items"][0]["work"] == "示例作品"
    capacity = payload["capacity"]
    assert capacity["incoming"] == 1
    assert capacity["external_items_are_protected"] is True
    assert capacity["configured_limit"] == 100
    assert capacity["would_exceed_limit"] is False


# ---------------------------------------------------------------------------
# 4) 导入落库：托管副本、去重、角色采纳
# ---------------------------------------------------------------------------


def _two_item_pack(tmp_path: Path) -> Path:
    """两张图的最小包，用来观察逐条进度与暂停/恢复。"""

    root = tmp_path / "two-item-pack"
    image_dir = root / "memes" / "happy"
    image_dir.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps({"id": "two-pack", "name": "Two", "categories": ["happy"]}),
        encoding="utf-8",
    )
    (root / "memes_data.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "a",
                        "relative_path": "memes/happy/a.png",
                        "category": "happy",
                        "caption": "第一张",
                    },
                    {
                        "id": "b",
                        "relative_path": "memes/happy/b.png",
                        "category": "happy",
                        "caption": "第二张",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (image_dir / "a.png").write_bytes(_png_bytes((10, 20, 30)))
    (image_dir / "b.png").write_bytes(_png_bytes((40, 50, 60)))
    return root


async def _wait_until(predicate, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


@pytest.mark.asyncio
async def test_pack_import_copies_tracks_and_deduplicates(tmp_path):
    """导入是"复制一份托管副本"，不是搬走源文件；重复图只算重复不算失败。"""

    service, db = _service(tmp_path)
    pack = _pack(tmp_path)
    source_file = pack / "memes" / "happy" / "smile.png"

    job = await service.import_now(
        {"source_type": "meme_pack", "path": str(pack), "scope_mode": "public"}
    )
    assert job["status"] == "completed", job.get("error")
    assert (job["imported"], job["duplicates"], job["failed"]) == (1, 0, 0)
    assert job["errors"] == []

    index = db.get_index_cache_readonly()
    assert len(index) == 1
    stored_path, info = next(iter(index.items()))
    assert Path(stored_path) != source_file
    assert Path(stored_path).is_file()
    assert source_file.is_file()
    assert info["retention_class"] == "external"
    assert info["source"] == "external:meme_pack"
    assert info["add_method"] == "external_import"
    assert info["desc"] == "角色开心挥手"
    assert info["overlay_text"] == "你好"
    assert info["work"] == "示例作品"
    # category: 前缀是分类标记而不是检索标签，落库时会被丢掉
    assert info["tags"] == ["挥手", "问候"]
    assert info["scenes"] == ["打招呼"]

    items = db.get_source_items("pack:demo-pack")
    assert len(items) == 1
    assert items[0]["external_id"] == "smile-1"
    assert items[0]["license"] == "CC0-1.0"
    assert items[0]["path"] == stored_path
    assert items[0]["stale"] == 0

    again = await service.import_now({"source_type": "meme_pack", "path": str(pack)})
    assert (again["imported"], again["duplicates"], again["failed"]) == (0, 1, 0)
    assert db.count_total() == 1
    assert source_file.is_file()


@pytest.mark.asyncio
async def test_direct_import_deduplicates_against_the_pending_pool(tmp_path):
    """待审核池里已有同一张图时，导入必须让路，不能再塞一份进正式库。"""

    service, db = _service(tmp_path)
    image_hash = hashlib.sha256(_png_bytes()).hexdigest()
    pending_file = tmp_path / "already-pending.png"
    pending_file.write_bytes(_png_bytes())
    assert await db.insert_pending(
        {"path": str(pending_file), "hash": image_hash, "category": "happy"}
    )

    job = await service.import_now(
        {"source_type": "meme_pack", "path": str(_pack(tmp_path))}
    )
    assert job["status"] == "completed"
    assert (job["imported"], job["duplicates"], job["failed"]) == (0, 1, 0)
    assert db.count_total() == 0
    assert db.count_pending() == 1
    # 溯源仍然记账，只是指向待审核文件
    linked = db.get_source_items("pack:demo-pack")
    assert linked[0]["metadata"]["pending_path"] == str(pending_file)


@pytest.mark.asyncio
async def test_character_assignment_updates_an_existing_pending_duplicate(tmp_path):
    """整批指定角色时，池子里那张缺角色的同图会顺带补上，不用手动再点一遍。"""

    service, db = _service(tmp_path)
    image_hash = hashlib.sha256(_png_bytes()).hexdigest()
    pending_file = tmp_path / "already-pending.png"
    pending_file.write_bytes(_png_bytes())
    await db.insert_pending(
        {"path": str(pending_file), "hash": image_hash, "category": "happy"}
    )

    job = await service.import_now(
        {
            "source_type": "meme_pack",
            "path": str(_pack(tmp_path)),
            "character": "series-one",
            "create_character": True,
        }
    )
    assert job["duplicates"] == 1
    pending = db.get_pending_by_hash(image_hash)
    assert pending is not None
    assert pending["character"] == "series-one"


@pytest.mark.asyncio
async def test_import_can_create_and_assign_a_character(tmp_path):
    service, db = _service(tmp_path)
    config = service.plugin.plugin_config

    job = await service.import_now(
        {
            "source_type": "meme_pack",
            "path": str(_pack(tmp_path)),
            "character": "Neuro-Sama",
            "create_character": True,
        }
    )
    assert job["status"] == "completed", job.get("error")
    assert "neuro-sama" in config.characters
    assert config.character_info["neuro-sama"]["name"] == "Neuro-Sama"
    info = next(iter(db.get_index_cache_readonly().values()))
    assert info["character"] == "neuro-sama"


@pytest.mark.asyncio
async def test_unknown_character_requires_explicit_creation(tmp_path):
    """没勾"顺便创建角色"就直接失败，避免悄悄往角色表里塞脏值。"""

    service, db = _service(tmp_path)
    config = service.plugin.plugin_config

    job = await service.import_now(
        {
            "source_type": "meme_pack",
            "path": str(_pack(tmp_path)),
            "character": "Neuro-Sama",
        }
    )
    assert job["status"] == "failed"
    assert "not registered" in job["error"]
    assert config.characters == []
    assert db.count_total() == 0
    assert db.get_sources() == []


@pytest.mark.asyncio
async def test_rejected_scope_does_not_create_character_or_source(tmp_path):
    """局部作用域没给归属对象 → 整批拒绝，且不留下半个角色或半个源。"""

    service, db = _service(tmp_path)
    config = service.plugin.plugin_config

    job = await service.import_now(
        {
            "source_type": "meme_pack",
            "path": str(_pack(tmp_path)),
            "scope_mode": "local",
            "character": "Neuro-Sama",
            "create_character": True,
        }
    )
    assert job["status"] == "failed"
    assert "origin_target" in job["error"]
    assert config.characters == []
    assert db.get_sources() == []
    assert db.count_total() == 0


@pytest.mark.asyncio
async def test_pack_character_is_only_adopted_when_registered(tmp_path):
    """包里自带的角色名是自由文本，只有对上已注册角色才写进库。"""

    service, db = _service(tmp_path)
    pack = _pack(tmp_path)
    config = service.plugin.plugin_config

    first = await service.import_now({"source_type": "meme_pack", "path": str(pack)})
    assert first["imported"] == 1
    info = next(iter(db.get_index_cache_readonly().values()))
    assert info["character"] == ""
    # 原文不丢，仍留在溯源信息里备查
    assert db.get_source_items("pack:demo-pack")[0]["metadata"]["character"] == "未注册角色"

    config.characters = ["未注册角色"]
    second = await service.import_now({"source_type": "meme_pack", "path": str(pack)})
    assert (second["imported"], second["duplicates"]) == (0, 1)
    assert db.count_total() == 1
    info = next(iter(db.get_index_cache_readonly().values()))
    assert info["character"] == "未注册角色"


@pytest.mark.asyncio
async def test_review_mode_routes_items_into_the_pending_pool(tmp_path):
    service, db = _service(tmp_path)

    job = await service.import_now(
        {"source_type": "meme_pack", "path": str(_pack(tmp_path)), "review": True}
    )
    assert job["status"] == "completed", job.get("error")
    assert (job["pending"], job["imported"]) == (1, 0)
    assert db.count_total() == 0
    assert db.count_pending() == 1


@pytest.mark.asyncio
async def test_review_import_is_refused_when_the_pending_pool_is_full(tmp_path):
    service, db = _service(tmp_path)
    service.plugin.plugin_config.steal_pool_capacity = 0

    job = await service.import_now(
        {"source_type": "meme_pack", "path": str(_pack(tmp_path)), "review": True}
    )
    assert job["status"] == "failed"
    assert "pending pool capacity" in job["error"]
    assert db.count_pending() == 0
    assert db.get_sources() == []


@pytest.mark.asyncio
async def test_invalid_pack_image_is_reported_without_library_write(tmp_path):
    """坏图只让这一条失败并写进错误清单，不影响整批，也不会留下垃圾文件。"""

    service, db = _service(tmp_path)
    pack = _pack(tmp_path)
    (pack / "memes" / "happy" / "smile.png").write_bytes(b"not an image")

    job = await service.import_now({"source_type": "meme_pack", "path": str(pack)})
    assert job["status"] == "completed"
    assert (job["failed"], job["imported"], job["processed"]) == (1, 0, 1)
    assert job["errors"] and job["errors"][0].startswith("smile-1: ")
    assert db.count_total() == 0
    categories_dir = service.plugin.plugin_config.categories_dir
    assert list(categories_dir.rglob("*.png")) == []


# ---------------------------------------------------------------------------
# 5) 后台任务：并发名额、暂停/恢复、进度可见
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_job_slot_pause_and_resume(tmp_path, monkeypatch):
    """一次只允许一个导入在跑；暂停要真的停在下一条之前，恢复后接着跑完。"""

    service, db = _service(tmp_path)
    pack = _two_item_pack(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_read = service._read_source_item

    async def _gated_read(reader, item):
        entered.set()
        await release.wait()
        return await original_read(reader, item)

    monkeypatch.setattr(service, "_read_source_item", _gated_read)
    job = service.start_import({"source_type": "meme_pack", "path": str(pack)})
    job_id = job["job_id"]
    assert job["status"] == "queued"
    assert "_work_seconds" not in job

    with pytest.raises(ExternalSourceError, match="too many"):
        service.start_import({"source_type": "meme_pack", "path": str(pack)})

    await asyncio.wait_for(entered.wait(), timeout=10)
    active = service.active_job()
    assert active is not None and active["job_id"] == job_id
    assert active["total"] == 2

    assert service.pause_job(job_id) is True
    assert service.get_job(job_id)["status"] == "paused"
    # 暂停中仍然占用名额，否则一直暂停就能叠出无数个导入任务
    with pytest.raises(ExternalSourceError, match="too many"):
        service.start_import({"source_type": "meme_pack", "path": str(pack)})

    release.set()
    await _wait_until(lambda: int(service.get_job(job_id)["processed"]) == 1)
    await asyncio.sleep(0.05)
    paused = service.get_job(job_id)
    assert paused["status"] == "paused"
    assert paused["processed"] == 1

    assert service.resume_job(job_id) is True
    await asyncio.wait_for(service._tasks[job_id], timeout=10)
    final = service.get_job(job_id)
    assert final["status"] == "completed"
    assert (final["imported"], final["failed"]) == (2, 0)
    assert final["current_file"] == ""
    assert final["paused"] is False
    assert "_work_seconds" not in final
    assert service.active_job() is None
    assert db.count_total() == 2


@pytest.mark.asyncio
async def test_cancelling_a_paused_job_releases_the_slot(tmp_path, monkeypatch):
    service, _db = _service(tmp_path)
    pack = _two_item_pack(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _gated_read(_reader, _item):
        entered.set()
        await release.wait()
        return _png_bytes()

    monkeypatch.setattr(service, "_read_source_item", _gated_read)
    job_id = service.start_import({"source_type": "meme_pack", "path": str(pack)})["job_id"]
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert service.pause_job(job_id) is True

    # 取消时必须先放行暂停门，否则任务永远等不到取消生效
    assert await service.cancel_job(job_id) is True
    assert service.get_job(job_id)["status"] == "cancelled"
    assert service.active_job() is None
    release.set()


@pytest.mark.asyncio
async def test_staged_upload_cleanup_preserves_referenced_archives(tmp_path):
    """浏览器上传的压缩包：过期的清掉，仍被源引用的留着，源删掉后立刻释放。"""

    service, db = _service(tmp_path)
    upload_dir = service.import_dir / "uploads"
    upload_dir.mkdir(parents=True)
    old = upload_dir / "old.zip"
    recent = upload_dir / "recent.zip"
    referenced = upload_dir / "referenced.zip"
    for archive in (old, recent, referenced):
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("manifest.json", "{}")
    stale_time = time.time() - service.STAGED_UPLOAD_TTL_SECONDS - 60
    os.utime(old, (stale_time, stale_time))
    await db.upsert_source(
        {
            "source_id": "pack:referenced",
            "source_type": "meme_pack",
            "name": "Referenced",
            "endpoint": str(referenced),
            "config": {"source_type": "meme_pack", "path": str(referenced)},
        }
    )

    assert await service.cleanup_staged_uploads() == 1
    assert not old.exists()
    assert recent.is_file()
    assert referenced.is_file()

    assert await service.delete_source("pack:referenced") is True
    assert not referenced.exists()
    assert recent.is_file()


# ---------------------------------------------------------------------------
# 6) 库表与容量：新增是纯增量，外部源副本不参与淘汰
# ---------------------------------------------------------------------------


def test_external_schema_is_additive_and_records_its_version(tmp_path):
    db_path = tmp_path / "schema.db"
    DatabaseService(db_path)
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"emoji", "emoji_pending", "meme_source", "meme_source_item"} <= tables
        emoji_columns = {row[1] for row in conn.execute("PRAGMA table_info(emoji)")}
        assert "retention_class" in emoji_columns
        pending_columns = {row[1] for row in conn.execute("PRAGMA table_info(emoji_pending)")}
        assert "retention_class" in pending_columns
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'external_schema_version'"
        ).fetchone()
        assert version is not None and version[0] == "1"
    conn.close()


@pytest.mark.asyncio
async def test_source_registry_marks_missing_items_stale(tmp_path):
    _service_obj, db = _service(tmp_path)
    await db.upsert_source(
        {"source_id": "pack:demo", "source_type": "meme_pack", "name": "Demo"}
    )
    for external_id in ("a", "b"):
        assert await db.link_source_item(
            {"source_id": "pack:demo", "external_id": external_id, "path": None}
        )

    assert await db.mark_source_items_stale("pack:demo") == 2
    assert db.count_stale_source_items("pack:demo") == 2
    # 对账只依据本次真实看到的清单：a 回来了，b 才算上游删了
    assert await db.reconcile_source_items("pack:demo", ["a"]) == 1
    assert db.count_stale_source_items("pack:demo") == 1
    states = {item["external_id"]: item["stale"] for item in db.get_source_items("pack:demo")}
    assert states == {"a": 0, "b": 1}


@pytest.mark.asyncio
async def test_failed_sync_does_not_mark_items_stale(tmp_path, monkeypatch):
    """预检失败 ≠ 上游删图：网络抖一下不能把整个源标成失效。"""

    service, db = _service(tmp_path)
    pack = _pack(tmp_path)
    first = await service.import_now({"source_type": "meme_pack", "path": str(pack)})
    assert first["status"] == "completed"
    assert [item["stale"] for item in db.get_source_items("pack:demo-pack")] == [0]

    def _boom(_path):
        raise ExternalSourceError("network is unreachable")

    monkeypatch.setattr(service, "_pack_reader", _boom)
    failed = await service.import_now({"source_type": "meme_pack", "path": str(pack)})
    assert failed["status"] == "failed"
    assert [item["stale"] for item in db.get_source_items("pack:demo-pack")] == [0]
    assert db.count_total() == 1


def test_retention_class_defaults_to_native_and_exempts_external():
    assert retention_class_of({}) == "native"
    assert retention_class_of(None) == "native"
    assert retention_class_of({"retention_class": " EXTERNAL "}) == "external"
    assert is_capacity_exempt({"retention_class": "external"}) is True
    assert is_capacity_exempt({"retention_class": "pinned"}) is True
    assert is_capacity_exempt({"retention_class": "native"}) is False
    assert is_capacity_exempt({"is_favorite": 1}) is False


def test_count_capacity_managed_counts_favorites_but_not_external():
    index = {
        "a.png": {"retention_class": "native"},
        "b.png": {"is_favorite": 1},
        "c.png": {"retention_class": "external"},
        "d.png": {"retention_class": "pinned"},
    }
    assert count_capacity_managed(index) == 2


def test_capacity_cleanup_skips_external_and_favorite_items():
    """整包导入的图不能被后台容量清理悄悄啃掉。"""

    handler = EventHandler.__new__(EventHandler)
    handler.plugin = SimpleNamespace(plugin_config=SimpleNamespace(max_reg_num=2))
    index = {
        "old-native.png": {"created_at": 1, "retention_class": "native"},
        "new-native.png": {"created_at": 5, "retention_class": "native"},
        "favorite.png": {"created_at": 2, "is_favorite": 1},
        "external.png": {"created_at": 0, "retention_class": "external"},
    }
    # 参与容量的只有 3 条（外部源不计），上限 2 → 只淘汰最旧的那张本机图
    assert handler._select_items_for_removal(index) == [("old-native.png", 1)]

    handler.plugin.plugin_config.max_reg_num = 0
    assert handler._select_items_for_removal(index) == []


def test_capacity_cleanup_never_touches_an_all_external_library():
    handler = EventHandler.__new__(EventHandler)
    handler.plugin = SimpleNamespace(plugin_config=SimpleNamespace(max_reg_num=1))
    index = {
        f"external-{i}.png": {"created_at": i, "retention_class": "external"}
        for i in range(5)
    }
    assert handler._select_items_for_removal(index) == []
