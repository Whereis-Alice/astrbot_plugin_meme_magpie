"""提示词管理器：负责加载、缓存和渲染 VLM 分类提示词模板。"""

import re
from typing import Any

_BLANK_LINES_RE = re.compile(r"\n{3,}")


class PromptManager:
    """管理表情包分类所需的 VLM 提示词。"""

    _PROMPT_PLACEHOLDER = "{emotion_list}"
    _KNOWN_FACTS_PLACEHOLDER = "{known_facts}"
    _KNOWN_FACTS_ANCHOR = "<output_format>"
    _KNOWN_FACT_MAX_LEN = 80
    _KNOWN_FACT_LABELS = (
        ("work", "作品"),
        ("character", "角色"),
        ("action", "动作"),
        ("overlay_text", "图上文字"),
    )

    _FALLBACK_PROMPT = (
        "分析表情包：从 `{emotion_list}` 中选择情绪分类，每个分类机会均等。"
        '返回JSON格式：{"category": "分类名", "tags": [], '
        '"description": "画面描述", "overlay_text": "", "scenes": []}'
        "tags 没有则 []。"
    )
    _FALLBACK_FILTER_PROMPT = (
        '审核图片是否含不当内容，不当则返回{"approved": false, "reason": "审核不通过"}。'
        "否则从 `{emotion_list}` 中选择情绪分类，每个分类机会均等。"
        '返回JSON格式：{"approved": true, "category": "分类名", "tags": [], '
        '"description": "画面描述", "overlay_text": "", "scenes": []}'
        "tags 没有则 []。"
    )

    def __init__(self, plugin_instance: Any) -> None:
        self.plugin = plugin_instance
        self.plugin_config = getattr(plugin_instance, "plugin_config", None)

        self.emoji_classification_prompt = getattr(
            plugin_instance, "EMOJI_CLASSIFICATION_PROMPT", self._FALLBACK_PROMPT
        )
        self.emoji_classification_with_filter_prompt = getattr(
            plugin_instance,
            "EMOJI_CLASSIFICATION_WITH_FILTER_PROMPT",
            self._FALLBACK_FILTER_PROMPT,
        )
        self.categories = list(self.plugin_config.categories or []) if self.plugin_config else []

    def update_config(
        self,
        categories=None,
        emoji_classification_prompt=None,
        emoji_classification_with_filter_prompt=None,
    ) -> None:
        if categories is not None:
            self.categories = categories
        if emoji_classification_prompt is not None:
            self.emoji_classification_prompt = emoji_classification_prompt
        if emoji_classification_with_filter_prompt is not None:
            self.emoji_classification_with_filter_prompt = emoji_classification_with_filter_prompt

    def build_classification_prompt(
        self,
        *,
        use_filter: bool = False,
        categories: list[str] | None = None,
        known: Any = None,
    ) -> str:
        """根据当前配置构建完整的 VLM 分类提示词。

        known 是调用方已经确认的信息（作品 / 角色 / 动作 / 图上文字），
        可以是 dict，也可以是 LlmMemeHints 实例。传了就会写进提示词，
        让视觉模型直接采用而不是自己猜。
        """
        if categories is None and self.plugin_config and hasattr(
            self.plugin_config, "get_vlm_categories"
        ):
            categories = self.plugin_config.get_vlm_categories()
        elif categories:
            categories = [item for item in categories if item != "other"]
        emotion_list = self._build_emotion_list_str(categories)
        template = (
            self.emoji_classification_with_filter_prompt
            if use_filter
            else self.emoji_classification_prompt
        )
        rendered = self._render_prompt_template(template, emotion_list)
        return self._inject_known_facts(rendered, self.render_known_facts_block(known))

    def _build_emotion_list_str(self, categories: list[str] | None = None) -> str:
        categories = categories if categories is not None else (self.categories or [])
        categories = [c for c in categories if isinstance(c, str) and c.strip()]
        info_map = getattr(self.plugin_config, "category_info", None) or {}

        lines = []
        for raw_key in categories:
            key = raw_key.strip()
            info = info_map.get(key)
            if isinstance(info, dict):
                name = str(info.get("name", "")).strip()
                desc = str(info.get("desc", "")).strip()
            else:
                name = ""
                desc = ""

            if name and name != key:
                if desc:
                    lines.append(f"{key} - {name}：{desc}")
                else:
                    lines.append(f"{key} - {name}")
            else:
                if desc:
                    lines.append(f"{key}：{desc}")
                else:
                    lines.append(key)

        if lines:
            return "\n".join(lines)
        return ", ".join(categories)

    @classmethod
    def normalize_known_facts(cls, src: Any = None) -> dict[str, str]:
        """把 dict / LlmMemeHints 收敛成 {字段名: 已知文本}，只保留非空值。"""
        if not src:
            return {}
        is_mapping = isinstance(src, dict)
        out: dict[str, str] = {}
        for key, _label in cls._KNOWN_FACT_LABELS:
            value = src.get(key) if is_mapping else getattr(src, key, None)
            if value is None:
                continue
            if not isinstance(value, str):
                value = str(value)
            value = " ".join(value.split())
            if not value:
                continue
            if len(value) > cls._KNOWN_FACT_MAX_LEN:
                value = value[: cls._KNOWN_FACT_MAX_LEN]
            out[key] = value
        return out

    @classmethod
    def render_known_facts_block(cls, known: Any = None) -> str:
        """把已知信息渲染成提示词片段；没有已知信息时返回空串。"""
        facts = cls.normalize_known_facts(known)
        if not facts:
            return ""
        lines = [f"- {label}：{facts[key]}" for key, label in cls._KNOWN_FACT_LABELS if facts.get(key)]
        tail = ["description 与 scenes 要把上面给出的信息用进去，不要另起一套说法。"]
        if facts.get("overlay_text"):
            tail.append("overlay_text 直接沿用上面给出的图上文字。")
        tail.append("tags 只补充上面没提到的画面关键词。")
        return (
            "<known_facts>\n"
            "以下信息由用户或上层对话模型提供，已确认可信，请直接采用，不要推翻或改写：\n"
            + "\n".join(lines)
            + "\n"
            + "".join(tail)
            + "\n</known_facts>"
        )

    @classmethod
    def known_facts_signature(cls, known: Any = None) -> str:
        """已知信息的稳定签名，用于把带 hints 的分析结果和不带 hints 的区分开缓存。"""
        facts = cls.normalize_known_facts(known)
        if not facts:
            return ""
        return "|".join(
            f"{key}={facts[key]}" for key, _label in cls._KNOWN_FACT_LABELS if facts.get(key)
        )

    @classmethod
    def _inject_known_facts(cls, template: str, known_block: str) -> str:
        """把已知信息片段放进模板。

        模板里写了 {known_facts} 就替换该占位符；用户用自定义提示词覆盖、
        导致占位符缺失时，退化为插到 <output_format> 之前，最差也追加到末尾，
        保证这项能力不会因为自定义提示词而静默失效。
        """
        if not template:
            return known_block or template
        if cls._KNOWN_FACTS_PLACEHOLDER in template:
            out = template.replace(cls._KNOWN_FACTS_PLACEHOLDER, known_block)
            return out if known_block else _BLANK_LINES_RE.sub("\n\n", out)
        if not known_block:
            return template
        idx = template.find(cls._KNOWN_FACTS_ANCHOR)
        if idx > 0:
            head = template[:idx].rstrip("\n")
            return f"{head}\n\n{known_block}\n\n{template[idx:]}"
        return template.rstrip() + "\n\n" + known_block

    @staticmethod
    def _render_prompt_template(template: str, emotion_list: str) -> str:
        """仅替换 emotion_list 占位符，保留 JSON 花括号原样输出。"""
        if not template:
            return emotion_list
        return template.replace(PromptManager._PROMPT_PLACEHOLDER, emotion_list)
