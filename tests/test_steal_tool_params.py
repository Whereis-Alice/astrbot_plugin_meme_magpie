"""steal_sticker（magpie_steal_meme）参数直传链路的端到端测试。

只替换 IO 边界（下载 / 视觉分析 / 索引落盘），验证：
- LLM 传的 work / character / action 等参数确实变成 llm_hints 传给处理流水线
- llm_steal_param_mode 三种模式的 skip_vlm 行为
- 入库来源标记为 add_method="llm"（上游这里错写成 "auto"）
"""

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

# main.py 用的是包内相对导入，必须以包形式导入；
# 同时 LlmMemeHints 也要走同一份模块身份，否则 isinstance 会失败。
_PKG = Path(__file__).resolve().parents[1].name
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

LlmMemeHints = importlib.import_module(
    f"{_PKG}.core.processing.llm_meme_hints"
).LlmMemeHints
Main = importlib.import_module(f"{_PKG}.main").Main

FULL_ARGS = {
    "emotion": "happy",
    "work": "孤独摇滚",
    "character": "后藤一里",
    "action": "抱着吉他发抖",
    "overlay_text": "我不想努力了",
    "tags": "吉他,社恐",
    "desc": "抱着吉他缩在角落的粉发少女",
    "scenes": "自嘲时,被cue时",
}


class _Config:
    steal_meme = True
    content_filtration = False
    llm_steal_param_mode = "merge"
    categories = ["happy", "sad", "angry"]
    category_info: dict = {}
    character_info: dict = {}

    def get_categories(self):
        return list(self.categories)

    def get_category_info(self):
        return dict(self.category_info)


def _make_plugin(tmp_path, *, param_mode="merge", entry=None):
    """构造一个只保留 steal_sticker 所需依赖的 Main 实例。"""
    image = tmp_path / "sticker.png"
    image.write_bytes(b"fake-png-bytes")

    plugin = Main.__new__(Main)
    cfg = _Config()
    cfg.llm_steal_param_mode = param_mode
    plugin.plugin_config = cfg
    plugin.content_filtration_fail_open = False
    plugin.calls = []

    stored = entry or {
        "category": "happy",
        "work": "孤独摇滚",
        "character": "后藤一里",
        "tags": ["吉他", "社恐"],
        "desc": "抱着吉他缩在角落的粉发少女",
        "scenes": ["自嘲时"],
        "overlay_text": "我不想努力了",
    }
    new_path = str(tmp_path / "stored.png")

    async def _load_index():
        return {}

    async def _save_index(idx):
        plugin.saved_index = idx
        return True

    plugin.index_manager = SimpleNamespace(
        load_index=_load_index, save_index=_save_index
    )
    plugin.is_steal_enabled_for_event = lambda event: True
    plugin._get_event_handler = lambda **kwargs: SimpleNamespace()
    plugin._precheck_image_file = lambda path: (True, "")
    plugin._build_steal_tool_extra_meta = lambda event, ref, source="": {"source": source}

    async def _resolve(event, ref, handler):
        return str(image), "llm_tool"

    plugin._resolve_steal_image_ref = _resolve

    async def _process_image(event, file_path, **kwargs):
        plugin.calls.append(kwargs)
        return True, {new_path: dict(stored)}

    plugin._process_image = _process_image
    return plugin


def _run(plugin, **overrides):
    args = dict(FULL_ARGS)
    args.update(overrides)
    ref = args.pop("image_ref", "https://example.com/a.png")

    async def _collect():
        out = []
        async for chunk in plugin.steal_sticker(object(), ref, **args):
            out.append(str(chunk))
        return out

    return asyncio.run(_collect())


# ── 参数直传 ─────────────────────────────────────────────


class TestHintsPlumbing:
    def test_llm_args_become_hints(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)
        _run(plugin)
        assert len(plugin.calls) == 1
        hints = plugin.calls[0]["llm_hints"]
        assert isinstance(hints, LlmMemeHints)
        assert hints.work == "孤独摇滚"
        assert hints.character == "后藤一里"
        assert "吉他" in hints.tags
        assert hints.overlay_text == "我不想努力了"

    def test_add_method_is_llm(self, tmp_path: Path):
        # 上游这里传的是 "auto"，导致 WebUI 里分不清哪些是 LLM 主动收的。
        plugin = _make_plugin(tmp_path)
        _run(plugin)
        assert plugin.calls[0]["add_method"] == "llm"

    def test_only_category_is_not_enough_to_skip_vlm(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)
        _run(plugin, work="", character="", action="", overlay_text="", tags="", scenes="",
             desc="", emotion="happy")
        call = plugin.calls[0]
        hints = call["llm_hints"]
        # 只给分类：分类会被采纳（它决定入库目录），
        # 但语义字段全空，仍需视觉模型补。
        assert isinstance(hints, LlmMemeHints)
        assert hints.category == "happy"
        assert hints.tags == []
        assert hints.is_complete is False
        assert call["skip_vlm"] is False

    def test_only_work_is_enough_to_build_hints(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)
        _run(plugin, character="", action="", overlay_text="", tags="", scenes="", desc="")
        hints = plugin.calls[0]["llm_hints"]
        assert isinstance(hints, LlmMemeHints)
        assert hints.work == "孤独摇滚"

    def test_no_args_at_all_falls_back_to_vlm(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)
        _run(plugin, **{k: "" for k in FULL_ARGS})
        assert plugin.calls[0]["llm_hints"] is None
        assert plugin.calls[0]["skip_vlm"] is False

# ── llm_steal_param_mode 三种策略 ─────────────────────────


class TestParamModes:
    def test_declared_modes(self):
        assert Main.LLM_STEAL_PARAM_MODES == frozenset(
            {"merge", "llm_first", "vlm_only"}
        )

    def test_merge_keeps_vlm(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path, param_mode="merge")
        _run(plugin)
        call = plugin.calls[0]
        assert isinstance(call["llm_hints"], LlmMemeHints)
        assert call["skip_vlm"] is False

    def test_llm_first_with_full_args_skips_vlm(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path, param_mode="llm_first")
        _run(plugin)
        assert plugin.calls[0]["skip_vlm"] is True

    def test_llm_first_without_category_keeps_vlm(self, tmp_path: Path):
        # 分类是硬需求（决定入库目录），缺了就必须让视觉模型补。
        plugin = _make_plugin(tmp_path, param_mode="llm_first")
        _run(plugin, emotion="")
        call = plugin.calls[0]
        assert isinstance(call["llm_hints"], LlmMemeHints)
        assert call["skip_vlm"] is False

    def test_llm_first_with_unknown_category_keeps_vlm(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path, param_mode="llm_first")
        _run(plugin, emotion="不知道")
        assert plugin.calls[0]["skip_vlm"] is False

    def test_vlm_only_drops_hints(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path, param_mode="vlm_only")
        _run(plugin)
        call = plugin.calls[0]
        assert call["llm_hints"] is None
        assert call["skip_vlm"] is False

    def test_unknown_mode_falls_back_to_merge(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path, param_mode="bogus")
        _run(plugin)
        call = plugin.calls[0]
        assert isinstance(call["llm_hints"], LlmMemeHints)
        assert call["skip_vlm"] is False

    def test_mode_is_trimmed_and_lowercased(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path, param_mode="  LLM_First  ")
        _run(plugin)
        assert plugin.calls[0]["skip_vlm"] is True


# ── 返回给 LLM 的文案 ─────────────────────────────────────


def _text(plugin, **overrides) -> str:
    return "\n".join(_run(plugin, **overrides))


class TestToolOutput:
    def test_success_prefix_and_fields(self, tmp_path: Path):
        out = _text(_make_plugin(tmp_path))
        assert out.startswith("偷取成功！")
        assert "- 分类：happy" in out
        assert "- 作品：孤独摇滚" in out
        assert "- 角色：后藤一里" in out
        assert "- 图上文字：我不想努力了" in out

    def test_work_line_omitted_when_absent(self, tmp_path: Path):
        plugin = _make_plugin(
            tmp_path, entry={"category": "happy", "tags": [], "desc": "", "scenes": []}
        )
        out = _text(plugin)
        assert "- 作品：" not in out
        assert "- 角色：" not in out
        assert "- 标签：无" in out
        assert "- 描述：无" in out

    def test_llm_first_reports_no_vlm_call(self, tmp_path: Path):
        out = _text(_make_plugin(tmp_path, param_mode="llm_first"))
        assert "全部采用你传的参数" in out
        assert "未调用视觉模型" in out

    def test_merge_reports_partial_origin(self, tmp_path: Path):
        out = _text(_make_plugin(tmp_path, param_mode="merge"))
        assert "你提供：" in out
        assert "其余由视觉模型补全" in out

    def test_vlm_only_reports_full_vlm_origin(self, tmp_path: Path):
        out = _text(_make_plugin(tmp_path, param_mode="vlm_only"))
        assert "全部由视觉模型分析" in out

    def test_no_args_reports_full_vlm_origin(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)
        out = _text(plugin, **{k: "" for k in FULL_ARGS})
        assert "全部由视觉模型分析" in out


# ── 拒绝与失败路径 ───────────────────────────────────────


class TestGuards:
    def test_disabled_by_config(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)
        plugin.plugin_config.steal_meme = False
        out = _text(plugin)
        assert out.startswith("偷取失败：")
        assert "未开启" in out
        assert plugin.calls == []

    def test_disabled_for_session(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)
        plugin.is_steal_enabled_for_event = lambda event: False
        out = _text(plugin)
        assert "已禁用" in out
        assert plugin.calls == []

    def test_precheck_failure_is_surfaced(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)
        plugin._precheck_image_file = lambda path: (False, "图片过大")
        out = _text(plugin)
        assert out == "偷取失败：图片过大"
        assert plugin.calls == []

    def test_missing_file_is_reported(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)

        async def _resolve(event, ref, handler):
            return str(tmp_path / "nope.png"), "llm_tool"

        plugin._resolve_steal_image_ref = _resolve
        out = _text(plugin)
        assert "图片文件不存在" in out

    def test_store_failure_mentions_vlm(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)

        async def _fail(event, file_path, **kwargs):
            plugin.calls.append(kwargs)
            return False, None

        plugin._process_image = _fail
        out = _text(plugin)
        assert "VLM 分析未通过" in out

    def test_store_failure_without_vlm_has_dedicated_reason(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path, param_mode="llm_first")

        async def _fail(event, file_path, **kwargs):
            plugin.calls.append(kwargs)
            return False, None

        plugin._process_image = _fail
        out = _text(plugin)
        assert "VLM" not in out
        assert "相似度过高" in out

    def test_exception_is_caught(self, tmp_path: Path):
        plugin = _make_plugin(tmp_path)

        async def _boom(event, file_path, **kwargs):
            raise RuntimeError("disk on fire")

        plugin._process_image = _boom
        out = _text(plugin)
        assert out.startswith("偷取出错：")
        assert "disk on fire" in out
