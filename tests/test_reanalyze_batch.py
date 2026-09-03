"""批量重新识别相关纯函数的单元测试（不触发真实 VLM 与磁盘写入）。"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
