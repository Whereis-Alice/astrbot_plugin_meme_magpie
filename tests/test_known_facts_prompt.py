"""已知信息（known facts）注入 VLM 提示词的单元测试。"""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrbot_plugin_meme_magpie.core.processing.prompt_manager import PromptManager

PM = PromptManager
_TEMPLATE = "分类候选：{emotion_list}\n\n{known_facts}\n\n<output_format>JSON</output_format>"


def _pm(prompt=None, filter_prompt=None, categories=None):
    cats = list(categories or ["开心", "生气"])
    cfg = types.SimpleNamespace(
        categories=cats,
        category_info={},
        get_vlm_categories=lambda: list(cats),
    )
    pm = PromptManager(types.SimpleNamespace(plugin_config=cfg))
    if prompt is not None:
        pm.emoji_classification_prompt = prompt
    if filter_prompt is not None:
        pm.emoji_classification_with_filter_prompt = filter_prompt
    return pm


class TestNormalizeKnownFacts:
    def test_empty_sources(self):
        assert PM.normalize_known_facts(None) == {}
        assert PM.normalize_known_facts({}) == {}
        assert PM.normalize_known_facts("") == {}

    def test_dict_source(self):
        got = PM.normalize_known_facts(
            {
                "work": "孤独摇滚",
                "character": "后藤一里",
                "action": "抱着吉他发抖",
                "overlay_text": "我不行了",
            }
        )
        assert got == {
            "work": "孤独摇滚",
            "character": "后藤一里",
            "action": "抱着吉他发抖",
            "overlay_text": "我不行了",
        }

    def test_object_source_skips_empty_attrs(self):
        hints = types.SimpleNamespace(
            work="轻音少女", character="平泽唯", action=None, overlay_text=""
        )
        assert PM.normalize_known_facts(hints) == {"work": "轻音少女", "character": "平泽唯"}

    def test_object_missing_attrs_ok(self):
        assert PM.normalize_known_facts(types.SimpleNamespace(work="A")) == {"work": "A"}

    def test_unrelated_keys_dropped(self):
        got = PM.normalize_known_facts({"work": "A", "category": "开心", "tags": ["x"]})
        assert got == {"work": "A"}

    def test_non_string_values_coerced(self):
        got = PM.normalize_known_facts({"work": 2024, "character": 7})
        assert got == {"work": "2024", "character": "7"}

    def test_whitespace_collapsed(self):
        assert PM.normalize_known_facts({"work": "  轻音   少女 \n 第一季 "}) == {
            "work": "轻音 少女 第一季"
        }

    def test_blank_only_value_dropped(self):
        assert PM.normalize_known_facts({"work": "  \n ", "character": "A"}) == {"character": "A"}

    def test_long_value_truncated(self):
        assert PM._KNOWN_FACT_MAX_LEN == 80
        got = PM.normalize_known_facts({"work": "字" * 200})
        assert len(got["work"]) == PM._KNOWN_FACT_MAX_LEN

    def test_label_order_is_stable(self):
        assert [key for key, _label in PM._KNOWN_FACT_LABELS] == [
            "work",
            "character",
            "action",
            "overlay_text",
        ]


class TestRenderKnownFactsBlock:
    def test_empty_returns_blank(self):
        assert PM.render_known_facts_block(None) == ""
        assert PM.render_known_facts_block({}) == ""
        assert PM.render_known_facts_block({"work": "  "}) == ""

    def test_wrapped_in_tag(self):
        block = PM.render_known_facts_block({"work": "A", "character": "B"})
        assert block.startswith("<known_facts>")
        assert block.endswith("</known_facts>")
        assert "- 作品：A" in block
        assert "- 角色：B" in block

    def test_lines_follow_label_order(self):
        block = PM.render_known_facts_block(
            {"overlay_text": "文字", "action": "动作", "character": "角色", "work": "作品名"}
        )
        assert (
            block.index("- 作品：")
            < block.index("- 角色：")
            < block.index("- 动作：")
            < block.index("- 图上文字：")
        )

    def test_only_given_fields_rendered(self):
        block = PM.render_known_facts_block({"work": "A"})
        assert "- 角色：" not in block
        assert "- 动作：" not in block
        assert "- 图上文字：" not in block

    def test_overlay_text_adds_reuse_instruction(self):
        with_text = PM.render_known_facts_block({"overlay_text": "早上好"})
        without_text = PM.render_known_facts_block({"work": "A"})
        assert "overlay_text 直接沿用上面给出的图上文字。" in with_text
        assert "overlay_text 直接沿用" not in without_text

    def test_always_tells_model_to_reuse_facts(self):
        block = PM.render_known_facts_block({"work": "A"})
        assert "不要推翻或改写" in block
        assert "description 与 scenes" in block


class TestKnownFactsSignature:
    def test_empty(self):
        assert PM.known_facts_signature(None) == ""
        assert PM.known_facts_signature({}) == ""

    def test_key_order_independent(self):
        left = PM.known_facts_signature({"character": "B", "work": "A"})
        right = PM.known_facts_signature({"work": "A", "character": "B"})
        assert left == right == "work=A|character=B"

    def test_different_values_produce_different_signature(self):
        assert PM.known_facts_signature({"work": "A"}) != PM.known_facts_signature({"work": "B"})

    def test_signature_uses_normalized_value(self):
        assert PM.known_facts_signature({"work": "  A  B  "}) == "work=A B"

    def test_object_source(self):
        hints = types.SimpleNamespace(work="A", character=None, action="挥手", overlay_text=None)
        assert PM.known_facts_signature(hints) == "work=A|action=挥手"


class TestInjectKnownFacts:
    def test_placeholder_replaced_in_place(self):
        out = PM._inject_known_facts(_TEMPLATE, "<known_facts>X</known_facts>")
        assert "{known_facts}" not in out
        assert out.index("<known_facts>X") < out.index("<output_format>")

    def test_placeholder_cleaned_when_no_facts(self):
        out = PM._inject_known_facts(_TEMPLATE, "")
        assert "{known_facts}" not in out
        assert "\n\n\n" not in out
        assert "<output_format>" in out

    def test_custom_template_without_placeholder_uses_anchor(self):
        out = PM._inject_known_facts("自定义提示词\n<output_format>JSON</output_format>", "BLOCK")
        assert out.index("BLOCK") < out.index("<output_format>")

    def test_custom_template_without_anchor_appends(self):
        out = PM._inject_known_facts("完全自定义的提示词", "BLOCK")
        assert out.startswith("完全自定义的提示词")
        assert out.endswith("BLOCK")

    def test_anchor_at_position_zero_appends(self):
        out = PM._inject_known_facts("<output_format>JSON</output_format>", "BLOCK")
        assert out.startswith("<output_format>")
        assert out.endswith("BLOCK")

    def test_template_untouched_without_facts(self):
        assert PM._inject_known_facts("原样保留", "") == "原样保留"

    def test_empty_template(self):
        assert PM._inject_known_facts("", "BLOCK") == "BLOCK"
        assert PM._inject_known_facts("", "") == ""


class TestBuildClassificationPromptWithKnown:
    def test_renders_both_placeholders(self):
        out = _pm(prompt=_TEMPLATE).build_classification_prompt(
            known={"work": "孤独摇滚", "character": "后藤一里"}
        )
        assert "开心" in out and "生气" in out
        assert "- 作品：孤独摇滚" in out
        assert "- 角色：后藤一里" in out
        assert "{known_facts}" not in out
        assert "{emotion_list}" not in out

    def test_no_known_no_block(self):
        out = _pm(prompt=_TEMPLATE).build_classification_prompt()
        assert "<known_facts>" not in out
        assert "{known_facts}" not in out
        assert "开心" in out

    def test_filter_template_supports_known(self):
        out = _pm(filter_prompt=_TEMPLATE).build_classification_prompt(
            use_filter=True, known={"character": "平泽唯"}
        )
        assert "- 角色：平泽唯" in out

    def test_hints_object_accepted(self):
        hints = types.SimpleNamespace(
            work="A", character="B", action="挥手", overlay_text="早上好"
        )
        out = _pm(prompt=_TEMPLATE).build_classification_prompt(known=hints)
        assert "- 动作：挥手" in out
        assert "- 图上文字：早上好" in out

    def test_shipped_templates_carry_placeholder(self):
        path = Path(__file__).resolve().parents[1] / "prompts.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("EMOJI_CLASSIFICATION_PROMPT", "EMOJI_CLASSIFICATION_WITH_FILTER_PROMPT"):
            template = data[key]
            assert "{known_facts}" in template, key
            assert template.index("{known_facts}") < template.index("<output_format>"), key
