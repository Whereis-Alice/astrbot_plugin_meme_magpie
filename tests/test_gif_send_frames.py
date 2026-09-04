"""发送侧 GIF 转换测试：帧数预算、待编码帧的内存形态、输出与源图一致。"""

import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace

import pytest

from core.processing import image_render_service as render_module
from core.processing.image_render_service import ImageRenderService

PILImage = render_module.PILImage

pytestmark = pytest.mark.skipif(
    PILImage is None, reason="需要 Pillow 才能验证发送侧 GIF 转换"
)


def _make_frames(size, count, mode="RGBA"):
    """造几帧差异明显的画面，方便逐帧比对。"""
    width, height = size
    frames = []
    for index in range(count):
        frame = PILImage.new("RGBA", size, (255, 255, 255, 255))
        block = PILImage.new(
            "RGBA",
            (max(1, width // 4), max(1, height // 4)),
            (250, 40 + index * 20, 30, 255),
        )
        frame.paste(block, ((index * 5) % max(1, width - block.width), height // 3))
        if mode == "P":
            frame = frame.convert("RGB").quantize(colors=64)
        frames.append(frame)
    return frames


def _write_animation(path, size, count, fmt="GIF", mode="RGBA"):
    frames = _make_frames(size, count, mode)
    frames[0].save(
        path,
        format=fmt,
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
    )
    return str(path)


def _service(**plugin_kwargs):
    plugin_kwargs.setdefault("send_meme_as_gif", True)
    return ImageRenderService(SimpleNamespace(**plugin_kwargs))


def _convert(service, path):
    return base64.b64decode(asyncio.run(service.file_to_gif_base64(path)))


def test_frame_budget_keeps_every_frame_for_normal_stickers():
    """常见尺寸的表情包帧数照旧，不能因为加了预算就悄悄丢帧。"""
    assert ImageRenderService.gif_frame_budget((512, 512), 60) == 30
    assert ImageRenderService.gif_frame_budget((400, 400), 12) == 12
    assert ImageRenderService.gif_frame_budget((1920, 480), 30) == 30
    assert ImageRenderService.gif_frame_budget((1000, 1000), 30) == 30


def test_frame_budget_shrinks_only_for_huge_animations():
    """4000x4000 一帧就 16MB，留 30 帧要 480MB，小内存机器扛不住。"""
    assert ImageRenderService.gif_frame_budget((4000, 4000), 30) == 4
    assert ImageRenderService.gif_frame_budget((12000, 12000), 30) == 1
    # 尺寸读出来是 0 这种异常情况也不能算出 0 帧
    assert ImageRenderService.gif_frame_budget((0, 0), 30) == 30


def test_paletted_frame_is_kept_as_palette_copy():
    """源文件是 GIF 时帧本来就是调色板图，原样留下才不掉色也最省内存。"""
    source = PILImage.new("P", (8, 8))
    source.putpalette([0, 0, 0, 255, 0, 0] + [7] * 762)
    source.paste(1, (0, 0, 4, 8))

    frame = ImageRenderService.to_gif_frame(source)

    assert frame.mode == "P"
    assert frame is not source
    assert frame.tobytes() == source.tobytes()
    assert frame.getpalette() == source.getpalette()


def test_rgba_frame_is_quantized_exactly_like_pillow():
    """预先量化必须和 Pillow 存 GIF 时自己做的那一步一致，否则出图会变样。"""
    from PIL import GifImagePlugin

    normalize = getattr(GifImagePlugin, "_normalize_mode", None)
    if normalize is None:
        pytest.skip("当前 Pillow 没有 _normalize_mode，无法做一致性对照")

    source = PILImage.new("RGBA", (16, 16), (0, 0, 0, 0))
    source.paste((12, 200, 90, 255), (0, 0, 16, 8))

    ours = ImageRenderService.to_gif_frame(source)
    theirs = normalize(source.copy())

    assert ours.mode == theirs.mode == "P"
    assert ours.tobytes() == theirs.tobytes()
    assert ours.getpalette() == theirs.getpalette()
    # 全透明区域要留成 GIF 的透明色，不能变成黑块
    assert "transparency" in ours.info
    assert ours.info.get("transparency") == theirs.info.get("transparency")


def test_animation_never_holds_rgba_frames(monkeypatch, tmp_path):
    """排队等编码的帧必须是 1 字节/像素的调色板图，这是省内存的关键。"""
    path = _write_animation(tmp_path / "a.webp", (64, 64), 8, fmt="WEBP")
    service = _service()

    modes = []
    original = ImageRenderService.to_gif_frame

    def spy(frame):
        result = original(frame)
        modes.append(result.mode)
        return result

    monkeypatch.setattr(ImageRenderService, "to_gif_frame", staticmethod(spy))

    data = _convert(service, path)

    assert modes and all(mode == "P" for mode in modes)
    with PILImage.open(BytesIO(data)) as out:
        assert out.format == "GIF"
        assert out.n_frames == 8


def test_output_frames_match_the_source_animation(tmp_path):
    """逐帧比对：帧数、尺寸、画面都要跟源图对得上。"""
    path = _write_animation(tmp_path / "a.gif", (48, 48), 6, fmt="GIF", mode="P")
    service = _service()

    data = _convert(service, path)

    with PILImage.open(path) as src, PILImage.open(BytesIO(data)) as out:
        assert out.n_frames == src.n_frames
        assert out.size == src.size
        for index in range(out.n_frames):
            src.seek(index)
            out.seek(index)
            before = src.convert("RGB").tobytes()
            after = out.convert("RGB").tobytes()
            worst = max(abs(a - b) for a, b in zip(before, after))
            assert worst == 0, f"第 {index} 帧画面变了"


def test_animation_with_per_frame_palettes_survives(tmp_path):
    """每帧带独立色表的 GIF 也不能掉帧或串色。"""
    path = tmp_path / "m.gif"
    frames = []
    for index in range(4):
        frame = PILImage.new("P", (32, 32))
        palette = [0, 0, 0] * 256
        palette[0:3] = [index * 60, 250 - index * 60, 30]
        palette[3:6] = [30, index * 50, 200]
        frame.putpalette(palette)
        frame.paste(1, (index * 4, 0, index * 4 + 8, 32))
        frames.append(frame)
    frames[0].save(
        path, save_all=True, append_images=frames[1:], duration=80, loop=0
    )
    service = _service()

    data = _convert(service, str(path))

    with PILImage.open(str(path)) as src, PILImage.open(BytesIO(data)) as out:
        assert out.n_frames == src.n_frames == 4
        for index in range(4):
            src.seek(index)
            out.seek(index)
            before = src.convert("RGB").tobytes()
            after = out.convert("RGB").tobytes()
            assert max(abs(a - b) for a, b in zip(before, after)) <= 2


def test_static_image_still_becomes_gif(tmp_path):
    path = tmp_path / "s.png"
    PILImage.new("RGBA", (32, 24), (10, 20, 30, 255)).save(path)
    service = _service()

    data = _convert(service, str(path))

    with PILImage.open(BytesIO(data)) as out:
        assert out.format == "GIF"
        assert out.size == (32, 24)


def test_switch_off_returns_original_bytes(tmp_path):
    """关掉「发送时转 GIF」就该原样返回文件，一个像素都不动。"""
    path = tmp_path / "s.png"
    PILImage.new("RGBA", (8, 8), (1, 2, 3, 255)).save(path)
    service = _service(send_meme_as_gif=False)

    got = base64.b64decode(asyncio.run(service.file_to_gif_base64(str(path))))

    assert got == path.read_bytes()
