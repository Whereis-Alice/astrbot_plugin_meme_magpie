"""表情包发送决策引擎：负责 LLM 响应拦截、自动发送决策和表情包发送。"""

import asyncio
import random
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..processing.natural_emotion_analyzer import EmotionQuery
from ..util.command_hint import command_like_pattern
from .event_context import get_event_session_key


class _MemeTurnState:
    """封装单次会话中的表情包发送状态。"""

    def __init__(self) -> None:
        self._active_sent = False
        self._candidates: list[dict] = []
        self._auto_decided = False
        self._auto_allowed = False
        self._auto_reason = ""
        self._auto_send_meme_claimed = False

    def mark_active_sent(self) -> None:
        """标记当前回合已主动发送过表情包。"""
        self._active_sent = True
        if hasattr(self, "_event"):
            self._event.set_extra("magpie_active_sent", True)

    def is_active_sent(self) -> bool:
        """检查当前回合是否已主动发送过表情包。"""
        return self._active_sent

    def set_candidates(self, candidates: list[dict]) -> None:
        """设置候选列表。"""
        self._candidates = candidates

    def get_candidates(self) -> list[dict]:
        """获取候选列表。"""
        return self._candidates

    def is_auto_decided(self) -> bool:
        """检查是否已做出自动决策。"""
        return self._auto_decided

    def set_auto_decision(self, allowed: bool, reason: str = "") -> None:
        """设置自动决策结果。"""
        self._auto_decided = True
        self._auto_allowed = allowed
        self._auto_reason = reason

    def get_auto_allowed(self) -> bool:
        """获取自动决策是否允许。"""
        return self._auto_allowed

    def get_auto_reason(self) -> str:
        """获取自动决策原因。"""
        return self._auto_reason

    def claim_auto_send_meme(self) -> bool:
        """尝试占用自动发送权限。"""
        if self._auto_decided and self._auto_allowed and not self._auto_send_meme_claimed:
            self._auto_send_meme_claimed = True
            return True
        return False

    def is_auto_claimed(self) -> bool:
        """检查是否已占用自动发送权限。"""
        return self._auto_send_meme_claimed

    def reset_for_new_turn(self) -> None:
        """重置回合状态，为新的一轮对话做准备。"""
        self._active_sent = False
        self._candidates = []
        self._auto_decided = False
        self._auto_allowed = False
        self._auto_reason = ""
        self._auto_send_meme_claimed = False


class MemeSenderEngine:
    """负责表情包自动发送决策和响应处理。"""

    AUTO_EMOJI_COOLDOWN_SECONDS = 20  # 同一会话自动发表情的最短间隔

    def __init__(self, plugin_instance: Any) -> None:
        self.plugin = plugin_instance
        self._auto_emoji_cooldowns: dict[str, float] = {}
        self._auto_emoji_cooldowns_max = 1000  # 最大条目数，防止内存泄漏
        self._auto_emoji_cooldowns_lock = asyncio.Lock()
        self._pending_auto_emoji_tasks: dict[str, asyncio.Task] = {}

    # --- 状态管理 ---

    def emoji_turn_state(self, event: AstrMessageEvent) -> _MemeTurnState:
        """获取或创建当前会话的 EmojiTurnState。"""
        key = self.get_auto_emoji_session_key(event)
        if not hasattr(event, "_emoji_turn_state"):
            event._emoji_turn_state = {}  # type: ignore[attr-defined]
        turn_states = event._emoji_turn_state  # type: ignore[attr-defined]
        if key not in turn_states:
            turn_states[key] = _MemeTurnState()
            turn_states[key]._event = event
        return turn_states[key]

    def get_auto_emoji_session_key(self, event: AstrMessageEvent) -> str:
        """获取自动表情会话键。"""
        return get_event_session_key(event)

    def reset_turn_state(self, event: AstrMessageEvent) -> None:
        """重置表情包回合状态及事件 extras，为新的一轮对话做准备。"""
        turn_state = self.emoji_turn_state(event)
        turn_state.reset_for_new_turn()
        for key in (
            "magpie_active_sent",
            "magpie_auto_emoji_turn_decided",
            "magpie_auto_emoji_turn_allowed",
            "magpie_auto_emoji_turn_claimed",
        ):
            try:
                event.set_extra(key, False)
            except Exception:
                pass

    def cancel_pending_auto_emoji(self, event: AstrMessageEvent, reason: str = "new_message") -> bool:
        """取消当前会话尚未发出的自动表情任务。"""
        key = self.get_auto_emoji_session_key(event)
        task = self._pending_auto_emoji_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            logger.debug(f"[MemeSenderEngine] 取消待发送自动表情: session={key}, reason={reason}")
            return True
        return False

    def schedule_auto_emoji_task(
        self,
        event: AstrMessageEvent,
        task: asyncio.Task | None,
    ) -> asyncio.Task | None:
        """记录当前会话的自动表情任务，并替换掉旧任务。"""
        if task is None:
            return None
        key = self.get_auto_emoji_session_key(event)
        old_task = self._pending_auto_emoji_tasks.get(key)
        if old_task and old_task is not task and not old_task.done():
            old_task.cancel()
        self._pending_auto_emoji_tasks[key] = task

        def _clear(done_task: asyncio.Task) -> None:
            if self._pending_auto_emoji_tasks.get(key) is done_task:
                self._pending_auto_emoji_tasks.pop(key, None)

        task.add_done_callback(_clear)
        return task

    # --- 决策检查 ---

    def should_skip_auto_emoji_by_gate(self, text: str) -> bool:
        """根据文本内容判断是否跳过自动发送。"""
        if not text:
            return True
        if not getattr(self.plugin, "auto_meme_intent_gate", True):
            return False
        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) <= 2:
            return True
        # 如果包含明确的指令或标记，跳过自动发送
        wake_prefix = getattr(self.plugin, "wake_prefix", None)
        prefix = wake_prefix() if callable(wake_prefix) else "/"
        skip_patterns = [
            command_like_pattern(prefix),
            r"^\\/",
            r"^\s*[!！/#]",
        ]
        for pattern in skip_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        serious_keywords = (
            "抱歉",
            "对不起",
            "错误",
            "失败",
            "异常",
            "无法",
            "不能",
            "隐私",
            "安全",
            "违规",
            "风险",
            "请提供",
            "需要更多",
        )
        if any(keyword in normalized for keyword in serious_keywords):
            return True
        question_marks = normalized.count("?") + normalized.count("？")
        if question_marks >= 2:
            return True
        return False

    async def is_auto_emoji_cooldown_ready(self, event: AstrMessageEvent) -> bool:
        """检查自动表情冷却是否就绪。"""
        key = self.get_auto_emoji_session_key(event)
        now = asyncio.get_event_loop().time()
        async with self._auto_emoji_cooldowns_lock:
            last = self._auto_emoji_cooldowns.get(key, 0)
            return now - last >= self.AUTO_EMOJI_COOLDOWN_SECONDS

    def normalize_auto_meme_chance(self) -> float:
        """归一化自动表情发送概率。"""
        try:
            chance = float(getattr(self.plugin, "meme_chance", 0.4))
        except (TypeError, ValueError):
            chance = 0.4
        return max(0.0, min(1.0, chance))

    async def resolve_auto_emoji_turn_permission(self, event: AstrMessageEvent) -> bool:
        """解析自动表情发送权限。"""
        turn_state = self.emoji_turn_state(event)

        if turn_state.is_auto_decided():
            return turn_state.get_auto_allowed()

        def decide(allowed: bool, reason: str) -> bool:
            event.set_extra("magpie_auto_emoji_turn_decided", True)
            event.set_extra("magpie_auto_emoji_turn_allowed", allowed)
            event.set_extra("magpie_auto_emoji_turn_reason", reason)
            turn_state.set_auto_decision(allowed, reason)
            return allowed

        if not getattr(self.plugin, "auto_send_meme", False):
            return decide(False, "auto_send_meme_disabled")
        if not self.plugin.is_send_enabled_for_event(event):
            return decide(False, "send_disabled")
        if not await self.is_auto_emoji_cooldown_ready(event):
            return decide(False, "cooldown")
        chance = self.normalize_auto_meme_chance()
        if chance <= 0:
            return decide(False, "chance_zero")
        if chance >= 1:
            return decide(True, "chance_hit")
        if random.random() < chance:
            return decide(True, "chance_hit")
        return decide(False, "chance_miss")

    async def _resolve_with_log(self, event: AstrMessageEvent) -> bool:
        """带日志的权限判断包装，方便排查发送概率问题。"""
        allowed = await self.resolve_auto_emoji_turn_permission(event)
        reason = event.get_extra("magpie_auto_emoji_turn_reason") or "unknown"
        logger.debug(f"[MemeThief] 当前轮次自动发表情判定: allowed={allowed}, reason={reason}")
        return allowed

    def claim_auto_emoji_turn(self, event: AstrMessageEvent) -> bool:
        """尝试占用当前回合的表情包发送权。"""
        turn_state = self.emoji_turn_state(event)
        if event.get_extra("magpie_auto_emoji_turn_claimed"):
            return False
        if turn_state.is_active_sent():
            return False
        if not turn_state.claim_auto_send_meme():
            return False
        event.set_extra("magpie_auto_emoji_turn_claimed", True)
        return True

    def prune_auto_emoji_cooldowns(self, now: float) -> None:
        """清理过期的自动表情冷却记录。"""
        cutoff = now - self.AUTO_EMOJI_COOLDOWN_SECONDS * 2
        expired = [k for k, v in self._auto_emoji_cooldowns.items() if v < cutoff]
        for k in expired:
            del self._auto_emoji_cooldowns[k]

    async def mark_auto_emoji_sent(self, event: AstrMessageEvent) -> None:
        """标记已发送自动表情。"""
        key = self.get_auto_emoji_session_key(event)
        now = asyncio.get_event_loop().time()
        async with self._auto_emoji_cooldowns_lock:
            self.prune_auto_emoji_cooldowns(now)
            self._auto_emoji_cooldowns[key] = now

    # --- 发送 ---

    async def try_send_emoji(
        self, event: AstrMessageEvent, emotions: list[str], cleaned_text: str
    ) -> bool:
        """尝试发送表情包（委托给 MemeSelector）。"""
        try:
            selector = getattr(self.plugin, "meme_selector", None)
            if selector is None:
                return False
            return await selector.try_send_emoji(event, emotions, cleaned_text)
        except Exception as e:
            logger.warning(f"[MemeSenderEngine] 尝试发送表情包失败: {e}")
            return False

    def get_meme_send_delay(self, text: str = "", task_start: float = 0.0) -> float:
        """获取表情包发送延迟（秒）。"""
        char_delay = getattr(self.plugin, "meme_send_char_delay", 0.0)
        if char_delay > 0 and text:
            desired = len(text) * char_delay
            if task_start > 0:
                elapsed = asyncio.get_event_loop().time() - task_start
                return max(0.0, desired - elapsed)
            return desired
        delay = getattr(self.plugin, "meme_send_delay", 0.5)
        try:
            base = float(delay)
        except (TypeError, ValueError):
            base = 0.5
        # meme_send_delay_random (bool) 开启时在 [base, base+max] 之间随机
        use_random = bool(getattr(self.plugin, "meme_send_delay_random", False))
        if use_random:
            delay_max = getattr(self.plugin, "meme_send_delay_max", 8.0)
            try:
                rand_max = float(delay_max)
            except (TypeError, ValueError):
                rand_max = 8.0
            if rand_max > 0:
                return base + random.random() * rand_max
        return base

    async def resolve_emoji_query(
        self,
        event: AstrMessageEvent,
        text: str,
        emotions: list[str],
        *,
        user_message: str = "",
    ) -> tuple[list[str], str]:
        """解析出用于检索表情的情绪先验和检索词。

        开启「智能提取检索词」时交给轻量模型提炼，否则直接用回复原文。
        返回 (情绪先验, 检索词)。是否真的发送由调用方的门控决定。
        """
        final_emotions = list(emotions or [])
        search_text = text
        if getattr(self.plugin, "enable_natural_emotion_analysis", False) and hasattr(
            self.plugin, "smart_emotion_matcher"
        ):
            analyzed = await self.plugin.smart_emotion_matcher.analyze_and_match_emotion(
                event,
                text,
                use_natural_analysis=True,
                user_message=user_message,
            )
            if isinstance(analyzed, EmotionQuery):
                # 是否发送仍由概率/冷却/意图门控决定，小模型只提供检索词和情绪先验。
                if analyzed.emotion_priors:
                    final_emotions = analyzed.emotion_priors
                if analyzed.search_query:
                    search_text = analyzed.search_query
            elif analyzed:
                final_emotions = [analyzed]
        return final_emotions, search_text

    def attach_timeout(self) -> float:
        """attach 模式等待选图的秒数上限，非法值退回默认 10 秒。"""
        try:
            value = float(getattr(self.plugin, "auto_meme_attach_timeout", 10.0))
        except (TypeError, ValueError):
            return 10.0
        return value if value > 0 else 10.0

    async def attach_emoji_to_result(
        self,
        event: AstrMessageEvent,
        result: Any,
        text: str,
        emotions: list[str],
        *,
        user_message: str = "",
    ) -> bool:
        """attach 投递模式：把表情追加到本条回复的消息链里。

        必须在 on_decorating_result 阶段内同步等出结果——hook 一返回，AstrBot
        就会把消息链交给发送阶段，之后再改动就来不及了。因此这里带超时，
        超时或失败都返回 False，由调用方回退到默认的独立消息模式。
        """
        selector = getattr(self.plugin, "meme_selector", None)
        if selector is None or not hasattr(selector, "attach_emoji_to_result"):
            return False
        try:
            final_emotions, search_text = await asyncio.wait_for(
                self.resolve_emoji_query(event, text, emotions, user_message=user_message),
                timeout=self.attach_timeout(),
            )
            if not final_emotions and not search_text:
                return False
            attached = await asyncio.wait_for(
                selector.attach_emoji_to_result(
                    event, result, final_emotions, search_text or text
                ),
                timeout=self.attach_timeout(),
            )
        except asyncio.TimeoutError:
            logger.debug(
                f"[MemeSenderEngine] attach 模式选图超过 {self.attach_timeout():.1f}s，"
                "改用独立消息补发"
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[MemeSenderEngine] attach 模式附加表情失败: {e}")
            return False

        if not attached:
            return False

        try:
            selector_obj = getattr(self.plugin, "meme_selector", None)
            if selector_obj is not None:
                await selector_obj.record_emoji_usage(attached, trigger="auto")
        except Exception as e:
            logger.debug(f"[MemeSenderEngine] 记录表情包使用失败: {e}")
        await self.mark_auto_emoji_sent(event)
        self.emoji_turn_state(event).mark_active_sent()
        logger.debug(f"[MemeSenderEngine] 已把表情附加到回复消息链: {attached}")
        return True

    @staticmethod
    def _is_streaming_result(result: Any) -> bool:
        """这条结果是否属于流式输出（推流中或流式收尾）。"""
        content_type = getattr(result, "result_content_type", None)
        name = str(getattr(content_type, "name", "") or content_type or "")
        return "STREAMING" in name.upper()

    async def async_analyze_and_send_emoji(
        self,
        event: AstrMessageEvent,
        text: str,
        emotions: list[str],
        *,
        user_message: str = "",
    ):
        """异步分析并发送表情包。

        调用方 _prepare_emoji_response 已通过 claim_auto_emoji_turn
        占用发送权（_auto_send_meme_claimed），防止重复创建任务。
        本方法成功发送后再标记 mark_active_sent 以记录实际发送时间。
        """
        try:
            task_start = asyncio.get_event_loop().time()
            final_emotions, search_text = await self.resolve_emoji_query(
                event, text, emotions, user_message=user_message
            )

            if not final_emotions and not search_text:
                return

            result = event.get_result()
            if (
                result
                and not result.get_plain_text().strip()
                and not self._is_streaming_result(result)
            ):
                # 流式结果的 chain 在推流阶段本来就是空的，不能当成「回复被撤了」
                logger.debug("[MemeSenderEngine] 主回复已被置空，跳过自动表情")
                return

            delay = self.get_meme_send_delay(text, task_start)
            if delay > 0:
                await asyncio.sleep(delay)

            sent = await self.try_send_emoji(event, final_emotions, search_text or text)
            if sent:
                await self.mark_auto_emoji_sent(event)
                self.emoji_turn_state(event).mark_active_sent()
        except asyncio.CancelledError:
            logger.debug("[MemeSenderEngine] 自动表情任务已取消")
            raise
        except Exception as e:
            logger.warning(f"[MemeSenderEngine] 异步分析发送表情包失败: {e}")
