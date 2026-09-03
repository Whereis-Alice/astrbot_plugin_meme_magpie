"""批量视觉分析限流器：并发上限 + RPM 令牌桶 + 退避重试。

上游的视觉 / 嵌入模型几乎都有 RPM 或并发上限，一次性把几百张表情包丢过去
必然撞 429。这个模块把「一次分析调用」包成受控调用：

* **并发上限**：同时在飞的请求数不超过 ``concurrency``。
* **RPM 节流**：所有调用串行地预约「发车时刻」，保证平均速率不超过 ``rpm``。
* **退避重试**：识别 429 / 5xx / 超时这类临时错误，指数退避后重试，并把整条
  队列的发车时刻一起推后，避免几个 worker 前后脚继续撞墙。

纯 asyncio 实现，不依赖插件实例，方便单测与复用（批量导入、后台偷图队列等）。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from astrbot.api import logger

T = TypeVar("T")

# 上游建议的 Retry-After 上限，防止某些实现返回超大值把任务挂死。
MAX_RETRY_AFTER_SECONDS = 120.0

# 这些 HTTP 状态码属于「等一等还有戏」。
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})
# 这些状态码重试多少次都是同样的结果，直接失败更省时间和额度。
_FATAL_STATUSES = frozenset({400, 401, 402, 403, 404, 405, 413, 415, 422})

# 没有结构化状态码时的兜底：按异常文本判断。先查致命再查可重试。
_FATAL_MARKERS: tuple[str, ...] = (
    "insufficient_quota",
    "insufficient quota",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "unauthorized",
    "authentication",
    "permission denied",
    "billing",
    "arrearage",
    "余额不足",
    "欠费",
)
_RETRYABLE_MARKERS: tuple[str, ...] = (
    "429",
    "too many requests",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "throttl",
    "overloaded",
    "server busy",
    "server_busy",
    "service unavailable",
    "temporarily unavailable",
    "try again later",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "请求过于频繁",
    "请求频率",
    "系统繁忙",
    "服务繁忙",
    "稍后重试",
)


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, result))


def _clamp_float(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, result))


def _extract_status(exc: BaseException) -> int | None:
    """尽量从各家 SDK 的异常对象里挖出 HTTP 状态码。"""
    for attr in ("status_code", "status", "http_status", "code"):
        raw = getattr(exc, attr, None)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw.strip())
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            raw = getattr(response, attr, None)
            if isinstance(raw, int) and not isinstance(raw, bool):
                return raw
    return None


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否属于「上游临时性错误」，退避后重试有意义。"""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status = _extract_status(exc)
    if status is not None:
        if status in _FATAL_STATUSES:
            return False
        if status in _RETRYABLE_STATUSES:
            return True
    text = f"{type(exc).__name__} {exc}".lower()
    if any(marker in text for marker in _FATAL_MARKERS):
        return False
    return any(marker in text for marker in _RETRYABLE_MARKERS)


def retry_after_seconds(exc: BaseException) -> float:
    """读取上游建议的等待秒数（Retry-After），读不到或不合法返回 0。"""
    raw: Any = getattr(exc, "retry_after", None)
    if raw is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            try:
                raw = headers.get("retry-after") or headers.get("Retry-After")
            except Exception:
                raw = None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return min(value, MAX_RETRY_AFTER_SECONDS)


class AnalysisThrottle:
    """把一批异步分析调用限制在「并发 N 路 + 每分钟 M 次」以内。

    用法::

        throttle = AnalysisThrottle(concurrency=2, rpm=20)
        result = await throttle.run(proc.classify_image, event=None, file_path=path)

    ``run()`` 只对「临时性错误」重试；鉴权、参数、余额之类的错误直接抛出，
    交给调用方按单张失败处理。
    """

    MAX_CONCURRENCY = 16
    MAX_RPM = 6000
    MAX_RETRIES_CAP = 8
    BASE_BACKOFF_SECONDS = 1.0
    MAX_BACKOFF_SECONDS = 60.0
    JITTER_RATIO = 0.25

    def __init__(
        self,
        *,
        concurrency: int = 2,
        rpm: int = 20,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        name: str = "analyze",
    ) -> None:
        self.name = str(name or "analyze")
        self.concurrency = _clamp_int(concurrency, 1, self.MAX_CONCURRENCY, 1)
        self.rpm = _clamp_int(rpm, 0, self.MAX_RPM, 0)
        self.max_retries = _clamp_int(max_retries, 0, self.MAX_RETRIES_CAP, 0)
        self.retry_backoff = _clamp_float(retry_backoff, 1.0, 10.0, 2.0)
        # rpm=0 表示不限速，只受并发上限约束。
        self.interval = 60.0 / self.rpm if self.rpm > 0 else 0.0
        self._sem = asyncio.Semaphore(self.concurrency)
        self._gate = asyncio.Lock()
        self._next_slot = 0.0
        self.inflight = 0
        self.completed = 0
        self.failed = 0
        self.retried = 0
        self.rate_limited = 0

    # ── 内部：发车时刻预约 ────────────────────────────────────

    async def _reserve(self) -> None:
        """占用一个 RPM 时隙；不限速时立即返回。"""
        if self.interval <= 0:
            return
        async with self._gate:
            now = time.monotonic()
            start = self._next_slot if self._next_slot > now else now
            self._next_slot = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def defer(self, seconds: float) -> None:
        """整体推后后续排队调用（被上游限流后调用）。"""
        if seconds <= 0:
            return
        target = time.monotonic() + seconds
        if target > self._next_slot:
            self._next_slot = target

    def backoff_delay(self, attempt: int, exc: BaseException | None = None) -> float:
        """第 ``attempt`` 次重试（从 0 开始）前应该等待的秒数。"""
        if exc is not None:
            hinted = retry_after_seconds(exc)
            if hinted > 0:
                return hinted
        delay = self.BASE_BACKOFF_SECONDS * (self.retry_backoff**max(0, attempt))
        delay = min(delay, self.MAX_BACKOFF_SECONDS)
        jitter = delay * self.JITTER_RATIO
        return max(0.1, delay + random.uniform(-jitter, jitter))

    # ── 对外：执行受控调用 ───────────────────────────────────

    async def run(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """在并发 / RPM 限制内执行一次异步调用，遇到限流自动退避重试。"""
        attempt = 0
        while True:
            await self._reserve()
            backoff = 0.0
            async with self._sem:
                self.inflight += 1
                try:
                    result = await fn(*args, **kwargs)
                    self.completed += 1
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not is_retryable(exc):
                        self.failed += 1
                        raise
                    self.rate_limited += 1
                    if attempt >= self.max_retries:
                        self.failed += 1
                        raise
                    backoff = self.backoff_delay(attempt, exc)
                    logger.warning(
                        f"[{self.name}] 上游限流/临时错误（{type(exc).__name__}: {exc}），"
                        f"{backoff:.1f}s 后重试（{attempt + 1}/{self.max_retries}）"
                    )
                finally:
                    self.inflight -= 1
            attempt += 1
            self.retried += 1
            # 让同批其它调用一起等，别接着撞墙。
            self.defer(backoff)
            await asyncio.sleep(backoff)

    def snapshot(self) -> dict[str, Any]:
        """限流器当前状态，用于状态接口回显。"""
        return {
            "concurrency": self.concurrency,
            "rpm": self.rpm,
            "max_retries": self.max_retries,
            "retry_backoff": round(self.retry_backoff, 2),
            "inflight": self.inflight,
            "completed": self.completed,
            "failed": self.failed,
            "retried": self.retried,
            "rate_limited": self.rate_limited,
        }


def build_analysis_throttle(
    config: Any,
    *,
    name: str = "analyze",
    concurrency: int | None = None,
    rpm: int | None = None,
) -> AnalysisThrottle:
    """按插件配置构造限流器；配置缺失时退回保守默认值。

    concurrency / rpm 传入非 None 时覆盖配置值，用于 WebUI 单次任务临时调速
    （改一次配置要重载插件，批量导入现场调速更实用）。
    """
    return AnalysisThrottle(
        concurrency=(
            concurrency
            if concurrency is not None
            else getattr(config, "batch_analyze_concurrency", 2)
        ),
        rpm=(rpm if rpm is not None else getattr(config, "batch_analyze_rpm", 20)),
        max_retries=getattr(config, "batch_analyze_max_retries", 3),
        retry_backoff=getattr(config, "batch_analyze_retry_backoff", 2.0),
        name=name,
    )
