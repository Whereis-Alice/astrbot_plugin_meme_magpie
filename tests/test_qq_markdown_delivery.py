"""QQ-native material upload, controlled export and Markdown attachment tests."""

import asyncio
import base64
import hashlib
import importlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image as PILImage

from astrbot.api.message_components import Image, Plain
from core.db.database_service import DatabaseService
from core.integration.qq_markdown_delivery import (
    EXTRA_KEY,
    QQMarkdownDeliveryService,
    normalize_markdown_image_url,
)


class _Event:
    def __init__(self, *, platform="qq_official", bot_id="bot-1", target="group:100"):
        self.platform = platform
        self.bot_id = bot_id
        self.target = target
        self.unified_msg_origin = f"{platform}:{target}"
        self.extra = {}

    def get_platform_name(self):
        return self.platform

    def get_self_id(self):
        return self.bot_id

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


def _png_bytes(size=(640, 320)):
    data = io.BytesIO()
    PILImage.new("RGB", size, (50, 90, 170)).save(data, format="PNG")
    return data.getvalue()


def _gif_bytes():
    data = io.BytesIO()
    first = PILImage.new("RGB", (24, 16), "red")
    first.save(data, format="GIF", save_all=True, append_images=[PILImage.new("RGB", (24, 16), "blue")], duration=100, loop=0)
    return data.getvalue()


def _setup(tmp_path, *, data=None, extension=".png", ferry=None, callback=""):
    base = tmp_path / "plugin_data"
    path = base / "categories" / "happy" / f"original{extension}"
    path.parent.mkdir(parents=True)
    path.write_bytes(data or _png_bytes())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    db = DatabaseService(base / "cache" / "emoji.db")
    entry = {"path": str(path), "hash": digest, "category": "happy", "scope_mode": "public"}
    asyncio.run(db.insert_batch([entry]))
    state = SimpleNamespace(candidates=[{"id": "keep-tool-candidate"}])
    state.get_candidates = lambda: state.candidates
    config = SimpleNamespace(
        qqofficial_markdown_images=True, send_meme_as_gif=False,
        get_event_target=lambda event: event.target.split(":", 1),
    )
    plugin = SimpleNamespace(
        base_dir=base, db_service=db, plugin_config=config,
        _emoji_turn_state=lambda _event: state,
        is_send_enabled_for_event=lambda _event: True,
        meme_selector=SimpleNamespace(is_path_allowed_for_event=lambda _path, _event: True),
        context=SimpleNamespace(
            get_registered_star=lambda _name: SimpleNamespace(star_cls=ferry) if ferry else None,
            get_config=lambda _umo=None: {"callback_api_base": callback},
        ),
    )
    service = QQMarkdownDeliveryService(plugin)
    service._native_request = AsyncMock(side_effect=RuntimeError("native unavailable"))
    return plugin, service, path, entry, state


def _native(service, data, url="qqbot-media://image/v1/opaque_token"):
    service._native_request = AsyncMock(side_effect=[
        {"upload_id": "upload-1", "presigned_url": "https://assets.example/upload?signature=value"},
        {"image_id": "image-1", "url": url, "ttl": 7776000},
    ])
    service._put_native = AsyncMock(return_value=hashlib.md5(data).hexdigest())


def _image(path):
    image = Image()
    image.path = str(path)
    image.file = "base64://image"
    return image


@pytest.mark.parametrize("platform", ["qq_official", "qq_official_webhook"])
def test_native_upload_uses_verified_bytes_and_appends_markdown_after_text(tmp_path, platform):
    plugin, service, path, _entry, state = _setup(tmp_path)
    original = path.read_bytes()
    _native(service, original)
    event = _Event(platform=platform)
    attached = plugin.base_dir / "compat_plugin_stealer" / path.name

    async def run():
        assert await service.prepare_attachment(event, str(path), str(attached))
        assert state.candidates == [{"id": "keep-tool-candidate"}]
        record = event.get_extra(EXTRA_KEY)["images"][0]
        assert record == {"path": str(attached), "url": "qqbot-media://image/v1/opaque_token", "width": 320, "height": 160}
        first = service._native_request.await_args_list[0]
        assert first.args[1:3] == ("POST", "/v2/media-images/prepare")
        assert first.args[3]["file_size"] == len(original)
        assert first.args[3]["md5"] == hashlib.md5(original).hexdigest()
        assert first.args[3]["sha1"] == hashlib.sha1(original).hexdigest()
        service._put_native.assert_awaited_once_with("https://assets.example/upload?signature=value", original, "image/png")
        second = service._native_request.await_args_list[1]
        assert second.args[1:3] == ("POST", "/v2/media-images/complete")
        assert second.args[3] == {"upload_id": "upload-1", "md5": hashlib.md5(original).hexdigest()}
        result = SimpleNamespace(chain=[Plain("文字在上"), _image(attached)])
        assert service.convert_result(event, result)
        assert result.chain[0].text == "文字在上"
        assert result.chain[1].text == "\n\n![表情 #320px #160px](qqbot-media://image/v1/opaque_token)\n"
        assert result.use_markdown_ is True and result.use_t2i_ is False
        assert not service.convert_result(event, result)
        await service.aclose()

    asyncio.run(run())
    assert path.read_bytes() == original


def test_gif_encoding_matches_native_and_ferry_payload_without_compression(tmp_path):
    gif = _gif_bytes()
    uploaded = []

    async def upload(_event, asset, **options):
        uploaded.append((asset, await asset.read_bytes(), options))
        return {"success": True, "url": "https://images.example/animated.gif"}

    ferry = SimpleNamespace(upload_asset=AsyncMock(side_effect=upload))
    plugin, service, path, _entry, _state = _setup(tmp_path, ferry=ferry)
    original = path.read_bytes()
    plugin.plugin_config.send_meme_as_gif = True
    plugin.image_processor_service = SimpleNamespace(_file_to_gif_base64=AsyncMock(return_value=base64.b64encode(gif).decode()))

    assert asyncio.run(service.prepare_attachment(_Event(), str(path), str(path)))
    asset, data, options = uploaded[0]
    assert data == gif
    assert asset.mime == "image/gif" and asset.filename.endswith(".gif")
    assert asset.sha256 == hashlib.sha256(gif).hexdigest()
    assert options == {"folder": "qqbot/memes", "compress": False, "output_format": "markdown"}
    with PILImage.open(io.BytesIO(data)) as image:
        assert image.n_frames == 2
    assert path.read_bytes() == original
    with pytest.raises(Exception, match="released"):
        asyncio.run(asset.read_bytes())


def test_native_gif_upload_preserves_animation_bytes(tmp_path):
    gif = _gif_bytes()
    _plugin, service, path, _entry, _state = _setup(tmp_path, data=gif, extension=".gif")
    _native(service, gif)
    assert asyncio.run(service.prepare_attachment(_Event(), str(path), str(path)))
    assert service._put_native.await_args.args[1:] == (gif, "image/gif")
    assert path.read_bytes() == gif


def test_disabled_other_platform_and_missing_event_do_not_upload(tmp_path):
    _plugin, service, path, _entry, _state = _setup(tmp_path)
    assert not asyncio.run(service.prepare_attachment(None, str(path), str(path)))
    assert not asyncio.run(service.prepare_attachment(_Event(platform="aiocqhttp"), str(path), str(path)))
    service.plugin.plugin_config.qqofficial_markdown_images = False
    assert not asyncio.run(service.prepare_attachment(_Event(), str(path), str(path)))
    service._native_request.assert_not_awaited()


def test_ferry_failure_keeps_original_image_and_text(tmp_path):
    ferry = SimpleNamespace(upload_asset=AsyncMock(return_value={"success": False, "code": "quota_exceeded"}))
    _plugin, service, path, _entry, _state = _setup(tmp_path, ferry=ferry)
    event = _Event()
    result = SimpleNamespace(chain=[Plain("保持正文"), _image(path)])
    assert not asyncio.run(service.prepare_attachment(event, str(path), str(path)))
    assert event.get_extra(EXTRA_KEY) is None
    assert not service.convert_result(event, result)
    assert isinstance(result.chain[1], Image)
    assert path.exists()


def test_callback_tokens_allow_repeated_reads_and_preserve_gif_bytes(tmp_path, monkeypatch):
    core_module = importlib.import_module("astrbot.core")

    records = {}

    async def register(path, *, timeout, single_use):
        records["token-1"] = (Path(path), timeout, single_use)
        return "token-1"

    monkeypatch.setattr(core_module, "file_token_service", SimpleNamespace(register_file=register), raising=False)
    plugin, service, path, _entry, _state = _setup(tmp_path, callback="https://bot.example")
    gif = _gif_bytes()
    plugin.plugin_config.send_meme_as_gif = True
    plugin.image_processor_service = SimpleNamespace(_file_to_gif_base64=AsyncMock(return_value=base64.b64encode(gif).decode()))
    event = _Event()
    assert asyncio.run(service.prepare_attachment(event, str(path), str(path)))
    assert event.get_extra(EXTRA_KEY)["images"][0]["url"] == "https://bot.example/api/file/token-1"
    generated, timeout, single_use = records["token-1"]
    assert generated.read_bytes() == gif
    assert generated.read_bytes() == gif
    assert single_use is False and timeout == 86400
    assert path.exists()


def test_same_hash_concurrency_reuses_upload_and_different_bots_do_not(tmp_path):
    _plugin, service, path, _entry, _state = _setup(tmp_path)
    data = path.read_bytes()
    _native(service, data)

    async def run():
        results = await asyncio.gather(*[
            service.prepare_attachment(_Event(), str(path), str(path)) for _ in range(3)
        ])
        assert results == [True, True, True]
        assert service._native_request.await_count == 2
        assert service._locks == {}
        _native(service, data, url="qqbot-media://image/v1/bot_2_material")
        event = _Event(bot_id="bot-2")
        assert await service.prepare_attachment(event, str(path), str(path))
        assert event.get_extra(EXTRA_KEY)["images"][0]["url"].endswith("bot_2_material")

    asyncio.run(run())


def test_cached_url_does_not_bypass_current_scope_or_source_integrity(tmp_path):
    plugin, service, path, entry, _state = _setup(tmp_path)
    _native(service, path.read_bytes())

    async def run():
        assert await service.prepare_attachment(_Event(), str(path), str(path))
        entry.update(scope_mode="local", origin_target="group:100")
        await plugin.db_service.insert_batch([entry])
        assert not await service.prepare_attachment(_Event(target="group:200"), str(path), str(path))
        assert service._native_request.await_count == 2
        path.write_bytes(_png_bytes((20, 10)))
        assert not await service.prepare_attachment(_Event(), str(path), str(path))

    asyncio.run(run())


def test_scope_change_during_upload_prevents_publishing_attachment(tmp_path):
    plugin, service, path, entry, _state = _setup(tmp_path)
    _native(service, path.read_bytes())

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def put(*_args):
            started.set()
            await release.wait()
            return hashlib.md5(path.read_bytes()).hexdigest()

        service._put_native = put
        event = _Event()
        task = asyncio.create_task(service.prepare_attachment(event, str(path), str(path)))
        await asyncio.wait_for(started.wait(), timeout=2)
        entry.update(scope_mode="local", origin_target="group:200")
        await plugin.db_service.insert_batch([entry])
        release.set()
        assert not await task
        assert event.get_extra(EXTRA_KEY) is None

    asyncio.run(run())


def test_native_cache_survives_reload_without_reuploading(tmp_path):
    plugin, service, path, _entry, _state = _setup(tmp_path)
    storage = {}

    async def put(key, value):
        storage[key] = value

    plugin.put_kv_data = put
    plugin.get_kv_data = AsyncMock(side_effect=lambda key, default: storage.get(key, default))
    _native(service, path.read_bytes())

    async def run():
        assert await service.prepare_attachment(_Event(), str(path), str(path))
        await service.aclose()
        restored = QQMarkdownDeliveryService(plugin)
        restored._native_request = AsyncMock()
        assert await restored.prepare_attachment(_Event(), str(path), str(path))
        restored._native_request.assert_not_awaited()

    asyncio.run(run())


def test_close_cancels_upload_and_releases_all_owned_work(tmp_path):
    _plugin, service, path, _entry, _state = _setup(tmp_path)
    _native(service, path.read_bytes())

    async def run():
        started = asyncio.Event()

        async def put(*_args):
            started.set()
            await asyncio.Event().wait()

        service._put_native = put
        task = asyncio.create_task(service.prepare_attachment(_Event(), str(path), str(path)))
        await asyncio.wait_for(started.wait(), timeout=2)
        await service.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service._cache == {} and service._locks == {}
        assert service._uploads == set() and service._preparing == set()
        assert not await service.prepare_attachment(_Event(), str(path), str(path))

    asyncio.run(run())
    assert path.exists()


@pytest.mark.parametrize("url", ["file:///secret", "https://user:pass@example.com/a.png", "https://example.com/a%0A.png", "qqbot-media://other/v1/token", "qqbot-media://image/v1/token?x=1", "https://example.com:invalid/a.png", "qqbot-media://image/v1/token/extra", "qqbot-media://image/v1/token)extra"])
def test_invalid_urls_are_rejected(url):
    assert normalize_markdown_image_url(url) == ""


def test_markdown_urls_escape_parentheses_spaces_and_literal_percent():
    assert normalize_markdown_image_url("https://images.example/a (1)%.png?q=%20") == "https://images.example/a%20%281%29%25.png?q=%20"


def test_url_paths_escape_quotes_and_brackets_without_breaking_ipv6_authority():
    assert normalize_markdown_image_url("https://img.example/a'b[1].png?q='x'") == (
        "https://img.example/a%27b%5B1%5D.png?q=%27x%27"
    )
    assert normalize_markdown_image_url("https://[2001:db8::1]:443/a[1].png") == (
        "https://[2001:db8::1]:443/a%5B1%5D.png"
    )
