"""LLM 主动偷图时自带的结构化标注。

背景：轻量 VLM 的“这是什么作品的哪个角色”识别准确率很低，但主对话 LLM
往往已经从上下文里知道群里在聊哪部番、谁发的表情。让 LLM 在调用偷图工具时
顺手把这些信息当参数传过来，比事后再跑一次视觉分析更准也更省 token。

这个模块只做纯数据清洗：把 LLM 传来的自由文本参数归一成可入库的字段，
不涉及 IO、不依赖插件实例，便于单测。

llm_tool 的 JSON schema 由 AstrBot 从 docstring 的 ``Args:`` 自动推导，且不会生成
``required``，所以所有参数天然可选。为了让 provider 兼容性最大化，工具参数全部用
``string``（多值用逗号 / 顿号分隔），而不用 array。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..util.normalization import normalize_label_list
from .semantic_schema import (
    MAX_DESC_CHARS,
    MAX_EMOTIONS,
    MAX_OVERLAY_CHARS,
    MAX_SCENES,
    MAX_TAGS,
    clip_chars,
)

# LLM 可能传“无/不知道/unknown”之类的占位字符，当空处理，避免写脏数据。
_NULL_TOKENS = frozenset(
    {
        "",
        "-",
        "--",
        "n/a",
        "na",
        "null",
        "none",
        "nil",
        "unknown",
        "not sure",
        "无",
        "没有",
        "不知道",
        "不确定",
        "未知",
        "无法识别",
        "不清楚",
        "略",
    }
)

_MAX_NAME_CHARS = 40


def clean_text(value: Any) -> str:
    """去除首尾空白与占位词，返回干净文本或空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    # 常见的包裹引号
    text = text.strip("\"'“”‘’「」『』 ")
    if text.lower() in _NULL_TOKENS:
        return ""
    return text


def _clean_name(value: Any) -> str:
    """角色名 / 作品名：单值、限长。LLM 偶尔会丢一整句话过来。"""
    text = clean_text(value)
    if not text:
        return ""
    # 如果 LLM 传了多个（“A、B”），只取第一个，余下的会被当标签回收。
    parts = normalize_label_list(text)
    if parts:
        text = parts[0]
    return clip_chars(text, _MAX_NAME_CHARS)


@dataclass(slots=True)
class LlmMemeHints:
    """LLM 偷图时自带的标注（已归一）。"""

    category: str = ""
    character: str = ""
    work: str = ""
    action: str = ""
    overlay_text: str = ""
    desc: str = ""
    tags: list[str] = field(default_factory=list)
    scenes: list[str] = field(default_factory=list)
    emotions: list[str] = field(default_factory=list)
    # 原始 category 参数传了但归一失败时保留，用于提示用户。
    rejected_category: str = ""
    # desc 是 LLM 显式传的，还是 _derive() 拼出来的。
    # 显式传的优先级高于 VLM；拼出来的只当兜底。
    desc_explicit: bool = False

    @classmethod
    def from_tool_args(
        cls,
        plugin_config: Any = None,
        *,
        emotion: Any = "",
        character: Any = "",
        work: Any = "",
        action: Any = "",
        overlay_text: Any = "",
        tags: Any = "",
        desc: Any = "",
        scenes: Any = "",
    ) -> LlmMemeHints:
        """把 llm_tool 的原始字符串参数归一成 :class:`LlmMemeHints`。

        Args:
            plugin_config: 用于 ``normalize_category_strict``；为 None 时只做小写归一。
        """
        raw_emotion = clean_text(emotion)
        category = ""
        rejected = ""
        if raw_emotion:
            if plugin_config is not None and hasattr(plugin_config, "normalize_category_strict"):
                category = plugin_config.normalize_category_strict(raw_emotion) or ""
            else:
                category = raw_emotion.lower()
            if not category:
                rejected = raw_emotion

        hints = cls(
            category=category,
            character=_clean_name(character),
            work=_clean_name(work),
            action=clip_chars(clean_text(action), _MAX_NAME_CHARS),
            overlay_text=clip_chars(clean_text(overlay_text), MAX_OVERLAY_CHARS),
            desc=clip_chars(clean_text(desc), MAX_DESC_CHARS),
            tags=normalize_label_list(clean_text(tags), MAX_TAGS),
            scenes=normalize_label_list(clean_text(scenes), MAX_SCENES),
            rejected_category=rejected,
            desc_explicit=bool(clean_text(desc)),
        )
        hints._derive()
        return hints

    def _derive(self) -> None:
        """用已有字段补全标签 / 描述 / 情绪，让检索有东西可抄。"""
        # 作品名、角色名、动作都是很好的检索词，补进标签（不超上限）。
        candidates = [self.character, self.work, self.action]
        merged = list(self.tags)
        for item in candidates:
            if item and item not in merged:
                merged.append(item)
        self.tags = normalize_label_list(merged, MAX_TAGS)

        if not self.emotions and self.category:
            self.emotions = normalize_label_list([self.category], MAX_EMOTIONS)

        if not self.desc:
            self.desc = clip_chars(self.describe(), MAX_DESC_CHARS)

    def describe(self) -> str:
        """拼一句自然描述，例：「《咒术回战》五条悟捉脸」。"""
        bits: list[str] = []
        if self.work:
            bits.append(f"《{self.work}》")
        if self.character:
            bits.append(self.character)
        if self.action:
            bits.append(self.action)
        if not bits and self.overlay_text:
            return self.overlay_text
        return "".join(bits)

    @property
    def has_semantics(self) -> bool:
        """是否带了任何可用信息。"""
        return bool(
            self.category
            or self.character
            or self.work
            or self.action
            or self.overlay_text
            or self.tags
            or self.scenes
            or self.desc
        )

    @property
    def is_complete(self) -> bool:
        """信息足够到可以直接入库、不必再跑 VLM。

        分类是硬需求（目录与发送都靠它）；另外至少要有一个语义字段，
        否则入库后检索没有任何抄得上的文本。
        """
        return bool(
            self.category
            and (self.character or self.work or self.overlay_text or self.tags or self.action)
        )

    def to_extra_meta(self) -> dict[str, Any]:
        """转成可直接合入 entry / pending meta 的字段。"""
        meta: dict[str, Any] = {}
        if self.character:
            meta["character"] = self.character
        if self.work:
            meta["work"] = self.work
        if self.overlay_text:
            meta["overlay_text"] = self.overlay_text
        if self.emotions:
            meta["emotions"] = list(self.emotions)
        return meta

    def merge_tags(self, other: Any) -> list[str]:
        """LLM 标签优先，后接 VLM 标签，去重后截到上限。"""
        return normalize_label_list([*self.tags, *normalize_label_list(other)], MAX_TAGS)

    def merge_scenes(self, other: Any) -> list[str]:
        return normalize_label_list([*self.scenes, *normalize_label_list(other)], MAX_SCENES)

    def merge_emotions(self, other: Any) -> list[str]:
        return normalize_label_list([*self.emotions, *normalize_label_list(other)], MAX_EMOTIONS)

    def resolve_desc(self, vlm_desc: Any = "") -> str:
        """描述优先级：LLM 显式传入 > VLM 分析 > 由作品/角色/动作拼出的兜底。

        拼出来的描述（如「《作品》角色动作」）信息量已经被 character/work
        字段覆盖，所以比 VLM 的自然语言描述优先级低。
        """
        if self.desc_explicit and self.desc:
            return self.desc
        cleaned = clean_text(vlm_desc)
        if cleaned:
            return clip_chars(cleaned, MAX_DESC_CHARS)
        return self.desc

    def resolve_overlay_text(self, vlm_overlay: Any = "") -> str:
        """图上文字：LLM 显式传入优先（它能看到原图上下文），否则用 VLM OCR 结果。"""
        if self.overlay_text:
            return self.overlay_text
        return clip_chars(clean_text(vlm_overlay), MAX_OVERLAY_CHARS)

    def provided_fields(self) -> list[str]:
        """返回 LLM 实际提供了哪些字段（中文名），用于回复里标注数据来源。"""
        names: list[str] = []
        if self.category:
            names.append("分类")
        if self.work:
            names.append("作品")
        if self.character:
            names.append("角色")
        if self.action:
            names.append("动作")
        if self.overlay_text:
            names.append("图上文字")
        if self.tags:
            names.append("标签")
        if self.scenes:
            names.append("场景")
        return names
