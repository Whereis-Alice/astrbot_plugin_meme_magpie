"""VLM 图片预处理测试：动图抽帧、上游不收的格式转码、mime 被拒后的自救。"""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from core.processing import vlm_call_service as vlm_module
from core.processing.vlm_call_service import VLMCallService

PILImage = vlm_module.PILImage

pytestmark = pytest.mark.skipif(PILImage is None, reason="需要 Pillow 才能验证图片预处理")

# 上游中转拒收 GIF 时的真实报文（已截断），同时含 mime 与 convert_request_failed 两个特征
MIME_ERROR = (
    "Error code: 500 - {'error': {'message': 'mime type is not supported by Gemini: "
    "image/gif, supported types are: [image/png image/jpeg]', "
    "'type': 'new_api_error', 'code': 'convert_request_failed'}}"
)


class _FakeLLMResponse:
    def __init__(self, text: str = "ok"):
        self.completion_text = text


class _RecordingLLM:
    """记录每次 llm_generate 的入参，并按脚本依次返回结果或抛异常。"""

    def __init__(self, script=None):
        self._script = list(script or [])
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0) if self._script else _FakeLLMResponse()
        if isinstance(item, BaseException):
            raise item
        return item


def _make_service(llm=None, **plugin_kwargs):
    plugin_config = SimpleNamespace(vision_provider_id="vlm-1", data_dir=None)
    plugin = SimpleNamespace(
        plugin_config=plugin_config,
        context=SimpleNamespace(llm_generate=llm),
        **plugin_kwargs,
    )
    return VLMCallService(plugin)


def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def _cleanup(*paths) -> None:
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def _write_static_gif(size=(48, 32)) -> str:
    path = _tmp(".gif")
    PILImage.new("RGB", size, (200, 40, 40)).save(path, "GIF")
    return path


def _write_animated_gif(frames: int = 4, size=(64, 64)) -> str:
    path = _tmp(".gif")
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    imgs = [PILImage.new("RGB", size, colors[i % len(colors)]) for i in range(frames)]
    imgs[0].save(path, "GIF", save_all=True, append_images=imgs[1:], duration=120, loop=0)
    return path


def _prepare(service, path):
    return asyncio.run(service._prepare_image_for_vlm(path))


# ── 预处理：格式白名单 ────────────────────────────────────


def test_static_gif_is_converted_to_png():
    """静态 GIF 同样要转码：上游只看 mime，不管这张图动不动。"""
    src = _write_static_gif()
    out = None
    try:
        out, is_animated = _prepare(_make_service(), src)
        assert is_animated is False
        assert out != src
        assert out.endswith(".png")
        with PILImage.open(out) as im:
            assert im.format == "PNG"
            assert im.size == (48, 32)
    finally:
        _cleanup(src, out)


def test_animated_gif_never_leaves_raw_gif():
    """动图不管抽帧成功还是降级，出口都不能还是 GIF。"""
    src = _write_animated_gif()
    out = None
    try:
        out, is_animated = _prepare(_make_service(), src)
        assert out != src
        assert out.endswith(".png")
        with PILImage.open(out) as im:
            assert im.format == "PNG"
            size = im.size
        if vlm_module.np is None:
            # 没有 numpy 时退化成单帧 PNG，而不是把原始动图直接丢给上游
            assert is_animated is False
            assert size == (64, 64)
        else:
            # 4 帧颜色互不相同，不会被相似帧过滤；矮图保持原高，不再补大片黑边
            assert is_animated is True
            assert size == (256, 64)
    finally:
        _cleanup(src, out)


def test_whitelisted_static_png_passes_through():
    png = _tmp(".png")
    PILImage.new("RGB", (20, 20), (10, 120, 220)).save(png, "PNG")
    try:
        out, is_animated = _prepare(_make_service(), png)
        assert out == png  # 原样放行，不产生临时文件
        assert is_animated is False
    finally:
        _cleanup(png)


def test_static_webp_passes_through():
    webp = _tmp(".webp")
    try:
        PILImage.new("RGB", (20, 20), (10, 120, 220)).save(webp, "WEBP")
    except Exception:
        _cleanup(webp)
        pytest.skip("当前 Pillow 不支持 WebP 编码")
    try:
        out, is_animated = _prepare(_make_service(), webp)
        assert out == webp
        assert is_animated is False
    finally:
        _cleanup(webp)


def test_extension_mismatch_is_rewritten():
    """.gif 里装着 PNG：上游多半按后缀猜 mime，必须重写一份再送。"""
    fake_gif = _tmp(".gif")
    PILImage.new("RGB", (24, 24), (0, 0, 0)).save(fake_gif, "PNG")
    out = None
    try:
        out, is_animated = _prepare(_make_service(), fake_gif)
        assert out != fake_gif
        assert out.endswith(".png")
        assert is_animated is False
    finally:
        _cleanup(fake_gif, out)


def test_flat_copy_composites_on_white_background():
    """单帧透明区域合到白底：黑底会把深色线稿和白描边文字糊成一片。"""
    src = _tmp(".png")
    PILImage.new("RGBA", (16, 16), (0, 0, 0, 0)).save(src, "PNG")
    out = None
    try:
        out = VLMCallService._render_flat_copy(src, "PNG")
        with PILImage.open(out) as im:
            assert im.mode == "RGB"
            assert im.getpixel((0, 0)) == (255, 255, 255)
    finally:
        _cleanup(src, out)


def test_flat_copy_downscales_oversized_image():
    src = _tmp(".png")
    PILImage.new("RGB", (3000, 150), (30, 30, 30)).save(src, "PNG")
    out = None
    try:
        out = VLMCallService._render_flat_copy(src, "PNG")
        with PILImage.open(out) as im:
            assert max(im.size) == vlm_module.MAX_VLM_DIMENSION
            assert im.size == (2048, 102)
    finally:
        _cleanup(src, out)


# ── 上游拒收格式后的自救 ──────────────────────────────────


def test_mime_rejection_detection():
    assert VLMCallService._is_mime_rejected(RuntimeError(MIME_ERROR)) is True
    assert VLMCallService._is_mime_rejected(RuntimeError("不支持的图片格式")) is True
    assert VLMCallService._is_mime_rejected(RuntimeError("429 too many requests")) is False


def test_mime_rejection_converts_and_retries():
    src = _write_static_gif()
    llm = _RecordingLLM([RuntimeError(MIME_ERROR), _FakeLLMResponse("这是一只猫")])
    service = _make_service(llm, vision_max_retries=3, vision_retry_delay=0)
    temp_files: list[str] = []
    try:
        text = asyncio.run(service._do_vlm_call("vlm-1", "prompt", src, temp_files=temp_files))
        assert text == "这是一只猫"
        assert len(llm.calls) == 2
        assert llm.calls[0]["image_urls"] == [src]
        converted = llm.calls[1]["image_urls"][0]
        assert converted.endswith(".png")
        assert temp_files == [converted]  # 登记给调用方统一清理
    finally:
        _cleanup(src, *temp_files)


def test_mime_rejection_stops_after_one_conversion():
    src = _write_static_gif()
    llm = _RecordingLLM([RuntimeError(MIME_ERROR), RuntimeError(MIME_ERROR)])
    service = _make_service(llm, vision_max_retries=5, vision_retry_delay=0)
    temp_files: list[str] = []
    try:
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(service._do_vlm_call("vlm-1", "prompt", src, temp_files=temp_files))
        assert "mime type is not supported" in str(excinfo.value)
        assert len(llm.calls) == 2  # 转码后仍被拒就不再空转
    finally:
        _cleanup(src, *temp_files)


def test_mime_rejection_raises_immediately_when_conversion_impossible():
    missing = os.path.join(tempfile.gettempdir(), "magpie-no-such-image.gif")
    _cleanup(missing)
    llm = _RecordingLLM([RuntimeError(MIME_ERROR), _FakeLLMResponse("never")])
    service = _make_service(llm, vision_max_retries=5, vision_retry_delay=0)
    with pytest.raises(RuntimeError):
        asyncio.run(service._do_vlm_call("vlm-1", "prompt", missing))
    assert len(llm.calls) == 1


def test_non_mime_errors_still_consume_retry_budget():
    """普通失败仍走原有的重试 + 退避逻辑，不受自救分支影响。"""
    llm = _RecordingLLM([RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
    service = _make_service(llm, vision_max_retries=3, vision_retry_delay=0)
    with pytest.raises(Exception) as excinfo:
        asyncio.run(service._do_vlm_call("vlm-1", "prompt", "http://example.com/a.png"))
    assert "已重试3次" in str(excinfo.value)
    assert len(llm.calls) == 3


def test_call_vision_model_sends_png_and_cleans_up():
    src = _write_static_gif()
    llm = _RecordingLLM([_FakeLLMResponse("描述")])
    service = _make_service(llm, vision_max_retries=2, vision_retry_delay=0)
    try:
        text = asyncio.run(service._call_vision_model(None, src, "分析这张图"))
        assert text == "描述"
        sent = llm.calls[0]["image_urls"][0]
        assert sent.endswith(".png")
        assert sent != src
        assert not os.path.exists(sent)  # 调用结束后临时文件已清理
        assert os.path.exists(src)  # 原图不能被动过
    finally:
        _cleanup(src)
