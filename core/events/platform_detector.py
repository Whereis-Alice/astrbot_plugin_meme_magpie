"""平台检测器：负责平台识别、元信息提取和 URL 解析。"""

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .event_context import get_event_platform_name, normalize_event_value

# QQ 官方商城表情 CDN 特征（用于 qq_official 平台的表情判定）
_QQ_EMOJI_URL_MARKERS = (
    "vip.qq.com/club/item/parcel",
    "gxh.vip.qq.com",
)

# OneBot 里承载「QQ 商城表情」的独立消息段类型。
# LLBot 会把商城表情单独发成 mface 段（没有 file 字段），
# NapCat / SnowLuma 则倾向折进普通 image 段，两条路径都要覆盖。
_STORE_EMOJI_SEG_TYPES = frozenset({"marketface", "mface"})

# 商城表情段里可能出现的图片 URL 字段，按“越靠前越可能是原图”排序。
_STORE_EMOJI_URL_KEYS = (
    "url",
    "cdnurl",
    "cdn_url",
    "raw_url",
    "origin_url",
    "original_url",
    "thumb",
    "thumb_url",
)


class PlatformDetector:
    """负责检测消息来源平台并提取表情包相关的元信息。"""

    def __init__(self, plugin_instance: Any = None) -> None:
        self.plugin = plugin_instance

    @staticmethod
    def _normalize_str(value: object) -> str:
        """规范化字符串值。"""
        return normalize_event_value(value)

    @staticmethod
    def iter_onebot_segments(event: AstrMessageEvent | None) -> list[dict]:
        """取出 OneBot 原始消息段列表，取不到时返回空列表。

        raw_message 在不同适配器下形态不一：aiocqhttp 给的是 dict 子类（既能下标
        也能属性访问），别的实现可能给纯 dict 或普通对象；OneBot 侧若配置
        messageFormat="string"，message 还会是一段 CQ 码字符串。这里统一兜住，
        避免调用方各写一遍。
        """
        raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
        raw_message: object = None
        if isinstance(raw_event, dict):
            # dict 分支要放在前面：aiocqhttp 的 Event 同时是 dict 且带 __getattr__，
            # 而纯 dict 用 getattr 取不到 message。
            raw_message = raw_event.get("message")
        elif raw_event is not None:
            raw_message = getattr(raw_event, "message", None)
        if not isinstance(raw_message, list):
            return []
        return [seg for seg in raw_message if isinstance(seg, dict)]

    @staticmethod
    def _seg_type(seg: dict) -> str:
        """取消息段类型（小写）。"""
        return str(seg.get("type", "") or "").strip().lower()

    def get_platform_name(self, event: AstrMessageEvent | None = None) -> str:
        """获取事件平台名（小写），失败时返回空字符串。"""
        return get_event_platform_name(event)

    def is_telegram_event(self, event: AstrMessageEvent | None = None) -> bool:
        """判断事件是否来自 Telegram 平台。"""
        platform_name = self.get_platform_name(event)
        return platform_name == "telegram"

    def is_qqofficial_event(self, event: AstrMessageEvent | None = None) -> bool:
        """判断事件是否来自 QQ 官方平台（qq_official）。

        QQ 官方（官方机器人 API）的 raw_message 是 botpy SDK 对象，不是
        OneBot 的 dict 段列表，消息组件链与 NapCat/OneBot 差异较大。
        """
        platform_name = self.get_platform_name(event)
        return platform_name == "qq_official"

    @staticmethod
    def _is_qq_emoji_url(url: str) -> bool:
        """判断图片 URL 是否带有 QQ 官方表情 CDN 特征。"""
        u = str(url or "").lower()
        return any(marker in u for marker in _QQ_EMOJI_URL_MARKERS)

    def check_platform_emoji_metadata(
        self,
        img: object,
        event: AstrMessageEvent | None = None,
        img_index: int | None = None,
        image_segments: list[dict] | None = None,
        image_file_map: dict[str, dict] | None = None,
    ) -> bool:
        """检查图片元信息，判断是否为平台标记的表情包。

        支持的平台特征：
        - NapCat/OneBot: subType=1 或 sub_type=1 表示表情包
        - QQ: summary包含"表情"关键词

        Args:
            img: 图片组件
            event: 消息事件对象（可选），用于访问原始消息数据

        Returns:
            bool: 是否为平台标记的表情包
        """
        try:
            # QQ 官方（botpy）兼容：raw_message 是 SDK 对象而非 OneBot dict 段列表，
            # 无法提取 sub_type/summary 等段标记，改为按图片 URL 特征 / 配置判定。
            if self.is_qqofficial_event(event):
                img_ref = self._normalize_str(getattr(img, "file", "")) or self._normalize_str(
                    getattr(img, "url", "")
                )
                try:
                    mode = str(
                        getattr(self.plugin.plugin_config, "qqofficial_steal_mode", "cdn_only")
                        or "cdn_only"
                    )
                except Exception:
                    mode = "cdn_only"
                if mode == "gif_only":
                    # 仅收录 GIF 格式（基于 URL 后缀的尽力过滤），命中即收
                    url_path = str(img_ref or "").lower().split("?", 1)[0]
                    if url_path.endswith(".gif"):
                        logger.debug("QQ_Official gif_only 模式：收录 GIF 图片")
                        return True
                    logger.debug("QQ_Official gif_only 模式：跳过非 GIF 图片")
                    return False
                if mode == "all_images":
                    if img_ref:
                        logger.debug("QQ_Official：已开启『所有图片按表情收录』，收录该图片")
                        return True
                    return False
                # cdn_only（默认）：仅收录带表情 CDN 特征的图片
                if self._is_qq_emoji_url(img_ref):
                    logger.debug(f"检测到 QQ_Official 表情包（CDN 特征）: {img_ref[:80]}")
                    return True
                return False

            def is_emoji_summary(summary: object) -> bool:
                s = self._normalize_str(summary)
                if not s:
                    return False
                s_lower = s.lower()
                return "表情" in s or "emoji" in s_lower or "sticker" in s_lower

            def is_sub_type_emoji(sub_type: object) -> bool:
                if sub_type is None:
                    return False
                if sub_type == 1 or sub_type == "1":
                    return True
                try:
                    return int(sub_type) == 1
                except Exception:
                    return False

            # Telegram 兼容：优先识别 sticker，再兜底 .webp 贴纸文件
            if self.is_telegram_event(event):
                try:
                    raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
                    tg_message = getattr(raw_event, "message", None)
                    tg_sticker = getattr(tg_message, "sticker", None) if tg_message else None
                    if tg_sticker is not None:
                        is_animated = bool(getattr(tg_sticker, "is_animated", False))
                        is_video = bool(getattr(tg_sticker, "is_video", False))
                        if is_animated or is_video:
                            logger.debug("检测到 Telegram 动态贴纸，当前跳过收录")
                            return False
                        logger.debug("检测到 Telegram 静态贴纸")
                        return True
                except Exception:
                    pass

                img_file = self._normalize_str(getattr(img, "file", "")).lower()
                img_url = self._normalize_str(getattr(img, "url", "")).lower()
                if img_file.endswith(".webp") or img_url.endswith(".webp"):
                    logger.debug("检测到 Telegram WebP 贴纸文件")
                    return True

            # 方式0: 从原始事件中查找 sub_type (最可靠的方法)
            if image_segments is None and event is not None:
                all_segments = self.iter_onebot_segments(event)
                image_segments = [
                    seg for seg in all_segments if self._seg_type(seg) == "image"
                ]
                logger.debug(
                    f"[EmojiCheck] 方式0: 原始段 {len(all_segments)} 个，其中图片段 {len(image_segments)} 个"
                )

            if image_segments:
                logger.debug(f"[EmojiCheck] 有 {len(image_segments)} 个 image_segments 可匹配, img_index={img_index}")
                matched_data: dict[str, object] | None = None

                if (
                    img_index is not None
                    and 0 <= img_index < len(image_segments)
                    and isinstance(image_segments[img_index], dict)
                ):
                    matched_data = image_segments[img_index].get("data", {}) or {}
                else:
                    img_file = self._normalize_str(getattr(img, "file", ""))
                    img_url = self._normalize_str(getattr(img, "url", ""))
                    img_file_unique = self._normalize_str(getattr(img, "file_unique", ""))

                    if image_file_map and img_file:
                        matched_data = image_file_map.get(img_file)
                        if matched_data is None and img_file_unique:
                            matched_data = image_file_map.get(img_file_unique)

                    if matched_data is None:
                        for seg in image_segments:
                            if not isinstance(seg, dict):
                                continue
                            data = seg.get("data", {}) or {}
                            if not isinstance(data, dict):
                                continue
                            seg_file = self._normalize_str(data.get("file", ""))
                            seg_url = self._normalize_str(data.get("url", ""))

                            if seg_file and (
                                seg_file == img_file
                                or (img_file_unique and seg_file == img_file_unique)
                                or (img_url and seg_file in img_url)
                                or (img_file and seg_file in img_file)
                            ):
                                matched_data = data
                                break

                            if seg_url and (
                                (img_url and seg_url == img_url)
                                or (img_file and seg_url in img_file)
                            ):
                                matched_data = data
                                break

                if matched_data is not None:
                    logger.debug(f"[EmojiCheck] matched_data keys: {list(matched_data.keys())}")
                    sub_type = matched_data.get("sub_type") or matched_data.get("subType")
                    if is_sub_type_emoji(sub_type):
                        logger.debug(f"检测到表情包标记: sub_type={sub_type} (从原始事件)")
                        return True

                    summary = matched_data.get("summary", "")
                    if is_emoji_summary(summary):
                        logger.debug(f"检测到表情包标记: summary='{summary}' (从原始事件)")
                        return True

                    # QQ 商城表情（把 mface 折进 image 段的实现会带这几个字段）。
                    # SnowLuma 这类实现给的 sub_type 是 0、summary 也未必带“表情”，
                    # 只能靠这些商城专属字段兜住，否则会被当成普通图片跳过。
                    if (
                        matched_data.get("emoji_id")
                        or matched_data.get("emoji_package_id")
                        or matched_data.get("emoji_pkg_id")
                        or matched_data.get("key")
                    ):
                        logger.debug("检测到表情包标记: QQ 商城表情字段 (从原始事件)")
                        return True

                    url = self._normalize_str(matched_data.get("url", ""))
                    if "vip.qq.com/club/item/parcel" in url or "gxh.vip.qq.com" in url:
                        logger.debug("检测到表情包标记: QQ 商城 CDN URL (从原始事件)")
                        return True

            # 方式1: 检查 Image 对象的 subType 字段
            if hasattr(img, "subType") and img.subType:
                if is_sub_type_emoji(img.subType):
                    logger.debug(f"检测到表情包标记: subType={img.subType}")
                    return True

            # 方式2: 检查 __dict__ 中的 sub_type
            if hasattr(img, "__dict__"):
                img_dict = img.__dict__
                sub_type_underscore = img_dict.get("sub_type")
                if is_sub_type_emoji(sub_type_underscore):
                    logger.debug(f"检测到表情包标记: sub_type={sub_type_underscore} (从__dict__)")
                    return True

            # 方式3: 通过 toDict() 检查
            try:
                raw_data = img.toDict()
                if isinstance(raw_data, dict) and "data" in raw_data:
                    data = raw_data["data"]

                    sub_type = data.get("sub_type") or data.get("subType")
                    if is_sub_type_emoji(sub_type):
                        logger.debug(f"检测到表情包标记: sub_type={sub_type} (从toDict)")
                        return True

                    summary = data.get("summary", "")
                    if is_emoji_summary(summary):
                        logger.debug(f"检测到表情包标记: summary='{summary}'")
                        return True

                    if data.get("emoji_id") or data.get("emoji_package_id"):
                        logger.debug("检测到表情包标记: emoji_id/emoji_package_id (从toDict)")
                        return True

                    img_type = data.get("type") or data.get("imageType") or data.get("image_type")
                    if img_type in ["emoji", "sticker", "face", "meme"]:
                        logger.debug(f"检测到表情包标记: type='{img_type}'")
                        return True
            except Exception as e:
                logger.debug(f"无法获取图片字典数据: {e}")

            return False

        except Exception as e:
            logger.debug(f"检查平台表情包元信息失败: {e}")
            return False

    def extract_store_emoji_segments(self, event: AstrMessageEvent) -> list[dict]:
        """从 OneBot 原始消息里提取 QQ 商城表情段（marketface / mface）。

        LLBot 会把商城表情发成独立的 mface 段，这类段没有 file 字段，AstrBot 也不会
        把它转成 Image 组件，所以只能直接读原始段。返回的每一项形如
        ``{"url": "https://...", "meta": {...}}``，meta 里带上商城表情自身的
        emoji_id / 表情包 id / 外显名，方便入库后回溯来源。
        """
        results: list[dict] = []
        seen: set[str] = set()
        try:
            for seg in self.iter_onebot_segments(event):
                if self._seg_type(seg) not in _STORE_EMOJI_SEG_TYPES:
                    continue
                data = seg.get("data", {}) or {}
                if not isinstance(data, dict):
                    continue

                urls: list[str] = []

                def push(value: object) -> None:
                    s = self._normalize_str(value)
                    if s.startswith("http://") or s.startswith("https://"):
                        if s not in urls:
                            urls.append(s)

                for key in _STORE_EMOJI_URL_KEYS:
                    push(data.get(key))
                # 字段名各家不统一，兜底再扫一遍所有字符串值
                if not urls:
                    for value in data.values():
                        push(value)
                if not urls:
                    continue

                meta = {
                    "source": "qq_store",
                    "qq_emoji_id": self._normalize_str(
                        data.get("emoji_id") or data.get("id")
                    ),
                    "qq_emoji_package_id": self._normalize_str(
                        data.get("emoji_package_id")
                        or data.get("emoji_pkg_id")
                        or data.get("package_id")
                        or data.get("tabId")
                    ),
                    "qq_key": self._normalize_str(data.get("key")),
                    "store_summary": self._normalize_str(
                        data.get("summary") or data.get("text")
                    ),
                }
                for url in urls:
                    if url in seen:
                        continue
                    seen.add(url)
                    results.append({"url": url, "meta": dict(meta, origin_url=url)})
        except Exception as e:
            logger.debug(f"提取商城表情段失败: {e}")
        return results

    def extract_store_emoji_urls(self, event: AstrMessageEvent) -> list[str]:
        """从 OneBot raw_message 里提取 QQ 商城表情（marketface/mface）的可下载 URL。"""
        return [item["url"] for item in self.extract_store_emoji_segments(event)]
