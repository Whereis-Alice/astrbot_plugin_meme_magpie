"""AnalysisThrottle 单元测试：限速、并发、重试判定、退避与配置构造。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from core.processing.analysis_throttle import (
    MAX_RETRY_AFTER_SECONDS,
    AnalysisThrottle,
    build_analysis_throttle,
    is_retryable,
    retry_after_seconds,
)


class _FakeHTTPError(Exception):
    """模拟各家 SDK 带 status_code 的异常。"""

    def __init__(self, status_code, message="upstream error", retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        if retry_after is not None:
            self.retry_after = retry_after


class _FakeResponseError(Exception):
    """状态码藏在 response 对象里的异常。"""

    def __init__(self, status, headers=None):
        super().__init__("response error")
        self.response = SimpleNamespace(status=status, headers=headers or {})


def _fast(throttle):
    """把退避基数压到毫秒级，避免单测真的等好几秒。"""
    throttle.BASE_BACKOFF_SECONDS = 0.005
    throttle.MAX_BACKOFF_SECONDS = 0.05
    return throttle


# ── 参数收敛 ─────────────────────────────────────────────


def test_defaults():
    t = AnalysisThrottle()
    assert t.concurrency == 2
    assert t.rpm == 20
    assert t.max_retries == 3
    assert t.retry_backoff == pytest.approx(2.0)
    assert t.interval == pytest.approx(3.0)
    assert t.name == "analyze"


def test_clamps_out_of_range_values():
    t = AnalysisThrottle(concurrency=999, rpm=10**9, max_retries=99, retry_backoff=99.0)
    assert t.concurrency == AnalysisThrottle.MAX_CONCURRENCY
    assert t.rpm == AnalysisThrottle.MAX_RPM
    assert t.max_retries == AnalysisThrottle.MAX_RETRIES_CAP
    assert t.retry_backoff == pytest.approx(10.0)

    t2 = AnalysisThrottle(concurrency=0, rpm=-5, max_retries=-1, retry_backoff=0.1)
    assert t2.concurrency == 1
    assert t2.rpm == 0
    assert t2.max_retries == 0
    assert t2.retry_backoff == pytest.approx(1.0)


def test_invalid_values_fall_back():
    t = AnalysisThrottle(concurrency="abc", rpm=None, max_retries=[], retry_backoff="x")
    assert t.concurrency == 1
    assert t.rpm == 0
    assert t.max_retries == 0
    assert t.retry_backoff == pytest.approx(2.0)


def test_rpm_zero_disables_rate_limit():
    t = AnalysisThrottle(rpm=0)
    assert t.interval == 0.0

    async def _noop():
        return "ok"

    async def _main():
        started = time.monotonic()
        for _ in range(5):
            assert await t.run(_noop) == "ok"
        return time.monotonic() - started

    assert asyncio.run(_main()) < 1.0
    assert t.completed == 5


def test_empty_name_falls_back():
    assert AnalysisThrottle(name="").name == "analyze"


# ── 并发与速率 ───────────────────────────────────────────


def test_concurrency_is_capped():
    t = AnalysisThrottle(concurrency=3, rpm=0)
    peak = {"value": 0, "now": 0}

    async def _work():
        peak["now"] += 1
        peak["value"] = max(peak["value"], peak["now"])
        await asyncio.sleep(0.02)
        peak["now"] -= 1
        return True

    async def _main():
        await asyncio.gather(*(t.run(_work) for _ in range(12)))

    asyncio.run(_main())
    assert peak["value"] <= 3
    assert t.completed == 12


def test_rpm_spaces_out_calls():
    # rpm=3000 → 每次间隔 20ms，5 次至少要跨 4 个间隔。
    t = AnalysisThrottle(concurrency=5, rpm=3000)
    assert t.interval == pytest.approx(0.02)

    async def _noop():
        return 1

    async def _main():
        started = time.monotonic()
        await asyncio.gather(*(t.run(_noop) for _ in range(5)))
        return time.monotonic() - started

    assert asyncio.run(_main()) >= 0.06


def test_defer_pushes_next_slot():
    t = AnalysisThrottle(rpm=60)
    before = t._next_slot
    t.defer(0.0)
    assert t._next_slot == before
    t.defer(5.0)
    assert t._next_slot > before
    high = t._next_slot
    t.defer(0.001)
    assert t._next_slot == high


# ── 可重试判定 ───────────────────────────────────────────


def test_retryable_statuses():
    for status in (408, 409, 425, 429, 500, 502, 503, 504, 520, 524):
        assert is_retryable(_FakeHTTPError(status)) is True


def test_fatal_statuses_not_retryable():
    for status in (400, 401, 403, 404, 413, 415, 422):
        assert is_retryable(_FakeHTTPError(status)) is False


def test_builtin_transient_errors_are_retryable():
    assert is_retryable(TimeoutError("deadline")) is True
    assert is_retryable(ConnectionError("reset by peer")) is True


def test_status_from_response_object():
    assert is_retryable(_FakeResponseError(429)) is True
    assert is_retryable(_FakeResponseError(401)) is False


def test_text_markers_when_no_status():
    assert is_retryable(RuntimeError("Rate limit reached, try again later")) is True
    assert is_retryable(RuntimeError("\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41")) is True
    assert is_retryable(RuntimeError("server busy")) is True
    assert is_retryable(ValueError("bad image format")) is False


def test_fatal_markers_win_over_retryable_markers():
    # 文本里同时出现「余额不足」和「稍后重试」时，必须判定为致命错误。
    exc = RuntimeError("insufficient_quota, please try again later")
    assert is_retryable(exc) is False
    exc_cn = RuntimeError("\u4f59\u989d\u4e0d\u8db3\uff0c\u7a0d\u540e\u91cd\u8bd5")
    assert is_retryable(exc_cn) is False


def test_bool_attributes_are_ignored():
    exc = RuntimeError("weird")
    exc.status = True
    assert is_retryable(exc) is False


def test_string_status_code_is_parsed():
    exc = RuntimeError("stringly typed")
    exc.status_code = "429"
    assert is_retryable(exc) is True


# ── Retry-After ──────────────────────────────────────────


def test_retry_after_from_attribute():
    assert retry_after_seconds(_FakeHTTPError(429, retry_after=7)) == pytest.approx(7.0)


def test_retry_after_from_headers():
    assert retry_after_seconds(_FakeResponseError(429, {"retry-after": "3.5"})) == pytest.approx(3.5)
    assert retry_after_seconds(_FakeResponseError(429, {"Retry-After": "2"})) == pytest.approx(2.0)


def test_retry_after_clamped_and_sanitised():
    assert retry_after_seconds(_FakeHTTPError(429, retry_after=99999)) == MAX_RETRY_AFTER_SECONDS
    assert retry_after_seconds(_FakeHTTPError(429, retry_after=0)) == 0.0
    assert retry_after_seconds(_FakeHTTPError(429, retry_after=-1)) == 0.0
    assert retry_after_seconds(_FakeHTTPError(429, retry_after="soon")) == 0.0
    assert retry_after_seconds(RuntimeError("no hint")) == 0.0


# ── 退避时长 ─────────────────────────────────────────────


def test_backoff_prefers_retry_after_hint():
    t = AnalysisThrottle()
    assert t.backoff_delay(0, _FakeHTTPError(429, retry_after=9)) == pytest.approx(9.0)


def test_backoff_grows_and_is_capped():
    t = AnalysisThrottle(retry_backoff=2.0)
    first = t.backoff_delay(0)
    later = t.backoff_delay(3)
    assert 0.7 <= first <= 1.3
    assert later > first
    assert t.backoff_delay(50) <= t.MAX_BACKOFF_SECONDS * (1 + t.JITTER_RATIO)


def test_backoff_never_below_floor():
    t = _fast(AnalysisThrottle(retry_backoff=1.0))
    assert t.backoff_delay(0) >= 0.1


# ── run() 的重试行为 ────────────────────────────────────


def test_run_retries_then_succeeds():
    t = _fast(AnalysisThrottle(concurrency=1, rpm=0, max_retries=3))
    calls = {"n": 0}

    async def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeHTTPError(429)
        return "done"

    assert asyncio.run(t.run(_flaky)) == "done"
    assert calls["n"] == 3
    assert t.completed == 1
    assert t.failed == 0
    assert t.retried == 2
    assert t.rate_limited == 2
    assert t.inflight == 0


def test_run_does_not_retry_fatal_errors():
    t = _fast(AnalysisThrottle(rpm=0, max_retries=5))
    calls = {"n": 0}

    async def _fatal():
        calls["n"] += 1
        raise _FakeHTTPError(401, "bad key")

    with pytest.raises(_FakeHTTPError):
        asyncio.run(t.run(_fatal))
    assert calls["n"] == 1
    assert t.failed == 1
    assert t.retried == 0
    assert t.rate_limited == 0


def test_run_gives_up_after_max_retries():
    t = _fast(AnalysisThrottle(rpm=0, max_retries=2))
    calls = {"n": 0}

    async def _always_429():
        calls["n"] += 1
        raise _FakeHTTPError(429)

    with pytest.raises(_FakeHTTPError):
        asyncio.run(t.run(_always_429))
    assert calls["n"] == 3  # 首次 + 2 次重试
    assert t.retried == 2
    assert t.rate_limited == 3
    assert t.failed == 1
    assert t.completed == 0


def test_run_with_zero_retries_fails_fast():
    t = _fast(AnalysisThrottle(rpm=0, max_retries=0))
    calls = {"n": 0}

    async def _always_429():
        calls["n"] += 1
        raise _FakeHTTPError(503)

    with pytest.raises(_FakeHTTPError):
        asyncio.run(t.run(_always_429))
    assert calls["n"] == 1
    assert t.retried == 0
    assert t.failed == 1


def test_run_passes_through_args_and_kwargs():
    t = AnalysisThrottle(rpm=0)

    async def _echo(a, b, *, c=0):
        return (a, b, c)

    assert asyncio.run(t.run(_echo, 1, 2, c=3)) == (1, 2, 3)


def test_cancellation_is_not_swallowed():
    t = AnalysisThrottle(rpm=0, max_retries=5)

    async def _cancelled():
        raise asyncio.CancelledError()

    async def _main():
        with pytest.raises(asyncio.CancelledError):
            await t.run(_cancelled)

    asyncio.run(_main())
    assert t.inflight == 0
    assert t.retried == 0


def test_inflight_resets_after_failure():
    t = _fast(AnalysisThrottle(concurrency=2, rpm=0, max_retries=1))

    async def _boom():
        raise _FakeHTTPError(500)

    async def _main():
        results = await asyncio.gather(
            t.run(_boom), t.run(_boom), return_exceptions=True
        )
        return results

    results = asyncio.run(_main())
    assert all(isinstance(r, _FakeHTTPError) for r in results)
    assert t.inflight == 0
    assert t.failed == 2


# ── snapshot / 配置构造 ─────────────────────────────────


def test_snapshot_shape():
    t = AnalysisThrottle(concurrency=4, rpm=120, max_retries=2, retry_backoff=1.5)
    snap = t.snapshot()
    assert snap == {
        "concurrency": 4,
        "rpm": 120,
        "max_retries": 2,
        "retry_backoff": 1.5,
        "inflight": 0,
        "completed": 0,
        "failed": 0,
        "retried": 0,
        "rate_limited": 0,
    }


def test_build_from_config():
    cfg = SimpleNamespace(
        batch_analyze_concurrency=5,
        batch_analyze_rpm=90,
        batch_analyze_max_retries=4,
        batch_analyze_retry_backoff=1.5,
    )
    t = build_analysis_throttle(cfg, name="reanalyze")
    assert (t.concurrency, t.rpm, t.max_retries, t.name) == (5, 90, 4, "reanalyze")
    assert t.retry_backoff == pytest.approx(1.5)


def test_build_overrides_win_over_config():
    cfg = SimpleNamespace(batch_analyze_concurrency=5, batch_analyze_rpm=90)
    t = build_analysis_throttle(cfg, concurrency=1, rpm=0)
    assert t.concurrency == 1
    assert t.rpm == 0
    assert t.interval == 0.0


def test_build_uses_conservative_defaults_when_config_empty():
    t = build_analysis_throttle(SimpleNamespace())
    assert (t.concurrency, t.rpm, t.max_retries) == (2, 20, 3)
    assert t.retry_backoff == pytest.approx(2.0)
