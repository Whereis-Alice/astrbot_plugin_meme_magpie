"""VLM 提示词默认值与“旧默认值自愈”的回归测试。

早期版本（含上游 stealer）把内置提示词写进 _conf_schema.json 的 default，
AstrBot 会把 schema 默认值落盘到用户配置，导致“用户自定义”永远非空、
内置提示词的后续改进永远到不了用户。这里守住修好之后的行为。
"""

import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# PluginConfig.__init__ 会调用 StarTools.get_data_dir，给每个实例一个独立临时目录
_star_module = sys.modules.get("astrbot.api.star")
if _star_module is not None:
    _star_module.StarTools = types.SimpleNamespace(
        get_data_dir=lambda name: os.path.join(tempfile.mkdtemp(prefix="magpie_prompt_test_"), name)
    )
    _star_module.Context = object
    _star_module.Star = object

from core.config import config as config_mod  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# (配置项名, prompts.json 里的键名)
PROMPT_KEYS = (
    ("custom_meme_classification_prompt", "EMOJI_CLASSIFICATION_PROMPT"),
    (
        "custom_meme_classification_with_filter_prompt",
        "EMOJI_CLASSIFICATION_WITH_FILTER_PROMPT",
    ),
)

BUNDLED = {
    "EMOJI_CLASSIFICATION_PROMPT": "PLUGIN A {emotion_list}",
    "EMOJI_CLASSIFICATION_WITH_FILTER_PROMPT": "PLUGIN B {emotion_list}",
}


def _load(name: str) -> dict:
    return json.loads((PLUGIN_DIR / name).read_text(encoding="utf-8"))


def test_schema_defaults_must_stay_empty():
    """schema default 一旦非空就会被 AstrBot 落盘，内置提示词从此更新不了。"""
    schema = _load("_conf_schema.json")
    for config_key, _bundled_key in PROMPT_KEYS:
        assert schema[config_key]["default"] == "", config_key
        assert "留空" in schema[config_key]["hint"], config_key


def test_bundled_prompts_are_not_mistaken_for_legacy_copies():
    bundled = _load("prompts.json")
    for _config_key, bundled_key in PROMPT_KEYS:
        assert not config_mod._is_legacy_builtin_prompt(bundled[bundled_key]), bundled_key


def test_fingerprint_ignores_line_ending_and_trailing_whitespace():
    plain = config_mod._prompt_fingerprint("第一行\n第二行")
    messy = config_mod._prompt_fingerprint("\n\r\n第一行  \r\n第二行\t\r\n\n")
    assert plain == messy


def test_fingerprint_set_covers_prompts_shipped_as_schema_defaults():
    """这几条分别是本插件 v1.4.2 与上游 stealer v3.0.0 落盘过的默认提示词。"""
    known = (
        "7c1da5f6f69203ac74ba798b12301d097e0b685a2c9a9d619e22b02a2c46d899",
        "9bb9f189a6c76f7ab12d8a6f8dcbb5ea650e4bf53db69042a66e31d2fa2d3119",
        "5226741ed4541357c5f644df62597c861f7aed7ea543ea013d57fdc0baad70bf",
        "ed0cf59b39baf77087ac3f273611a892a404ed1f441acf2d7d33b5fd57e81c71",
    )
    for fingerprint in known:
        assert fingerprint in config_mod._LEGACY_BUILTIN_PROMPT_FINGERPRINTS, fingerprint
    assert len(config_mod._LEGACY_BUILTIN_PROMPT_FINGERPRINTS) >= 14


def _pin_legacy(monkeypatch, *texts):
    monkeypatch.setattr(
        config_mod,
        "_LEGACY_BUILTIN_PROMPT_FINGERPRINTS",
        frozenset(config_mod._prompt_fingerprint(text) for text in texts),
    )
    monkeypatch.setattr(config_mod, "_healed_prompt_keys", set())


def test_get_prompts_treats_legacy_copy_as_not_customized(monkeypatch):
    legacy = "旧版内置提示词 {emotion_list}"
    _pin_legacy(monkeypatch, legacy)
    fake_cfg = types.SimpleNamespace(
        # 落盘往往带上换行/行尾空白的差异，归一化后仍要认出来
        custom_meme_classification_prompt="\r\n" + legacy + "  \r\n",
        custom_meme_classification_with_filter_prompt="我自己写的 {emotion_list}",
    )
    result = config_mod.PluginConfig.get_prompts(fake_cfg, BUNDLED)
    assert result["emoji_classification_prompt"] == "PLUGIN A {emotion_list}"
    assert result["emoji_classification_with_filter_prompt"] == "我自己写的 {emotion_list}"


def test_get_prompts_keeps_edited_legacy_prompt(monkeypatch):
    """用户在旧默认值上改了一处，就是真的自定义，必须原样尊重。"""
    legacy = "旧版内置提示词 {emotion_list}"
    _pin_legacy(monkeypatch, legacy)
    edited = legacy + "\n补一句我自己的要求"
    fake_cfg = types.SimpleNamespace(
        custom_meme_classification_prompt=edited,
        custom_meme_classification_with_filter_prompt="",
    )
    result = config_mod.PluginConfig.get_prompts(fake_cfg, BUNDLED)
    assert result["emoji_classification_prompt"] == edited
    assert result["emoji_classification_with_filter_prompt"] == "PLUGIN B {emotion_list}"


def test_legacy_prompt_config_is_cleared_and_saved(monkeypatch):
    """构造 PluginConfig 时就把落盘的旧默认值清成空串，配置页不再显示一大段假自定义。"""
    legacy_plain = "旧版内置提示词 {emotion_list}"
    legacy_filter = "旧版内置审核提示词 {emotion_list}"
    _pin_legacy(monkeypatch, legacy_plain, legacy_filter)

    saved: list[dict | None] = []

    class _FakeAstrBotConfig(dict):
        def save_config(self, replace_config=None):
            saved.append(replace_config)

    raw = _FakeAstrBotConfig(
        custom_meme_classification_prompt=legacy_plain,
        custom_meme_classification_with_filter_prompt=legacy_filter + "\n我改过一行",
    )
    cfg = config_mod.PluginConfig(raw)

    assert raw["custom_meme_classification_prompt"] == ""
    assert cfg.custom_meme_classification_prompt == ""
    # 改过的那条不动
    assert raw["custom_meme_classification_with_filter_prompt"].endswith("我改过一行")
    assert len(saved) == 1

    result = cfg.get_prompts(BUNDLED)
    assert result["emoji_classification_prompt"] == "PLUGIN A {emotion_list}"
    assert result["emoji_classification_with_filter_prompt"].endswith("我改过一行")


def test_untouched_config_is_not_saved(monkeypatch):
    """没有旧默认值残留时不要无谓回写配置文件。"""
    _pin_legacy(monkeypatch, "旧版内置提示词 {emotion_list}")
    saved: list[dict | None] = []

    class _FakeAstrBotConfig(dict):
        def save_config(self, replace_config=None):
            saved.append(replace_config)

    cfg = config_mod.PluginConfig(_FakeAstrBotConfig(custom_meme_classification_prompt=""))
    assert saved == []
    assert cfg.get_prompts(BUNDLED)["emoji_classification_prompt"] == "PLUGIN A {emotion_list}"
