"""批量重新识别与「识别失败检测」的单元测试（不触发真实 VLM 与磁盘写入）。"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrbot_plugin_meme_magpie import plugin_api as plugin_api_module
from astrbot_plugin_meme_magpie.plugin_api import PluginAPI


def _api(index=None, config=None):
    db = types.SimpleNamespace(get_index_cache_readonly=lambda: dict(index or {}))
    plugin = types.SimpleNamespace(
        db_service=db,
        plugin_config=config or types.SimpleNamespace(),
        base_dir=Path("."),
        cache_service=None,
    )
    return PluginAPI(plugin)


class _Proc:
    CATEGORY_FILTERED = "过滤不通过"
    CATEGORY_NOT_EMOJI = "非表情包"


# ── _needs_reanalysis ────────────────────────────────────


class TestNeedsReanalysis:
    def test_missing_tags(self):
        assert PluginAPI._needs_reanalysis({"tags": [], "desc": "有描述"}) is True

    def test_missing_desc(self):
        assert PluginAPI._needs_reanalysis({"tags": ["开心"], "desc": ""}) is True

    def test_blank_desc_counts_as_missing(self):
        assert PluginAPI._needs_reanalysis({"tags": ["开心"], "desc": "   "}) is True

    def test_complete_entry(self):
        assert PluginAPI._needs_reanalysis({"tags": ["开心"], "desc": "笑"}) is False

    def test_empty_meta(self):
        assert PluginAPI._needs_reanalysis({}) is True


# ── _normalize_analysis_result ───────────────────────────


class TestNormalizeAnalysisResult:
    def test_full_tuple(self):
        classified = ("开心", ["笑", "猫"], "一只笑猫", "开心", ["聊天"], "哈哈", ["喜悦"])
        assert PluginAPI._normalize_analysis_result(classified, _Proc()) == {
            "category": "开心",
            "tags": ["笑", "猫"],
            "desc": "一只笑猫",
            "scenes": ["聊天"],
            "overlay_text": "哈哈",
            "emotions": ["喜悦"],
        }

    def test_none_fields_are_normalised(self):
        classified = ("开心", None, None, None, None, None, None)
        assert PluginAPI._normalize_analysis_result(classified, _Proc()) == {
            "category": "开心",
            "tags": [],
            "desc": "",
            "scenes": [],
            "overlay_text": "",
            "emotions": [],
        }

    def test_empty_category_drops_everything(self):
        classified = ("", ["标签"], "描述", "", ["场景"], "文字", ["情绪"])
        assert PluginAPI._normalize_analysis_result(classified, _Proc()) is None

    def test_rejected_category_drops_everything(self):
        # 这是上游的 bug：分类被判为「非表情包」时仍会保留同一次分析的标签。
        for cat in ("过滤不通过", "非表情包"):
            classified = (cat, ["标签"], "描述", "", ["场景"], "文字", ["情绪"])
            assert PluginAPI._normalize_analysis_result(classified, _Proc()) is None

    def test_bad_shape_returns_none(self):
        assert PluginAPI._normalize_analysis_result(("只有一个",), _Proc()) is None
        assert PluginAPI._normalize_analysis_result(None, _Proc()) is None

    def test_missing_proc_constants_use_fallbacks(self):
        classified = ("非表情包", [], "", "", [], "", [])
        assert PluginAPI._normalize_analysis_result(classified, object()) is None


# ── _merge_reanalysis ────────────────────────────────────


class TestMergeReanalysis:
    ANALYSIS = {
        "tags": ["笑", "猫"],
        "desc": "一只笑猫",
        "scenes": ["聊天"],
        "overlay_text": "哈哈",
        "emotions": ["喜悦"],
    }

    def test_fill_only_empty_fields_by_default(self):
        meta = {"tags": ["原有"], "desc": "", "scenes": [], "overlay_text": "旧文字"}
        updates = PluginAPI._merge_reanalysis(meta, self.ANALYSIS, overwrite=False)
        assert updates == {"desc": "一只笑猫", "scenes": ["聊天"], "emotions": ["喜悦"]}

    def test_overwrite_replaces_existing(self):
        meta = {"tags": ["原有"], "desc": "旧描述", "overlay_text": "哈哈"}
        updates = PluginAPI._merge_reanalysis(meta, self.ANALYSIS, overwrite=True)
        assert updates["tags"] == ["笑", "猫"]
        assert updates["desc"] == "一只笑猫"
        # overlay_text 值没变，不该出现在 updates 里。
        assert "overlay_text" not in updates

    def test_empty_analysis_produces_no_updates(self):
        meta = {"tags": [], "desc": ""}
        blank = {"tags": [], "desc": "  ", "scenes": None, "overlay_text": "", "emotions": []}
        assert PluginAPI._merge_reanalysis(meta, blank, overwrite=True) == {}

    def test_category_is_never_written(self):
        analysis = dict(self.ANALYSIS, category="其他")
        updates = PluginAPI._merge_reanalysis({}, analysis, overwrite=True)
        assert "category" not in updates

    def test_list_values_are_stripped(self):
        analysis = {"tags": [" 笑 ", "", None, "猫"]}
        assert PluginAPI._merge_reanalysis({}, analysis, overwrite=True) == {
            "tags": ["笑", "猫"]
        }

    def test_identical_lists_are_skipped(self):
        meta = {"tags": ["笑", "猫"]}
        analysis = {"tags": ["笑", "猫"]}
        assert PluginAPI._merge_reanalysis(meta, analysis, overwrite=True) == {}


# ── _collect_reanalyze_targets ───────────────────────────


def _index():
    return {
        "/d/a.png": {"hash": "h1", "tags": [], "desc": "", "created_at": "2026-01-01"},
        "/d/b.png": {"hash": "h2", "tags": ["笑"], "desc": "有", "created_at": "2026-01-02"},
        "/d/c.png": {"hash": "h3", "tags": ["哭"], "desc": "", "created_at": "2025-12-31",
                     "original_name": "sad.png"},
    }


class TestCollectReanalyzeTargets:
    def test_empty_index(self):
        items, error = _api({})._collect_reanalyze_targets(target="all", hashes=[], limit=None)
        assert items == []
        assert "索引为空" in error

    def test_all_sorted_by_created_at(self):
        items, error = _api(_index())._collect_reanalyze_targets(
            target="all", hashes=[], limit=None
        )
        assert error == ""
        assert [i["path"] for i in items] == ["/d/c.png", "/d/a.png", "/d/b.png"]

    def test_missing_only_picks_incomplete(self):
        items, error = _api(_index())._collect_reanalyze_targets(
            target="missing", hashes=[], limit=None
        )
        assert error == ""
        assert {i["path"] for i in items} == {"/d/a.png", "/d/c.png"}

    def test_selected_uses_hashes(self):
        items, error = _api(_index())._collect_reanalyze_targets(
            target="selected", hashes=["h2", " "], limit=None
        )
        assert error == ""
        assert [i["path"] for i in items] == ["/d/b.png"]

    def test_selected_without_hashes_errors(self):
        items, error = _api(_index())._collect_reanalyze_targets(
            target="selected", hashes=[], limit=None
        )
        assert items == []
        assert "没有选择" in error

    def test_limit_is_applied(self):
        items, _ = _api(_index())._collect_reanalyze_targets(
            target="all", hashes=[], limit=2
        )
        assert len(items) == 2

    def test_no_match_returns_error(self):
        items, error = _api(_index())._collect_reanalyze_targets(
            target="selected", hashes=["nope"], limit=None
        )
        assert items == []
        assert "没有符合条件" in error

    def test_filename_prefers_original_name(self):
        items, _ = _api(_index())._collect_reanalyze_targets(
            target="selected", hashes=["h3"], limit=None
        )
        assert items[0]["filename"] == "sad.png"

    def test_filename_falls_back_to_basename(self):
        items, _ = _api(_index())._collect_reanalyze_targets(
            target="selected", hashes=["h1"], limit=None
        )
        assert items[0]["filename"] == "a.png"

    def test_non_dict_meta_is_skipped(self):
        items, _ = _api({"/d/x.png": "broken", "/d/a.png": {"hash": "h1"}})\
            ._collect_reanalyze_targets(target="all", hashes=[], limit=None)
        assert [i["path"] for i in items] == ["/d/a.png"]


# ── 调速参数解析 ─────────────────────────────────────────


class TestOptionalInt:
    def test_valid_values(self):
        assert PluginAPI._optional_int(3) == 3
        assert PluginAPI._optional_int("4") == 4
        assert PluginAPI._optional_int("4.7") == 4
        assert PluginAPI._optional_int(0) == 0

    def test_unspecified_values(self):
        for raw in (None, "", "   ", "abc", [], {}):
            assert PluginAPI._optional_int(raw) is None


class TestBatchThrottleDefaults:
    def test_reads_config(self):
        cfg = types.SimpleNamespace(
            batch_analyze_concurrency=4,
            batch_analyze_rpm=90,
            batch_analyze_max_retries=5,
        )
        got = _api({}, cfg)._batch_throttle_defaults()
        assert got["concurrency"] == 4
        assert got["rpm"] == 90
        assert got["max_retries"] == 5
        assert got["max_concurrency"] == 16

    def test_clamps_and_defaults(self):
        cfg = types.SimpleNamespace(
            batch_analyze_concurrency=999,
            batch_analyze_rpm=-10,
            batch_analyze_max_retries=-3,
        )
        got = _api({}, cfg)._batch_throttle_defaults()
        assert got["concurrency"] == 16
        assert got["rpm"] == 0
        assert got["max_retries"] == 0

    def test_missing_config_uses_conservative_values(self):
        got = _api({}, types.SimpleNamespace())._batch_throttle_defaults()
        assert (got["concurrency"], got["rpm"], got["max_retries"]) == (2, 20, 3)


# ── 关键词过滤与 ETA ────────────────────────────────────


class TestItemMatchesSearch:
    ITEM = {
        "desc": "一只笑猫",
        "category": "开心",
        "hash": "abc123",
        "character": "初音未来",
        "work": "vocaloid",
        "overlay_text": "哈哈",
        "original_name": "cat.png",
        "source": "group",
        "origin_target": "12345",
        "tags": ["笑", "猫"],
        "scenes": ["聊天"],
    }

    def test_empty_needle_matches_all(self):
        assert PluginAPI._item_matches_search({}, "") is True

    def test_scalar_fields(self):
        for needle in ("笑猫", "开心", "abc", "初音", "vocaloid", "哈哈", "cat", "group", "12345"):
            assert PluginAPI._item_matches_search(self.ITEM, needle) is True

    def test_list_fields(self):
        assert PluginAPI._item_matches_search(self.ITEM, "猫") is True
        assert PluginAPI._item_matches_search(self.ITEM, "聊天") is True

    def test_no_match(self):
        assert PluginAPI._item_matches_search(self.ITEM, "不存在的词") is False

    def test_non_list_container_field(self):
        assert PluginAPI._item_matches_search({"tags": "单个标签"}, "单个") is True


class TestBatchEta:
    def test_none_before_first_result(self):
        assert PluginAPI._batch_eta_seconds({"processed": 0, "total": 10, "started_at": 1.0}) is None

    def test_none_when_finished(self):
        task = {"processed": 10, "total": 10, "started_at": 1.0, "updated_at": 21.0}
        assert PluginAPI._batch_eta_seconds(task) is None

    def test_linear_estimate(self):
        task = {"processed": 5, "total": 20, "started_at": 100.0, "updated_at": 110.0}
        assert PluginAPI._batch_eta_seconds(task) == 30.0

    def test_none_without_started_at(self):
        assert PluginAPI._batch_eta_seconds({"processed": 1, "total": 5}) is None


# ── _collect_pending_reanalyze_targets ───────────────────


def _pending_rows():
    return [
        {"id": 1, "path": "/p/a.png", "tags": [], "desc": "", "created_at": 300},
        {
            "id": 2,
            "path": "/p/b.png",
            "tags": ["笑"],
            "desc": "有描述",
            "created_at": 100,
            "original_name": "b-original.png",
        },
        {"id": 3, "path": "/p/c.png", "tags": ["哭"], "desc": "", "created_at": 200},
    ]


def _pending_api(rows=None):
    """造一个带待审核池的 PluginAPI，并记录 get_pending_paginated 的调用参数。"""
    calls: list[dict] = []

    def get_pending_paginated(page=1, page_size=20, **kwargs):
        calls.append({"page": page, "page_size": page_size})
        data = _pending_rows() if rows is None else rows
        return list(data), len(data), {}

    db = types.SimpleNamespace(
        get_index_cache_readonly=lambda: {},
        get_pending_paginated=get_pending_paginated,
    )
    plugin = types.SimpleNamespace(
        db_service=db,
        plugin_config=types.SimpleNamespace(),
        base_dir=Path("."),
        cache_service=None,
    )
    return PluginAPI(plugin), calls


class TestCollectPendingReanalyzeTargets:
    def test_db_without_pending_support_reports_error(self):
        items, error = _api({})._collect_pending_reanalyze_targets(
            target="all", ids=[], limit=None
        )
        assert items == []
        assert error == "db 不可用"

    def test_missing_db_reports_error(self):
        plugin = types.SimpleNamespace(db_service=None, plugin_config=types.SimpleNamespace())
        items, error = PluginAPI(plugin)._collect_pending_reanalyze_targets(
            target="all", ids=[], limit=None
        )
        assert items == []
        assert error == "db 不可用"

    def test_all_sorted_by_created_at(self):
        api, _ = _pending_api()
        items, error = api._collect_pending_reanalyze_targets(
            target="all", ids=[], limit=None
        )
        assert error == ""
        assert [i["pending_id"] for i in items] == [2, 3, 1]

    def test_missing_only_picks_incomplete(self):
        api, _ = _pending_api()
        items, error = api._collect_pending_reanalyze_targets(
            target="missing", ids=[], limit=None
        )
        assert error == ""
        assert {i["pending_id"] for i in items} == {1, 3}

    def test_selected_uses_ids(self):
        api, _ = _pending_api()
        items, error = api._collect_pending_reanalyze_targets(
            target="selected", ids=["2", 0, None, "  "], limit=None
        )
        assert error == ""
        assert [i["pending_id"] for i in items] == [2]

    def test_selected_without_ids_errors(self):
        api, _ = _pending_api()
        items, error = api._collect_pending_reanalyze_targets(
            target="selected", ids=[], limit=None
        )
        assert items == []
        assert error == "没有选择任何待审核图片"

    def test_no_match_returns_error(self):
        api, _ = _pending_api()
        items, error = api._collect_pending_reanalyze_targets(
            target="selected", ids=[999], limit=None
        )
        assert items == []
        assert error == "没有符合条件的待审核图片"

    def test_empty_pool_returns_error(self):
        api, _ = _pending_api(rows=[])
        items, error = api._collect_pending_reanalyze_targets(
            target="all", ids=[], limit=None
        )
        assert items == []
        assert error == "没有符合条件的待审核图片"

    def test_limit_is_applied(self):
        api, _ = _pending_api()
        items, _error = api._collect_pending_reanalyze_targets(
            target="all", ids=[], limit=2
        )
        assert [i["pending_id"] for i in items] == [2, 3]

    def test_rows_missing_id_or_path_are_skipped(self):
        api, _ = _pending_api(
            rows=[
                "broken",
                {"id": 0, "path": "/p/x.png"},
                {"id": 5, "path": ""},
                {"id": 6, "path": "/p/ok.png"},
            ]
        )
        items, error = api._collect_pending_reanalyze_targets(
            target="all", ids=[], limit=None
        )
        assert error == ""
        assert [i["pending_id"] for i in items] == [6]

    def test_item_shape(self):
        api, _ = _pending_api()
        items, _error = api._collect_pending_reanalyze_targets(
            target="selected", ids=[1], limit=None
        )
        item = items[0]
        assert item["kind"] == "pending"
        assert item["pending_id"] == 1
        assert item["path"] == "/p/a.png"
        assert item["filename"] == "a.png"
        assert item["meta"]["created_at"] == 300

    def test_filename_prefers_original_name(self):
        api, _ = _pending_api()
        items, _error = api._collect_pending_reanalyze_targets(
            target="selected", ids=[2], limit=None
        )
        assert items[0]["filename"] == "b-original.png"

    def test_reads_first_page_with_hard_cap(self):
        api, calls = _pending_api()
        api._collect_pending_reanalyze_targets(target="all", ids=[], limit=None)
        assert calls == [{"page": 1, "page_size": PluginAPI.REANALYZE_MAX_ITEMS}]


class TestReanalyzeScopeRouting:
    def test_pending_scope_is_forwarded(self):
        api, _ = _pending_api()
        items, error = api._collect_reanalyze_targets(
            target="all", hashes=[], limit=None, scope="pending", ids=[]
        )
        assert error == ""
        assert [i["kind"] for i in items] == ["pending"] * 3

    def test_pending_scope_selected_uses_ids_not_hashes(self):
        api, _ = _pending_api()
        items, error = api._collect_reanalyze_targets(
            target="selected", hashes=["h1"], limit=None, scope="pending", ids=[3]
        )
        assert error == ""
        assert [i["pending_id"] for i in items] == [3]

    def test_library_scope_is_the_default(self):
        items, error = _api(_index())._collect_reanalyze_targets(
            target="all", hashes=[], limit=None
        )
        assert error == ""
        assert {i["kind"] for i in items} == {"library"}
        assert all("pending_id" not in i for i in items)


class TestMergeReanalysisFields:
    ANALYSIS = {"category": "开心", "tags": ["笑"], "desc": "一只笑猫"}

    def test_fields_narrows_scope_to_category(self):
        updates = PluginAPI._merge_reanalysis(
            {}, self.ANALYSIS, overwrite=True, fields=("category",)
        )
        assert updates == {"category": "开心"}

    def test_fields_still_respects_overwrite_false(self):
        updates = PluginAPI._merge_reanalysis(
            {"category": "难过"}, self.ANALYSIS, overwrite=False, fields=("category",)
        )
        assert updates == {}

    def test_identical_category_is_skipped(self):
        updates = PluginAPI._merge_reanalysis(
            {"category": "开心"}, self.ANALYSIS, overwrite=True, fields=("category",)
        )
        assert updates == {}

    def test_default_fields_still_exclude_category(self):
        updates = PluginAPI._merge_reanalysis({}, self.ANALYSIS, overwrite=True)
        assert "category" not in updates
        assert updates["tags"] == ["笑"]
# ── 识别失败（描述为空）检测 ─────────────────────────────


class TestMissingDescription:
    def test_blank_desc_is_missing(self):
        assert PluginAPI._missing_description({"tags": ["笑"], "desc": ""}) is True
        assert PluginAPI._missing_description({"tags": ["笑"], "desc": "  \n "}) is True
        assert PluginAPI._missing_description({"desc": None}) is True
        assert PluginAPI._missing_description({}) is True

    def test_desc_present_is_enough(self):
        # 和 _needs_reanalysis 的关键差别：只看描述，缺标签不算「识别失败」。
        assert PluginAPI._missing_description({"tags": [], "desc": "一只笑猫"}) is False


def _desc_index():
    """比 _index() 多一条「有描述但没标签」，用来区分 missing 与 no_desc。"""
    return {
        "/d/a.png": {"hash": "h1", "tags": [], "desc": "", "created_at": 300},
        "/d/b.png": {"hash": "h2", "tags": ["笑"], "desc": "有描述", "created_at": 100},
        "/d/c.png": {"hash": "h3", "tags": ["哭"], "desc": "   ", "created_at": 200},
        "/d/d.png": {"hash": "h4", "tags": [], "desc": "有描述没标签", "created_at": 50},
    }


def _desc_pending_rows():
    """待审核池版本的同一组数据：id 4 是「有描述没标签」。"""
    return [
        {"id": 1, "path": "/p/a.png", "tags": [], "desc": "", "created_at": 300},
        {"id": 2, "path": "/p/b.png", "tags": ["笑"], "desc": "有描述", "created_at": 100},
        {"id": 3, "path": "/p/c.png", "tags": ["哭"], "desc": "   ", "created_at": 200},
        {"id": 4, "path": "/p/d.png", "tags": [], "desc": "有描述没标签", "created_at": 50},
    ]


def _desc_api(index=None, rows=None, pending_total=None, pending_error=False):
    """同时带正式库索引和待审核池的 PluginAPI。"""

    def get_pending_paginated(page=1, page_size=20, **kwargs):
        if pending_error:
            raise RuntimeError("待审核查询挂了")
        data = _desc_pending_rows() if rows is None else rows
        total = len(data) if pending_total is None else pending_total
        return list(data), total, {}

    db = types.SimpleNamespace(
        get_index_cache_readonly=lambda: dict(_desc_index() if index is None else index),
        get_pending_paginated=get_pending_paginated,
    )
    plugin = types.SimpleNamespace(
        db_service=db,
        plugin_config=types.SimpleNamespace(categories=["开心"]),
        base_dir=Path("."),
        cache_service=None,
    )
    return PluginAPI(plugin)


class _Args:
    """伪造 quart 的 request.args，支持 get(key, default, type=int)。"""

    def __init__(self, values):
        self._values = {k: str(v) for k, v in (values or {}).items()}

    def get(self, key, default=None, type=None):
        if key not in self._values:
            return default
        raw = self._values[key]
        if type is None:
            return raw
        try:
            return type(raw)
        except (TypeError, ValueError):
            return default


@pytest.fixture()
def api_query(monkeypatch):
    """jsonify 直接回传 dict；返回的函数用来伪造 querystring。"""
    monkeypatch.setattr(plugin_api_module, "jsonify", lambda payload: payload)

    def _set(**query):
        monkeypatch.setattr(
            plugin_api_module,
            "request",
            types.SimpleNamespace(args=_Args(query), method="GET"),
        )

    _set()
    return _set


class TestNoDescTarget:
    def test_registered_as_a_target(self):
        assert "no_desc" in PluginAPI.REANALYZE_TARGETS

    def test_library_picks_blank_desc_only(self):
        items, error = _desc_api()._collect_reanalyze_targets(
            target="no_desc", hashes=[], limit=None
        )
        assert error == ""
        assert {i["path"] for i in items} == {"/d/a.png", "/d/c.png"}

    def test_library_missing_is_wider_than_no_desc(self):
        items, _error = _desc_api()._collect_reanalyze_targets(
            target="missing", hashes=[], limit=None
        )
        assert {i["path"] for i in items} == {"/d/a.png", "/d/c.png", "/d/d.png"}

    def test_library_respects_limit(self):
        items, _error = _desc_api()._collect_reanalyze_targets(
            target="no_desc", hashes=[], limit=1
        )
        assert len(items) == 1

    def test_library_without_match_returns_error(self):
        api = _desc_api(index={"/d/b.png": {"hash": "h2", "tags": ["笑"], "desc": "有描述"}})
        items, error = api._collect_reanalyze_targets(target="no_desc", hashes=[], limit=None)
        assert items == []
        assert "没有符合条件" in error

    def test_pending_picks_blank_desc_only(self):
        items, error = _desc_api()._collect_pending_reanalyze_targets(
            target="no_desc", ids=[], limit=None
        )
        assert error == ""
        assert {i["pending_id"] for i in items} == {1, 3}

    def test_pending_missing_is_wider_than_no_desc(self):
        items, _error = _desc_api()._collect_pending_reanalyze_targets(
            target="missing", ids=[], limit=None
        )
        assert {i["pending_id"] for i in items} == {1, 3, 4}

    def test_pending_scope_routing(self):
        items, error = _desc_api()._collect_reanalyze_targets(
            target="no_desc", hashes=[], limit=None, scope="pending", ids=[]
        )
        assert error == ""
        assert {i["pending_id"] for i in items} == {1, 3}


class TestReanalyzeScanCounts:
    def test_counts_both_sides(self, api_query):
        payload = asyncio.run(_desc_api().handle_reanalyze_scan())
        assert payload["success"] is True
        assert (payload["total"], payload["missing"], payload["no_desc"]) == (4, 3, 2)
        assert payload["pending_total"] == 4
        assert payload["pending_missing"] == 3
        assert payload["pending_no_desc"] == 2
        assert payload["max_items"] == PluginAPI.REANALYZE_MAX_ITEMS

    def test_pending_failure_keeps_library_counts(self, api_query):
        payload = asyncio.run(_desc_api(pending_error=True).handle_reanalyze_scan())
        assert payload["success"] is True
        assert payload["no_desc"] == 2
        assert payload["pending_total"] == 0
        assert payload["pending_no_desc"] == 0


class TestStatsNoDesc:
    def test_stats_expose_no_desc(self, api_query):
        payload = asyncio.run(_desc_api().handle_get_stats())
        assert payload["stats"]["total"] == 4
        assert payload["stats"]["no_desc"] == 2


class TestHandleMissingDescription:
    def test_library_is_default_and_sorts_oldest_first(self, api_query):
        payload = asyncio.run(_desc_api().handle_missing_description())
        assert payload["success"] is True
        assert payload["scope"] == "library"
        assert payload["total"] == 2
        assert payload["scanned"] == 4
        assert payload["truncated"] is False
        assert payload["max_items"] == PluginAPI.REANALYZE_MAX_ITEMS
        assert [i["hash"] for i in payload["images"]] == ["h3", "h1"]

    def test_pending_scope_sorts_oldest_first(self, api_query):
        api_query(scope="pending")
        payload = asyncio.run(_desc_api().handle_missing_description())
        assert payload["scope"] == "pending"
        assert payload["total"] == 2
        assert payload["scanned"] == 4
        assert [i["id"] for i in payload["images"]] == [3, 1]

    def test_unknown_scope_falls_back_to_library(self, api_query):
        api_query(scope="somewhere")
        payload = asyncio.run(_desc_api().handle_missing_description())
        assert payload["scope"] == "library"
        assert payload["total"] == 2

    def test_scope_is_trimmed_and_lowercased(self, api_query):
        api_query(scope=" PENDING ")
        payload = asyncio.run(_desc_api().handle_missing_description())
        assert payload["scope"] == "pending"

    def test_pagination_slices_the_list(self, api_query):
        api_query(page=2, size=1)
        payload = asyncio.run(_desc_api().handle_missing_description())
        assert (payload["page"], payload["size"], payload["total"]) == (2, 1, 2)
        assert [i["hash"] for i in payload["images"]] == ["h1"]

    def test_page_beyond_the_end_is_empty(self, api_query):
        api_query(page=9, size=1)
        payload = asyncio.run(_desc_api().handle_missing_description())
        assert payload["total"] == 2
        assert payload["images"] == []

    def test_page_and_size_are_clamped(self, api_query):
        api_query(page=0, size=999)
        payload = asyncio.run(_desc_api().handle_missing_description())
        assert (payload["page"], payload["size"]) == (1, 200)

    def test_bad_numbers_fall_back_to_defaults(self, api_query):
        api_query(page="abc", size="abc")
        payload = asyncio.run(_desc_api().handle_missing_description())
        assert (payload["page"], payload["size"]) == (1, 24)

    def test_pending_reports_pool_size_and_truncation(self, api_query):
        api_query(scope="pending")
        api = _desc_api(
            rows=[{"id": 1, "path": "/p/a.png", "desc": "", "created_at": 1}],
            pending_total=PluginAPI.REANALYZE_MAX_ITEMS + 1,
        )
        payload = asyncio.run(api.handle_missing_description())
        # scanned 是待审核池的总条数，不是这次返回的条数
        assert payload["scanned"] == PluginAPI.REANALYZE_MAX_ITEMS + 1
        assert payload["truncated"] is True
        assert payload["total"] == 1

    def test_pending_without_db_support_returns_empty(self, api_query):
        api_query(scope="pending")
        payload = asyncio.run(_api(_desc_index()).handle_missing_description())
        assert payload["success"] is True
        assert (payload["total"], payload["scanned"]) == (0, 0)
        assert payload["images"] == []

    def test_non_dict_meta_is_skipped(self, api_query):
        api = _desc_api(index={"/d/x.png": "broken", "/d/a.png": {"hash": "h1", "desc": ""}})
        payload = asyncio.run(api.handle_missing_description())
        assert payload["scanned"] == 1
        assert [i["hash"] for i in payload["images"]] == ["h1"]

    def test_legacy_string_created_at_does_not_break_sorting(self, api_query):
        api = _desc_api(
            index={
                "/d/a.png": {"hash": "h1", "desc": "", "created_at": "2026-01-01"},
                "/d/b.png": {"hash": "h2", "desc": "", "created_at": 5},
            }
        )
        payload = asyncio.run(api.handle_missing_description())
        assert payload["success"] is True
        assert [i["hash"] for i in payload["images"]] == ["h1", "h2"]

    def test_index_failure_returns_error(self, api_query):
        def boom():
            raise RuntimeError("索引读挂了")

        plugin = types.SimpleNamespace(
            db_service=types.SimpleNamespace(get_index_cache_readonly=boom),
            plugin_config=types.SimpleNamespace(),
            base_dir=Path("."),
            cache_service=None,
        )
        payload = asyncio.run(PluginAPI(plugin).handle_missing_description())
        assert payload["success"] is False
        assert "索引读挂了" in payload["error"]

    def test_route_is_registered(self):
        calls: list[tuple] = []
        context = types.SimpleNamespace(
            register_web_api=lambda path, handler, methods, desc: calls.append(
                (path, handler, tuple(methods))
            )
        )
        api = _desc_api()
        api.register(context)
        hit = [c for c in calls if c[0].endswith("/images/missing-description")]
        assert len(hit) == 1
        assert hit[0][1] == api.handle_missing_description
        assert hit[0][2] == ("GET",)
