"""A force-capture minute admits every image with isolated annotation snapshots."""

import asyncio
import copy
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import Image


event_module = import_module("astrbot_plugin_meme_magpie.core.events.event_handler")
EventHandler = event_module.EventHandler


class CaptureQueue:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None

    async def submit_force_capture_async(self, descriptors):
        snapshot = copy.deepcopy(descriptors)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        self.calls.extend(snapshot)
        return len(snapshot)


def _event(*, origin="aiocqhttp:GroupMessage:42", sender="owner", images=()):
    return SimpleNamespace(
        unified_msg_origin=origin,
        get_sender_id=lambda: sender,
        get_session_id=lambda: "42",
        get_messages=lambda: list(images),
        send=AsyncMock(),
        message_obj=SimpleNamespace(sender=SimpleNamespace(user_id=sender)),
    )


def _image(path=None, *, url=""):
    image = Image()
    image.file = str(path) if path else ""
    image.url = url
    return image


def _handler(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(event_module.time, "time", lambda: clock[0])
    plugin = SimpleNamespace(
        steal_meme=False,
        is_steal_enabled_for_event=lambda event: False,
        plugin_config=SimpleNamespace(audit_required=True),
        _process_image=AsyncMock(),
    )
    handler = EventHandler(plugin)
    plugin.event_handler = handler
    plugin.get_force_capture_entry = handler.get_force_capture_entry
    plugin.consume_force_capture = handler.consume_force_capture
    handler._extract_store_emoji_segments = lambda _event: []
    queue = CaptureQueue()
    handler._background_queue = queue
    return handler, plugin, queue, clock


def test_capture_minute_expires_without_consuming_first_read(monkeypatch):
    handler, _plugin, _queue, clock = _handler(monkeypatch)
    event = _event()
    handler.begin_force_capture(event, 60, character="爱乃", work="anemoi")
    entry = handler.get_force_capture_entry(event)
    assert entry["character"] == "爱乃"
    assert entry["work"] == "anemoi"
    clock[0] = 159.9
    assert handler.get_force_capture_entry(event) is not None
    clock[0] = 160.1
    assert handler.get_force_capture_entry(event) is None


def test_capture_entries_are_detached_snapshots(monkeypatch):
    handler, _plugin, _queue, _clock = _handler(monkeypatch)
    event = _event()
    handler.begin_force_capture(event, 60, character="艾拉", work="可塑性记忆")
    entry = handler.get_force_capture_entry(event)
    entry["character"] = "changed by caller"
    entry["until"] = 0
    assert handler.get_force_capture_entry(event)["character"] == "艾拉"


def test_two_command_senders_in_one_group_have_independent_windows(monkeypatch):
    handler, _plugin, _queue, _clock = _handler(monkeypatch)
    first, second = _event(sender="first"), _event(sender="second")
    handler.begin_force_capture(first, 60, character="艾拉", work="可塑性记忆")
    handler.begin_force_capture(second, 60, character="爱乃", work="anemoi")
    assert handler.get_force_capture_entry(first)["character"] == "艾拉"
    assert handler.get_force_capture_entry(second)["character"] == "爱乃"
    assert handler.get_force_capture_entry(_event(sender="unrelated")) is None
    assert handler.get_force_capture_entry(_event(sender="")) is None


def test_same_bare_group_id_on_different_platforms_does_not_share_window(monkeypatch):
    handler, _plugin, _queue, _clock = _handler(monkeypatch)
    first = _event(origin="aiocqhttp:GroupMessage:42")
    second = _event(origin="telegram:GroupMessage:42")
    handler.begin_force_capture(first, 60, character="艾拉")
    assert handler.get_force_capture_entry(second) is None
    handler.begin_force_capture(second, 60, character="爱乃")
    assert handler.get_force_capture_entry(first)["character"] == "艾拉"
    assert handler.get_force_capture_entry(second)["character"] == "爱乃"


def test_multi_image_message_and_store_emojis_are_all_admitted(monkeypatch, tmp_path):
    handler, plugin, queue, _clock = _handler(monkeypatch)
    paths = []
    for index in range(5):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"image fixture")
        paths.append(path)
    event = _event(images=[_image(path) for path in paths])
    handler._extract_store_emoji_segments = lambda _event: [
        {"url": "https://example.invalid/store-1.gif", "meta": {"emoji_id": "one"}},
        {"url": "https://example.invalid/store-2.gif", "meta": {"emoji_id": "two"}},
    ]
    handler.begin_force_capture(event, 60, character="爱乃", work="anemoi")
    asyncio.run(handler.on_message(event))
    assert len(queue.calls) == 7
    assert {item["media_ref"] for item in queue.calls} == {
        *(str(path) for path in paths),
        "https://example.invalid/store-1.gif", "https://example.invalid/store-2.gif",
    }
    assert all(item["source"] == "force_capture" for item in queue.calls)
    assert all(item["to_pending"] is False for item in queue.calls)
    assert all(item["character"] == "爱乃" and item["work"] == "anemoi" for item in queue.calls)
    assert handler.get_force_capture_entry(event) is not None
    assert event.send.await_count >= 1
    plugin._process_image.assert_not_awaited()


def test_more_than_queue_capacity_concurrent_messages_are_not_first_only(monkeypatch, tmp_path):
    handler, _plugin, queue, _clock = _handler(monkeypatch)
    events = []
    for index in range(48):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"image fixture")
        events.append(_event(images=[_image(path)]))
    handler.begin_force_capture(events[0], 60, character="艾拉", work="可塑性记忆")

    async def run():
        await asyncio.gather(*(handler.on_message(event) for event in events))

    asyncio.run(run())
    assert len(queue.calls) == 48
    assert len({item["media_ref"] for item in queue.calls}) == 48
    assert all(item["character"] == "艾拉" for item in queue.calls)
    assert handler.get_force_capture_entry(events[-1]) is not None


def test_admitted_metadata_survives_expiry_and_new_command(monkeypatch, tmp_path):
    handler, _plugin, queue, clock = _handler(monkeypatch)
    path = tmp_path / "old-window.png"
    path.write_bytes(b"image fixture")
    event = _event(images=[_image(path)])
    handler.begin_force_capture(event, 60, character="艾拉", work="可塑性记忆")

    async def run():
        queue.release = asyncio.Event()
        task = asyncio.create_task(handler.on_message(event))
        try:
            await asyncio.wait_for(queue.entered.wait(), timeout=1)
            clock[0] = 160.1
            assert handler.get_force_capture_entry(event) is None
            handler.begin_force_capture(event, 60, character="爱乃", work="anemoi")
        finally:
            queue.release.set()
            await task

    asyncio.run(run())
    assert len(queue.calls) == 1
    assert queue.calls[0]["character"] == "艾拉"
    assert queue.calls[0]["work"] == "可塑性记忆"
    assert handler.get_force_capture_entry(event)["character"] == "爱乃"


def test_after_expiration_images_are_not_force_admitted(monkeypatch, tmp_path):
    handler, _plugin, queue, clock = _handler(monkeypatch)
    path = tmp_path / "late.png"
    path.write_bytes(b"image fixture")
    event = _event(images=[_image(path)])
    handler.begin_force_capture(event, 60)
    clock[0] = 160.1
    asyncio.run(handler.on_message(event))
    assert queue.calls == []


def test_non_initiating_sender_is_not_annotated_or_force_admitted(monkeypatch, tmp_path):
    handler, _plugin, queue, _clock = _handler(monkeypatch)
    path = tmp_path / "other-sender.png"
    path.write_bytes(b"image fixture")
    handler.begin_force_capture(_event(sender="owner"), 60, character="艾拉")
    other = _event(sender="other", images=[_image(path)])
    asyncio.run(handler.on_message(other))
    assert queue.calls == []


def test_manual_character_and_work_override_model_metadata_and_join_tags(tmp_path):
    from core.processing.llm_meme_hints import LlmMemeHints
    from tests.test_vlm_response_validation import _make_processor

    processor, _llm, _framework, path = _make_processor(tmp_path)
    stored = AsyncMock(return_value=(True, {"result": {}}))
    processor._store_and_index_image = stored
    hints = LlmMemeHints.from_tool_args(character="爱乃", work="anemoi")
    result = asyncio.run(processor._handle_classification_result(
        "happy", "happy", ["笑"], "一位女孩在笑", [], path, False, "hash", {},
        extra_meta={"character": "错误角色", "work": "错误作品"},
        llm_hints=hints,
    ))
    assert result[0] is True
    kwargs = stored.call_args.kwargs
    assert kwargs["extra_meta"]["character"] == "爱乃"
    assert kwargs["extra_meta"]["work"] == "anemoi"
    assert set(kwargs["tags"]) >= {"爱乃", "anemoi", "笑"}


def test_manual_labels_are_in_model_prompt_and_separate_cache_entries(tmp_path):
    from core.processing.llm_meme_hints import LlmMemeHints
    from core.processing.prompt_manager import PromptManager
    from tests.test_vlm_response_validation import _make_processor

    processor, llm, _framework, path = _make_processor(tmp_path)
    first = LlmMemeHints.from_tool_args(character="艾拉", work="可塑性记忆")
    second = LlmMemeHints.from_tool_args(character="爱乃", work="anemoi")
    assert asyncio.run(processor.classify_image(None, path, known=first))[0] == "happy"
    assert "艾拉" in llm.call_args.kwargs["prompt"]
    assert "可塑性记忆" in llm.call_args.kwargs["prompt"]
    first_sig = "model#known:" + PromptManager.known_facts_signature(first)
    second_sig = "model#known:" + PromptManager.known_facts_signature(second)
    assert first_sig != second_sig
    processor._put_image_cache("same-image", "happy", [], "笑", "happy", [], first_sig)
    assert processor._get_valid_cache("same-image", second_sig) is None
