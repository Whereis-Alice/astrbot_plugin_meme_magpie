"""POST /analyze 的已知信息透传，以及 pending 更新的 work / overlay_text 白名单。"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrbot_plugin_meme_magpie import plugin_api as plugin_api_module
from astrbot_plugin_meme_magpie.plugin_api import PluginAPI

CHARACTERS = [
    {"key": "gotoh_hitori", "name": "后藤一里", "desc": ""},
    {"key": "kita_ikuyo", "name": "喜多郁代", "desc": ""},
]


class _Proc:
    CATEGORY_FILTERED = "过滤不通过"
    CATEGORY_NOT_EMOJI = "非表情包"

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self.result = result or ("开心", ["笑"], "有人在笑", "开心", ["聊天"], "哈哈", ["喜悦"])

    async def classify_image(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _api(*, proc=None, index=None, pending=None, characters=CHARACTERS, update_log=None):
    cfg = types.SimpleNamespace(
        categories=["开心", "生气"],
        get_characters=lambda: [item["key"] for item in characters],
        get_character_info_list=lambda: [dict(item) for item in characters],
        get_category_info=lambda: [],
    )

    async def update_pending(pending_id, fields):
        if update_log is not None:
            update_log.append((pending_id, dict(fields)))
        row = dict((pending or {}).get(pending_id) or {})
        if not row:
            return None
        row.update(fields)
        row.setdefault("id", pending_id)
        return row

    db = types.SimpleNamespace(
        get_index_cache_readonly=lambda: dict(index or {}),
        get_pending=lambda pid: dict((pending or {}).get(pid) or {}) or None,
        update_pending=update_pending,
    )
    plugin = types.SimpleNamespace(
        plugin_config=cfg,
        db_service=db,
        image_processor_service=proc,
        base_dir=Path("."),
        cache_service=None,
    )
    return PluginAPI(plugin)


@pytest.fixture()
def api_request(monkeypatch):
    """把 quart 的 request/jsonify 换成可控桩：jsonify 直接回传 dict。"""
    monkeypatch.setattr(plugin_api_module, "jsonify", lambda payload: payload)

    def _set(payload):
        async def get_json():
            return payload

        monkeypatch.setattr(
            plugin_api_module,
            "request",
            types.SimpleNamespace(get_json=get_json, method="POST"),
        )

    return _set


class TestKnownFactsHelpers:
    def test_character_key_maps_to_display_name(self):
        assert _api()._character_display("gotoh_hitori") == "后藤一里"

    def test_unknown_character_key_kept_as_is(self):
        assert _api()._character_display("someone_else") == "someone_else"

    def test_blank_character_key(self):
        assert _api()._character_display("") == ""
        assert _api()._character_display(None) == ""

    def test_known_facts_uses_display_name(self):
        assert _api()._known_facts(work="孤独摇滚", character="gotoh_hitori") == {
            "work": "孤独摇滚",
            "character": "后藤一里",
        }

    def test_known_facts_drops_empty(self):
        assert _api()._known_facts(work="", character=None) == {}
        assert _api()._known_facts(work="  ") == {}

    def test_known_facts_normalizes_work(self):
        assert _api()._known_facts(work="  孤独   摇滚 ")["work"] == "孤独 摇滚"

    def test_known_facts_accepts_display_name_as_character(self):
        assert _api()._known_facts(character="后藤一里")["character"] == "后藤一里"


class TestHandleAnalyzeImage:
    @pytest.mark.asyncio
    async def test_missing_processor_returns_json_error(self, api_request):
        api_request({"hash": "abc"})
        result = await _api(proc=None).handle_analyze_image()
        assert result == {"success": False, "error": "图片处理服务不可用"}

    @pytest.mark.asyncio
    async def test_missing_locator_returns_json_error(self, api_request):
        api_request({})
        result = await _api(proc=_Proc()).handle_analyze_image()
        assert result["success"] is False
        assert "hash" in result["error"]

    @pytest.mark.asyncio
    async def test_library_hash_reuses_stored_facts(self, api_request, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        proc = _Proc()
        api = _api(
            proc=proc,
            index={
                str(img): {
                    "hash": "h1",
                    "work": "孤独摇滚",
                    "character": "gotoh_hitori",
                }
            },
        )
        api_request({"hash": "h1"})

        result = await api.handle_analyze_image()

        assert result["success"] is True
        assert result["known_facts"] == {"work": "孤独摇滚", "character": "后藤一里"}
        assert proc.calls[0]["known"] == {"work": "孤独摇滚", "character": "后藤一里"}
        assert proc.calls[0]["file_path"] == str(img)

    @pytest.mark.asyncio
    async def test_pending_id_locates_file(self, api_request, tmp_path):
        img = tmp_path / "p.png"
        img.write_bytes(b"png")
        proc = _Proc()
        api = _api(
            proc=proc,
            pending={7: {"id": 7, "path": str(img), "work": "轻音少女"}},
        )
        api_request({"pending_id": 7})

        result = await api.handle_analyze_image()

        assert result["success"] is True
        assert result["known_facts"] == {"work": "轻音少女"}
        assert proc.calls[0]["file_path"] == str(img)

    @pytest.mark.asyncio
    async def test_explicit_params_override_stored_facts(self, api_request, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        proc = _Proc()
        api = _api(
            proc=proc,
            index={str(img): {"hash": "h1", "work": "旧作品", "character": "kita_ikuyo"}},
        )
        api_request({"hash": "h1", "work": "孤独摇滚", "character": "gotoh_hitori"})

        result = await api.handle_analyze_image()

        assert result["known_facts"] == {"work": "孤独摇滚", "character": "后藤一里"}
        assert proc.calls[0]["known"]["character"] == "后藤一里"

    @pytest.mark.asyncio
    async def test_explicit_blank_clears_stored_facts(self, api_request, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        proc = _Proc()
        api = _api(proc=proc, index={str(img): {"hash": "h1", "work": "旧作品"}})
        api_request({"hash": "h1", "work": ""})

        result = await api.handle_analyze_image()

        assert result["known_facts"] == {}
        assert proc.calls[0]["known"] is None

    @pytest.mark.asyncio
    async def test_no_known_facts_passes_none(self, api_request, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        proc = _Proc()
        api = _api(proc=proc, index={str(img): {"hash": "h1"}})
        api_request({"hash": "h1"})

        result = await api.handle_analyze_image()

        assert result["known_facts"] == {}
        assert proc.calls[0]["known"] is None

    @pytest.mark.asyncio
    async def test_filtered_category_reported(self, api_request, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        proc = _Proc(result=("过滤不通过", [], "", "", [], "", []))
        api = _api(proc=proc, index={str(img): {"hash": "h1"}})
        api_request({"hash": "h1"})

        result = await api.handle_analyze_image()
        assert result == {"success": False, "error": "图片内容审核不通过"}

    @pytest.mark.asyncio
    async def test_full_payload_shape(self, api_request, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"png")
        api = _api(proc=_Proc(), index={str(img): {"hash": "h1"}})
        api_request({"hash": "h1"})

        result = await api.handle_analyze_image()

        assert result == {
            "success": True,
            "category": "开心",
            "tags": ["笑"],
            "description": "有人在笑",
            "scenes": ["聊天"],
            "overlay_text": "哈哈",
            "emotions": ["喜悦"],
            "known_facts": {},
        }

    @pytest.mark.asyncio
    async def test_base64_upload_path_cleans_temp_file(self, api_request):
        import base64
        import os

        proc = _Proc()
        api = _api(proc=proc)
        payload = "data:image/png;base64," + base64.b64encode(b"png-bytes").decode()
        api_request({"base64": payload, "work": "孤独摇滚"})

        result = await api.handle_analyze_image()

        assert result["success"] is True
        assert result["known_facts"] == {"work": "孤独摇滚"}
        tmp_path = proc.calls[0]["file_path"]
        assert tmp_path.endswith(".png")
        assert not os.path.exists(tmp_path)


class TestPendingUpdateWhitelist:
    @pytest.mark.asyncio
    async def test_work_and_overlay_text_persisted(self, api_request):
        log: list = []
        api = _api(pending={3: {"id": 3, "path": "a.png"}}, update_log=log)
        api_request(
            {"id": 3, "work": "  孤独   摇滚 ", "overlay_text": "  我不行了  "}
        )

        result = await api.handle_pending_update()

        assert result["success"] is True
        assert log == [(3, {"work": "孤独 摇滚", "overlay_text": "我不行了"})]
        assert result["item"]["work"] == "孤独 摇滚"
        assert result["item"]["overlay_text"] == "我不行了"

    @pytest.mark.asyncio
    async def test_work_can_be_cleared(self, api_request):
        log: list = []
        api = _api(pending={3: {"id": 3, "path": "a.png", "work": "旧作品"}}, update_log=log)
        api_request({"id": 3, "work": ""})

        result = await api.handle_pending_update()

        assert result["success"] is True
        assert log == [(3, {"work": ""})]

    @pytest.mark.asyncio
    async def test_character_validated_against_config(self, api_request):
        api = _api(pending={3: {"id": 3, "path": "a.png"}})
        api_request({"id": 3, "character": "不存在的角色"})

        result = await api.handle_pending_update()
        assert result["success"] is False
        assert "角色无效" in result["error"]

    @pytest.mark.asyncio
    async def test_untouched_fields_not_written(self, api_request):
        log: list = []
        api = _api(pending={3: {"id": 3, "path": "a.png"}}, update_log=log)
        api_request({"id": 3, "desc": "描述"})

        await api.handle_pending_update()
        assert log == [(3, {"desc": "描述"})]

    @pytest.mark.asyncio
    async def test_no_updatable_field_rejected(self, api_request):
        api = _api(pending={3: {"id": 3, "path": "a.png"}})
        api_request({"id": 3, "unknown_field": "x"})

        result = await api.handle_pending_update()
        assert result == {"success": False, "error": "没有可更新字段"}
