"""本地事件图片下载/复制行为测试。"""

import asyncio
from pathlib import Path

from astrbot_plugin_meme_magpie.core.events.image_download_service import (
    ImageDownloadService,
)


def test_detect_local_file_type_uses_magic_bytes(tmp_path):
    cases = {
        "gif": (b"GIF89a\x01\x02\x03\x04", ".gif", True),
        "png": (b"\x89PNG\r\n\x1a\n\x00\x00", ".png", False),
        "webp": (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp", False),
        "jpeg": (b"\xff\xd8\xff\xe0\x00\x10JFIF", ".jpg", False),
        "bmp": (b"BM\x00\x00\x00\x00\x00\x00", ".bmp", False),
    }
    for name, (payload, expected_ext, expected_gif) in cases.items():
        path = tmp_path / "wrong-name.jpg"
        path.write_bytes(payload)
        assert ImageDownloadService.detect_local_file_type(str(path)) == (
            expected_ext,
            expected_gif,
        ), name


def test_detect_local_file_type_keeps_allowed_unknown_suffix(tmp_path):
    path = tmp_path / "adapter-export.webp"
    path.write_bytes(b"not-a-known-magic-number")

    assert ImageDownloadService.detect_local_file_type(str(path)) == (".webp", False)


def test_download_original_image_copies_event_owned_file(tmp_path):
    source = tmp_path / "event-owned.gif"
    source.write_bytes(b"GIF89afake-but-readable")
    service = ImageDownloadService()
    image = type("Image", (), {"path": "", "file": str(source), "url": ""})()

    temp_path, is_gif = asyncio.run(service.download_original_image(image))

    try:
        assert is_gif is True
        assert temp_path != str(source)
        assert Path(temp_path).suffix == ".gif"
        assert Path(temp_path).read_bytes() == source.read_bytes()
    finally:
        Path(temp_path).unlink()


def test_download_original_image_detects_mislabeled_webp(tmp_path):
    source = tmp_path / "event-owned.jpg"
    source.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    service = ImageDownloadService()
    image = type("Image", (), {"path": str(source), "file": "", "url": ""})()

    temp_path, is_gif = asyncio.run(service.download_original_image(image))

    try:
        assert is_gif is False
        assert Path(temp_path).suffix == ".webp"
    finally:
        Path(temp_path).unlink()
