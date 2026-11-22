"""Native Markdown is prepared before attaching and converted after splitters."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_attach_delivery import Main, _Event, _Result, _image, _service


def test_official_markdown_preparation_precedes_image_attachment(tmp_path):
    source = str(tmp_path / "meme.png")
    calls = []

    async def prepare(_event, original, attached):
        calls.append(("prepare", original, attached))
        return True

    markdown = SimpleNamespace(prepare_attachment=prepare)
    service = _service(_get_qq_markdown_delivery_service=lambda: markdown)

    async def append(_event, result, path):
        calls.append(("append", path))
        result.chain.append(_image(path=path))
        return True

    service._append_emoji_to_result = append
    result = _Result("文字在上")
    assert asyncio.run(service.attach_selected_emoji(_Event(), result, source)) == source
    assert calls == [("prepare", source, source), ("append", source)]
    assert result.use_markdown_ is True
    assert result.use_t2i_ is False
    assert len(result.chain) == 2


def test_cancelled_markdown_preparation_leaves_no_attached_image(tmp_path):
    source = str(tmp_path / "meme.png")
    append = AsyncMock(return_value=True)

    async def run():
        started = asyncio.Event()

        async def prepare(*_args):
            started.set()
            await asyncio.Event().wait()

        markdown = SimpleNamespace(prepare_attachment=prepare)
        service = _service(_get_qq_markdown_delivery_service=lambda: markdown)
        service._append_emoji_to_result = append
        result = _Result("原回复")
        task = asyncio.create_task(service.attach_selected_emoji(_Event(), result, source))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(result.chain) == 1
        assert result.get_plain_text() == "原回复"

    asyncio.run(run())
    append.assert_not_awaited()


def test_unavailable_markdown_upload_keeps_the_standard_image_path(tmp_path):
    source = str(tmp_path / "meme.png")
    markdown = SimpleNamespace(prepare_attachment=AsyncMock(return_value=False))
    service = _service(_get_qq_markdown_delivery_service=lambda: markdown)
    service._append_emoji_to_result = AsyncMock(return_value=True)
    result = _Result("仍可回复")
    assert asyncio.run(service.attach_selected_emoji(_Event(), result, source)) == source
    service._append_emoji_to_result.assert_awaited_once()
    assert not hasattr(result, "use_markdown_")


@pytest.mark.parametrize("prepared", [True, False])
def test_scope_change_during_preparation_does_not_attach_via_fallback(tmp_path, prepared):
    source = str(tmp_path / "meme.png")
    allowed = True

    async def prepare(*_args):
        nonlocal allowed
        allowed = False
        return prepared

    service = _service(
        _get_qq_markdown_delivery_service=lambda: SimpleNamespace(prepare_attachment=prepare),
        meme_selector=SimpleNamespace(is_path_allowed_for_event=lambda *_args: allowed),
        is_send_enabled_for_event=lambda _event: True,
    )
    service._append_emoji_to_result = AsyncMock(return_value=True)
    result = _Result("原回复仍可发送")
    assert asyncio.run(service.attach_selected_emoji(_Event(), result, source)) is None
    service._append_emoji_to_result.assert_not_awaited()
    assert len(result.chain) == 1


def test_final_hook_converts_the_result_left_by_the_splitter():
    result = _Result("最后一段")
    event = _Event()
    event.get_result = lambda: result
    converter = Mock()
    main = Main.__new__(Main)
    main._get_qq_markdown_delivery_service = lambda: SimpleNamespace(convert_result=converter)
    asyncio.run(main._finalize_qq_markdown(event))
    converter.assert_called_once_with(event, result)
