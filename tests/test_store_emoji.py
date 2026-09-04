"""QQ 商城表情段提取 + 表情外显投递的单元测试。

覆盖 LLBot（独立 mface 段 / 只认 subType）与 NapCat、SnowLuma
（商城表情折进 image 段 / 只认 sub_type）两条路径。
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrbot_plugin_meme_magpie.core.events.platform_detector import (
    PlatformDetector,
    _STORE_EMOJI_URL_KEYS,
)


def _event(raw_message, *, platform="aiocqhttp", as_object=False):
    """构造一个只带 raw_message 的假事件。"""
    if raw_message is None:
        raw_event = None
    elif as_object:
        raw_event = types.SimpleNamespace(message=raw_message)
    else:
        raw_event = {"message": raw_message}
    return types.SimpleNamespace(
        message_obj=types.SimpleNamespace(raw_message=raw_event),
        get_platform_name=lambda: platform,
    )


def _detector():
    plugin = types.SimpleNamespace(plugin_config=types.SimpleNamespace())
    return PlatformDetector(plugin)


def _mface(**data):
    return {"type": "mface", "data": data}


class TestIterOnebotSegments:
    def test_dict_raw_message(self):
        segs = [{"type": "image", "data": {}}]
        assert PlatformDetector.iter_onebot_segments(_event(segs)) == segs

    def test_object_raw_message(self):
        segs = [{"type": "image", "data": {}}]
        assert PlatformDetector.iter_onebot_segments(_event(segs, as_object=True)) == segs

    def test_none_event(self):
        assert PlatformDetector.iter_onebot_segments(None) == []

    def test_missing_raw_message(self):
        assert PlatformDetector.iter_onebot_segments(_event(None)) == []

    def test_cq_code_string_message(self):
        # messageFormat="string" 时 message 是一段 CQ 码，不是段列表
        event = _event("[CQ:image,file=abc.jpg]")
        assert PlatformDetector.iter_onebot_segments(event) == []

    def test_non_dict_items_filtered(self):
        segs = [{"type": "image", "data": {}}, "text", 42, None]
        assert PlatformDetector.iter_onebot_segments(_event(segs)) == [segs[0]]

    def test_event_without_message_obj(self):
        assert PlatformDetector.iter_onebot_segments(types.SimpleNamespace()) == []


class TestSegType:
    def test_lowercased_and_stripped(self):
        assert PlatformDetector._seg_type({"type": "  MFace "}) == "mface"

    def test_missing_type(self):
        assert PlatformDetector._seg_type({}) == ""

    def test_none_type(self):
        assert PlatformDetector._seg_type({"type": None}) == ""


class TestExtractStoreEmojiSegments:
    def test_llbot_mface_segment(self):
        event = _event(
            [
                {"type": "text", "data": {"text": "看这个"}},
                _mface(
                    url="https://gxh.vip.qq.com/club/item/parcel/item/aa/bb/raw300.gif",
                    emoji_id="abc123",
                    emoji_package_id="240000",
                    key="deadbeef",
                    summary="[开心]",
                ),
            ]
        )
        items = _detector().extract_store_emoji_segments(event)
        assert len(items) == 1
        item = items[0]
        assert item["url"].endswith("raw300.gif")
        assert item["meta"] == {
            "source": "qq_store",
            "qq_emoji_id": "abc123",
            "qq_emoji_package_id": "240000",
            "qq_key": "deadbeef",
            "store_summary": "[开心]",
            "origin_url": item["url"],
        }

    def test_marketface_type_also_supported(self):
        event = _event([{"type": "marketface", "data": {"url": "https://a.com/x.gif"}}])
        assert _detector().extract_store_emoji_urls(event) == ["https://a.com/x.gif"]

    def test_urls_facade_returns_plain_strings(self):
        event = _event([_mface(url="https://a.com/x.gif"), _mface(url="https://b.com/y.gif")])
        assert _detector().extract_store_emoji_urls(event) == [
            "https://a.com/x.gif",
            "https://b.com/y.gif",
        ]

    def test_duplicate_urls_deduped(self):
        event = _event([_mface(url="https://a.com/x.gif"), _mface(url="https://a.com/x.gif")])
        assert _detector().extract_store_emoji_urls(event) == ["https://a.com/x.gif"]

    def test_url_key_priority(self):
        # url 排在 thumb 前面，应优先取原图
        event = _event(
            [_mface(thumb="https://a.com/thumb.gif", url="https://a.com/raw.gif")]
        )
        assert _detector().extract_store_emoji_urls(event)[0] == "https://a.com/raw.gif"
        assert _STORE_EMOJI_URL_KEYS.index("url") < _STORE_EMOJI_URL_KEYS.index("thumb")

    def test_unknown_field_name_fallback_scan(self):
        # 字段名没在白名单里时兜底扫全部 value
        event = _event([_mface(some_new_cdn="https://a.com/x.gif")])
        assert _detector().extract_store_emoji_urls(event) == ["https://a.com/x.gif"]

    def test_non_http_values_ignored(self):
        event = _event([_mface(emoji_id="123", summary="[开心]", file="local.gif")])
        assert _detector().extract_store_emoji_segments(event) == []

    def test_image_segment_not_treated_as_store(self):
        event = _event([{"type": "image", "data": {"url": "https://a.com/x.gif"}}])
        assert _detector().extract_store_emoji_segments(event) == []

    def test_missing_optional_meta_fields_are_blank(self):
        event = _event([_mface(url="https://a.com/x.gif")])
        meta = _detector().extract_store_emoji_segments(event)[0]["meta"]
        assert meta["qq_emoji_id"] == ""
        assert meta["qq_key"] == ""
        assert meta["source"] == "qq_store"

    def test_id_and_package_id_aliases(self):
        event = _event([_mface(url="https://a.com/x.gif", id="fallback-id", tabId="777")])
        meta = _detector().extract_store_emoji_segments(event)[0]["meta"]
        assert meta["qq_emoji_id"] == "fallback-id"
        assert meta["qq_emoji_package_id"] == "777"

    def test_broken_data_field_skipped(self):
        event = _event([{"type": "mface", "data": "not-a-dict"}])
        assert _detector().extract_store_emoji_segments(event) == []

    def test_no_raw_message_returns_empty(self):
        assert _detector().extract_store_emoji_segments(_event(None)) == []


class TestStoreEmojiFoldedIntoImageSegment:
    """SnowLuma / NapCat 把商城表情折进 image 段，sub_type 可能是 0 或缺失。"""

    def _check(self, data, **kwargs):
        img = types.SimpleNamespace(file=data.get("file", ""), url=data.get("url", ""))
        event = _event([{"type": "image", "data": data}])
        return _detector().check_platform_emoji_metadata(img, event, **kwargs)

    def test_emoji_id_only(self):
        assert self._check({"file": "a.gif", "emoji_id": "123", "sub_type": 0}) is True

    def test_emoji_package_id_only(self):
        assert self._check({"file": "a.gif", "emoji_package_id": "240000"}) is True

    def test_emoji_pkg_id_alias(self):
        assert self._check({"file": "a.gif", "emoji_pkg_id": "240000"}) is True

    def test_key_field(self):
        assert self._check({"file": "a.gif", "key": "deadbeef"}) is True

    def test_plain_image_still_rejected(self):
        assert self._check({"file": "a.gif", "url": "https://a.com/a.gif"}) is False

    def test_snake_case_sub_type_wins(self):
        assert self._check({"file": "a.gif", "sub_type": 1}) is True

    def test_camel_case_sub_type_wins(self):
        assert self._check({"file": "a.gif", "subType": "1"}) is True

    def test_summary_keyword(self):
        assert self._check({"file": "a.gif", "summary": "[动画表情]"}) is True

    def test_empty_summary_not_enough(self):
        assert self._check({"file": "a.gif", "summary": "", "sub_type": 0}) is False

    def test_store_cdn_url(self):
        assert (
            self._check(
                {"file": "a.gif", "url": "https://gxh.vip.qq.com/club/item/parcel/item/a.gif"}
            )
            is True
        )


class TestSendQqImageAsSticker:
    @pytest.fixture()
    def delivery(self, monkeypatch, tmp_path):
        class _AiocqhttpMessageEvent:
            pass

        mod_names = [
            "astrbot.core.platform",
            "astrbot.core.platform.sources",
            "astrbot.core.platform.sources.aiocqhttp",
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        ]
        for name in mod_names:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        sys.modules[mod_names[-1]].AiocqhttpMessageEvent = _AiocqhttpMessageEvent

        from astrbot_plugin_meme_magpie.core.events import emoji_delivery

        monkeypatch.setattr(emoji_delivery, "MessageChain", lambda chain: {"chain": chain})
        monkeypatch.setattr(
            emoji_delivery, "Image", lambda file: types.SimpleNamespace(file=file)
        )
        return emoji_delivery, _AiocqhttpMessageEvent

    def _event(self, cls, payload):
        sent = []

        class _Evt(cls):
            def __init__(self):
                self.message_obj = types.SimpleNamespace(raw_message={"message_id": 1})
                self.bot = types.SimpleNamespace(send=self._send)

            async def _parse_onebot_json(self, chain):
                return payload

            async def _send(self, raw, message):
                sent.append((raw, message))

        return _Evt(), sent

    @pytest.mark.asyncio
    async def test_writes_all_three_keys(self, delivery, tmp_path):
        module, cls = delivery
        path = tmp_path / "a.gif"
        path.write_bytes(b"gif")
        payload = [{"type": "image", "data": {"file": str(path)}}]
        event, sent = self._event(cls, payload)

        ok = await module.send_qq_image_as_sticker(event, str(path), summary="[动画表情]")

        assert ok is True
        data = sent[0][1][0]["data"]
        assert data["summary"] == "[动画表情]"
        assert data["sub_type"] == 1
        assert data["subType"] == 1

    @pytest.mark.asyncio
    async def test_rejects_non_aiocqhttp_event(self, delivery, tmp_path):
        module, _cls = delivery
        path = tmp_path / "a.gif"
        path.write_bytes(b"gif")
        assert await module.send_qq_image_as_sticker(types.SimpleNamespace(), str(path)) is False

    @pytest.mark.asyncio
    async def test_rejects_missing_file(self, delivery, tmp_path):
        module, cls = delivery
        event, _sent = self._event(cls, [{"type": "image", "data": {}}])
        assert await module.send_qq_image_as_sticker(event, str(tmp_path / "nope.gif")) is False
        assert await module.send_qq_image_as_sticker(event, "") is False

    @pytest.mark.asyncio
    async def test_send_failure_returns_false(self, delivery, tmp_path):
        module, cls = delivery
        path = tmp_path / "a.gif"
        path.write_bytes(b"gif")
        event, _sent = self._event(cls, [{"type": "image", "data": {}}])

        async def boom(*_args, **_kwargs):
            raise RuntimeError("offline")

        event.bot = types.SimpleNamespace(send=boom)
        assert await module.send_qq_image_as_sticker(event, str(path)) is False
