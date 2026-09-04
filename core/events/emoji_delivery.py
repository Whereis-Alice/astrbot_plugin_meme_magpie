"""平台相关的表情包投递优化。"""

import os
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image


async def send_qq_image_as_sticker(
    event: AstrMessageEvent,
    file_path: str,
    summary: str = "[动画表情]",
    plugin: Any = None,
) -> bool:
    """在 QQ (aiocqhttp) 平台把图片按「表情」发出去。

    除了改 summary 外显，还会把 sub_type / subType 一起标成 1，这样 LLBot、
    NapCat、SnowLuma 等实现都会把它当表情而不是普通图片渲染。
    """
    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )
    except ImportError:
        return False
    if not isinstance(event, AiocqhttpMessageEvent):
        return False
    if not file_path or not os.path.exists(file_path):
        return False

    # 这里使用适配器内部接口做 QQ 专属优化；任何失败都交给调用方的标准
    # event.send 路径回退，避免平台版本变化中断回复。
    try:
        file_source: str = file_path
        if plugin and getattr(plugin, "send_meme_as_gif", False):
            image_processor = getattr(plugin, "image_processor_service", None)
            if image_processor:
                encoded = await image_processor._file_to_gif_base64(file_path)
                if encoded:
                    file_source = f"base64://{encoded}"
        chain = MessageChain(chain=[Image(file=file_source)])
        onebot_message = await event._parse_onebot_json(chain)
        data = onebot_message[0]["data"]
        # 各家 OneBot 实现读的字段不一样：LLBot 只认 camelCase 的 subType，
        # NapCat / SnowLuma 只认 snake_case 的 sub_type，summary 则是外显文案。
        # 三个键一起写，谁都能识别成“表情”，多余的键各家都会忽略。
        data["summary"] = summary
        data["sub_type"] = 1
        data["subType"] = 1
        await event.bot.send(event.message_obj.raw_message, onebot_message)
        return True
    except Exception:
        return False
