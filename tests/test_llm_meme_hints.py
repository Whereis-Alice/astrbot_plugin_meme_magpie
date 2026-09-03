"""LLM 主动偷图自传参数（LlmMemeHints）的归一化与合并规则回归。"""

import ast
import inspect
from pathlib import Path

import pytest

from core.processing.llm_meme_hints import LlmMemeHints, clean_text
from core.processing.semantic_schema import MAX_EMOTIONS, MAX_SCENES, MAX_TAGS


class _FakeConfig:
    """只实现 normalize_category_strict 的最小配置替身。"""

    _ALIASES = {"开心": "happy", "无语": "troll", "难过": "sad"}
    _KNOWN = {"happy", "sad", "angry", "troll"}

    def normalize_category_strict(self, category):
        value = str(category or "").lower().strip()
        if value in self._KNOWN:
            return value
        return self._ALIASES.get(value)


# ── clean_text ──


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "无", "没有", "不知道", "未知", "unknown", "N/A", "none", "-", "无法识别"],
)
def test_clean_text_drops_placeholder_tokens(raw):
    assert clean_text(raw) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"五条悟"', "五条悟"),
        ("「刻晴」", "刻晴"),
        ("  排球少年  ", "排球少年"),
        ("‘abc’", "abc"),
    ],
)
def test_clean_text_strips_wrapping_quotes(raw, expected):
    assert clean_text(raw) == expected


def test_clean_text_handles_none():
    assert clean_text(None) == ""


# ── from_tool_args ──


def test_all_empty_args_produce_no_semantics():
    hints = LlmMemeHints.from_tool_args(_FakeConfig())
    assert hints.has_semantics is False
    assert hints.is_complete is False
    assert hints.to_extra_meta() == {}
    assert hints.provided_fields() == []


def test_placeholder_only_args_produce_no_semantics():
    hints = LlmMemeHints.from_tool_args(
        _FakeConfig(), emotion="不知道", character="未知", work="N/A", action="无"
    )
    assert hints.has_semantics is False


def test_chinese_emotion_alias_is_normalized():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), emotion="开心")
    assert hints.category == "happy"
    assert hints.rejected_category == ""


def test_unknown_emotion_is_rejected_not_guessed():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), emotion="嘎嘎乱杀")
    assert hints.category == ""
    assert hints.rejected_category == "嘎嘎乱杀"


def test_without_config_emotion_falls_back_to_lowercase():
    hints = LlmMemeHints.from_tool_args(None, emotion="Happy")
    assert hints.category == "happy"


def test_names_take_first_value_only():
    hints = LlmMemeHints.from_tool_args(
        _FakeConfig(), character="五条悟、夏油杰", work="咒术回战,剧场版"
    )
    assert hints.character == "五条悟"
    assert hints.work == "咒术回战"


def test_long_name_is_clipped():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), character="很" * 100)
    assert 0 < len(hints.character) <= 40


def test_semantic_fields_are_folded_into_tags():
    hints = LlmMemeHints.from_tool_args(
        _FakeConfig(), work="排球少年", character="日向翔阳", action="振臂"
    )
    assert hints.tags
    assert len(hints.tags) <= MAX_TAGS
    assert "排球少年" in hints.tags or "日向翔阳" in hints.tags


def test_explicit_tags_win_over_derived_ones():
    hints = LlmMemeHints.from_tool_args(
        _FakeConfig(), tags="打排球,热血,青春", work="排球少年", character="日向翔阳"
    )
    assert hints.tags[:3] == ["打排球", "热血", "青春"]
    assert len(hints.tags) == MAX_TAGS


def test_emotions_default_to_category():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), emotion="开心", work="孤独摇滚")
    assert hints.emotions == ["happy"]


def test_scenes_are_split_and_capped():
    hints = LlmMemeHints.from_tool_args(
        _FakeConfig(), scenes="被吐槽时,认输时,道歉时,尴尬时"
    )
    assert len(hints.scenes) == MAX_SCENES


# ── describe / desc ──


def test_describe_composes_work_character_action():
    hints = LlmMemeHints.from_tool_args(
        _FakeConfig(), work="咒术回战", character="五条悟", action="捂脸"
    )
    assert hints.describe() == "《咒术回战》五条悟捂脸"
    assert hints.desc == "《咒术回战》五条悟捂脸"
    assert hints.desc_explicit is False


def test_describe_falls_back_to_overlay_text():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), overlay_text="你在教我做事？")
    assert hints.describe() == "你在教我做事？"


def test_explicit_desc_beats_vlm_desc():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), desc="一只很生气的猫")
    assert hints.desc_explicit is True
    assert hints.resolve_desc("VLM 写的描述") == "一只很生气的猫"


def test_vlm_desc_beats_derived_desc():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), work="孤独摇滚", character="波奇")
    assert hints.resolve_desc("角色缩在角落瑟瑟发抖") == "角色缩在角落瑟瑟发抖"


def test_derived_desc_used_when_vlm_silent():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), work="孤独摇滚", character="波奇")
    assert hints.resolve_desc("") == "《孤独摇滚》波奇"


def test_resolve_overlay_text_prefers_llm():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), overlay_text="确实")
    assert hints.resolve_overlay_text("VLM OCR 结果") == "确实"


def test_resolve_overlay_text_falls_back_to_vlm():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), work="孤独摇滚")
    assert hints.resolve_overlay_text("VLM OCR 结果") == "VLM OCR 结果"


# ── is_complete ──


def test_is_complete_requires_category_and_one_semantic_field():
    assert LlmMemeHints.from_tool_args(_FakeConfig(), emotion="开心").is_complete is False
    assert (
        LlmMemeHints.from_tool_args(_FakeConfig(), work="排球少年").is_complete is False
    )
    assert (
        LlmMemeHints.from_tool_args(
            _FakeConfig(), emotion="开心", work="排球少年"
        ).is_complete
        is True
    )


# ── merge ──


def test_merge_tags_puts_llm_first_and_dedupes():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), tags="五条悟")
    merged = hints.merge_tags(["五条悟", "白发", "墨镜"])
    assert merged[0] == "五条悟"
    assert len(merged) == len(set(merged)) <= MAX_TAGS


def test_merge_emotions_respects_cap():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), emotion="开心")
    merged = hints.merge_emotions(["excitement", "love", "thank", "shy"])
    assert merged[0] == "happy"
    assert len(merged) <= MAX_EMOTIONS


def test_merge_scenes_accepts_none():
    hints = LlmMemeHints.from_tool_args(_FakeConfig(), scenes="认输时")
    assert hints.merge_scenes(None) == ["认输时"]


# ── to_extra_meta ──


def test_to_extra_meta_only_includes_present_fields():
    hints = LlmMemeHints.from_tool_args(
        _FakeConfig(), emotion="开心", work="排球少年", character="日向翔阳"
    )
    meta = hints.to_extra_meta()
    assert meta["work"] == "排球少年"
    assert meta["character"] == "日向翔阳"
    assert meta["emotions"] == ["happy"]
    assert "overlay_text" not in meta


def test_provided_fields_reports_llm_supplied_data():
    hints = LlmMemeHints.from_tool_args(
        _FakeConfig(), emotion="开心", work="排球少年", action="振臂"
    )
    assert hints.provided_fields() == ["分类", "作品", "动作", "标签"]


# ── llm_tool docstring 契约 ──
#
# AstrBot 从 docstring 的 Args: 推导 JSON schema，任一参数缺类型注释或类型不受支持
# 都会在插件加载时抛 ValueError（astrbot/core/star/register/star_handler.py）。
# 这里把这条契约固化成测试，避免以后改 docstring 把插件改崩。

_PY_TO_JSON_TYPE = {
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "str": "string",
    "dict": "object",
    "list": "array",
    "tuple": "array",
    "set": "array",
}
_SUPPORTED_TYPES = {"string", "number", "object", "array", "boolean"}


def _iter_llm_tool_functions():
    """从 main.py 源码里静态抓出所有 @filter.llm_tool 装饰的函数。"""
    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            if isinstance(target, ast.Attribute) and target.attr == "llm_tool":
                yield node
                break


def test_main_declares_expected_llm_tools():
    names = sorted(node.name for node in _iter_llm_tool_functions())
    assert names == ["search_meme", "send_meme", "steal_sticker"]


def test_every_llm_tool_param_has_a_supported_docstring_type():
    docstring_parser = pytest.importorskip("docstring_parser")
    for node in _iter_llm_tool_functions():
        doc = ast.get_docstring(node)
        assert doc, f"{node.name} 缺少 docstring"
        parsed = docstring_parser.parse(doc)
        documented = {}
        for param in parsed.params:
            type_name = param.type_name
            assert type_name, f"{node.name}.{param.arg_name} 缺少类型注释"
            type_name = _PY_TO_JSON_TYPE.get(type_name, type_name)
            assert type_name in _SUPPORTED_TYPES, (
                f"{node.name}.{param.arg_name} 类型 {param.type_name} 不受支持"
            )
            documented[param.arg_name] = type_name

        signature_args = [
            arg.arg
            for arg in node.args.args
            if arg.arg not in {"self", "event", "cls"}
        ]
        assert set(documented) == set(signature_args), (
            f"{node.name} 的 docstring 参数与函数签名不一致"
        )


def test_steal_tool_optional_params_all_have_defaults():
    """AstrBot 生成的 schema 没有 required，Python 侧必须给默认值，否则 LLM 少传就 TypeError。"""
    node = next(n for n in _iter_llm_tool_functions() if n.name == "steal_sticker")
    args = [a.arg for a in node.args.args if a.arg not in {"self", "event"}]
    defaults = node.args.defaults
    without_default = args[: len(args) - len(defaults)]
    # image_ref 是唯一允许无默认值的必填参数
    assert without_default == ["image_ref"]


def test_steal_tool_advertises_work_and_character_params():
    node = next(n for n in _iter_llm_tool_functions() if n.name == "steal_sticker")
    doc = ast.get_docstring(node) or ""
    for keyword in ("work(string)", "character(string)", "action(string)"):
        assert keyword in doc
    assert inspect.cleandoc(doc)
