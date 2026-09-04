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


class _CountingImage:
    """包一层真 Pillow 图片，只为了数 convert 被调了几次、每次转成什么模式。"""

    def __init__(self, im, modes):
        self._im = im
        self._modes = modes

    def __getattr__(self, name):
        return getattr(self._im, name)

    def __enter__(self):
        self._im.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._im.__exit__(*exc_info)

    def convert(self, *args, **kwargs):
        self._modes.append(args[0] if args else kwargs.get("mode"))
        return self._im.convert(*args, **kwargs)


class _CountingImageModule:
    """顶替 vlm_call_service.PILImage：open() 出来的图片会记账，其余原样转发。"""

    def __init__(self, modes):
        self._modes = modes

    def __getattr__(self, name):
        return getattr(PILImage, name)

    def open(self, *args, **kwargs):
        return _CountingImage(PILImage.open(*args, **kwargs), self._modes)


def _prepare(service, path):
    return asyncio.run(service._prepare_image_for_vlm(path))


def _montage_size(src) -> tuple[int, int]:
    out = None
    try:
        out, is_animated = _prepare(_make_service(), src)
        assert is_animated is True
        with PILImage.open(out) as im:
            return im.size
    finally:
        _cleanup(out)


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
            # 4 帧颜色互不相同，不会被相似帧过滤；排成 2x2 网格，小图不放大
            assert is_animated is True
            assert size == (128, 128)
    finally:
        _cleanup(src, out)


def test_wide_animation_is_packed_into_a_grid():
    """宽动图要排成网格：一字排开的话总宽必然超限，压回去每帧只剩几十像素高。"""
    if vlm_module.np is None:
        pytest.skip("没有 numpy 就不走抽帧分支")
    src = _write_animated_gif(frames=12, size=(600, 200))
    try:
        # 12 帧排成 2 列 x 6 行，每帧还是原始的 600x200，一个像素都没被压
        assert _montage_size(src) == (1200, 1200)
    finally:
        _cleanup(src)


def test_montage_never_exceeds_vlm_limit():
    """单帧本身就很大时按网格整体等比缩：长宽不许越过输入上限，总面积也有预算。"""
    if vlm_module.np is None:
        pytest.skip("没有 numpy 就不走抽帧分支")
    src = _write_animated_gif(frames=8, size=(1600, 400))
    try:
        width, height = _montage_size(src)
    finally:
        _cleanup(src)
    assert width <= vlm_module.MAX_VLM_DIMENSION
    assert height <= vlm_module.MAX_VLM_DIMENSION
    # 长宽各自压回 2048 以内，乘起来仍可能是四百万像素，总面积预算才是真正的封顶
    assert width * height <= vlm_module.MAX_MONTAGE_PIXELS
    assert height > 400  # 确实换行了，不是拼成一条长带子


def test_oversized_montage_falls_back_to_jpeg(monkeypatch):
    """拼接图超出体积预算时改存 JPEG：几 MB 的无损 PNG 上传慢，还容易被中转拒收。"""
    if vlm_module.np is None:
        pytest.skip("没有 numpy 就不走抽帧分支")
    # 预算压到 1 字节，任何拼接图都算超预算
    monkeypatch.setattr(vlm_module, "MONTAGE_MAX_BYTES", 1)
    src = _write_animated_gif(frames=6, size=(200, 200))
    out = None
    try:
        out, is_animated = _prepare(_make_service(), src)
        assert is_animated is True
        assert out.endswith(".jpg")  # 扩展名要跟真实格式对上，否则上游按后缀猜 mime 会猜错
        with PILImage.open(out) as im:
            assert im.format == "JPEG"
    finally:
        _cleanup(src, out)


def test_long_animation_caps_frame_count():
    """长动图最多拼 MAX_MONTAGE_FRAMES 帧，输出尺寸不随源动图帧数膨胀。"""
    if vlm_module.np is None:
        pytest.skip("没有 numpy 就不走抽帧分支")
    short = _write_animated_gif(frames=12, size=(120, 120))
    long_gif = _write_animated_gif(frames=90, size=(120, 120))
    try:
        # 12 帧 -> 3 列 x 4 行；90 帧均匀抽 12 帧，出图尺寸完全一样
        assert _montage_size(short) == (360, 480)
        assert _montage_size(long_gif) == (360, 480)
    finally:
        _cleanup(short, long_gif)


def test_frame_scan_avoids_full_size_decodes(monkeypatch):
    """挑帧只解 32x32 指纹，全尺寸 RGBA 解码次数不超过最终拼进去的帧数。

    这是内存峰值的关键：早期版本一次性把最多 60 帧全解成 RGBA 攥在手里，一张
    1920x480 的长动图光这一步就是几百 MB，小内存机器很容易被打爆。
    """
    if vlm_module.np is None:
        pytest.skip("没有 numpy 就不走抽帧分支")
    src = _write_animated_gif(frames=40, size=(200, 200))
    modes: list = []
    monkeypatch.setattr(vlm_module, "PILImage", _CountingImageModule(modes))
    out = None
    try:
        out, is_animated = _prepare(_make_service(), src)
        assert is_animated is True
        assert modes.count("RGBA") <= vlm_module.MAX_MONTAGE_FRAMES
        assert modes.count("RGB") >= 12  # 第一趟按步长扫了一遍指纹
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


def test_flat_copy_keeps_palette_colors_when_downscaling():
    """调色板图得先展开成真彩色再缩放，否则缩出来的是按颜色索引插值的乱码。"""
    src = _tmp(".gif")
    PILImage.new("RGB", (3000, 3000), (12, 200, 90)).save(src, "GIF")
    out = None
    try:
        out = VLMCallService._render_flat_copy(src, "PNG")
        with PILImage.open(out) as im:
            assert max(im.size) == vlm_module.MAX_VLM_DIMENSION
            pixel = im.convert("RGB").getpixel((im.width // 2, im.height // 2))
        assert max(abs(a - b) for a, b in zip(pixel, (12, 200, 90))) <= 8
    finally:
        _cleanup(src, out)


def test_strip_resize_matches_whole_image_resize(monkeypatch):
    """条带缩放的结果要跟整图缩放对得上，接缝处不能出现色带。"""
    base = PILImage.radial_gradient("L")
    src = PILImage.merge(
        "RGB",
        (
            base.resize((900, 600), PILImage.LANCZOS),
            PILImage.linear_gradient("L").resize((900, 600), PILImage.LANCZOS),
            base.resize((900, 600), PILImage.NEAREST),
        ),
    )
    # 把条带预算压到十几行，逃不开多条带、多接缝的路径
    monkeypatch.setattr(vlm_module, "STRIP_SOURCE_PIXELS", 60_000)
    whole = VLMCallService._resize_image(src, (300, 200))
    strips = VLMCallService._resize_in_strips(src, "RGB", (300, 200))
    assert strips.size == whole.size == (300, 200)
    worst = max(abs(a - b) for a, b in zip(whole.tobytes(), strips.tobytes()))
    assert worst <= 2, f"条带接缝处偏差过大：{worst}"


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
