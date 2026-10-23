"""Provider fallback, cancellation and cache behavior without AstrBot or network I/O."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.processing.classification_parser import ClassificationResponseError
from core.processing.image_processor_service import ImageProcessorService


VALID_RESPONSE = '{"category":"happy","description":"一只笑着的猫","tags":["猫"]}'


def _provider(provider_id, modalities=None):
    return SimpleNamespace(
        meta=lambda: SimpleNamespace(id=provider_id),
        provider_config={"modalities": modalities or []},
    )


def _make_processor(
    tmp_path,
    script=None,
    *,
    provider="",
    providers=(),
    global_provider="",
    current_provider="",
    attempts=1,
    timeout=60.0,
):
    queues = {key: list(values) for key, values in (script or {}).items()}
    framework = {
        "provider_settings": {"default_image_caption_provider_id": global_provider}
    }

    async def generate(**kwargs):
        queue = queues.get(kwargs["chat_provider_id"], [])
        value = queue.pop(0) if queue else VALID_RESPONSE
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(completion_text=value)

    llm = AsyncMock(side_effect=generate)
    current = AsyncMock(return_value=_provider(current_provider))
    config = SimpleNamespace(
        raw_dir=tmp_path / "raw",
        categories_dir=tmp_path / "categories",
        categories=["happy", "sad", "confused"],
        category_info={},
        content_filtration=False,
        vision_provider_id=provider,
        vision_provider_ids=list(providers),
        vision_timeout_seconds=timeout,
        normalize_category_strict=lambda raw: raw
        if raw in {"happy", "sad", "confused"} else None,
        closest_category=lambda raw: "confused",
    )
    plugin = SimpleNamespace(
        plugin_config=config,
        context=SimpleNamespace(
            get_config=lambda: framework,
            get_using_provider_async=current,
            llm_generate=llm,
        ),
        vision_max_retries=attempts,
        vision_retry_delay=0,
    )
    processor = ImageProcessorService(plugin)
    plugin.image_processor_service = processor
    path = tmp_path / "test.png"
    path.write_bytes(b"image decoder is mocked in these provider tests")
    processor._vlm_call_service._prepare_image_for_vlm = AsyncMock(
        return_value=(str(path), None)
    )
    return processor, llm, current, framework, str(path)


def _requested_providers(llm):
    return [call.kwargs["chat_provider_id"] for call in llm.await_args_list]


def test_explicit_models_keep_order_remove_blanks_and_duplicates(tmp_path):
    processor, _llm, current, _framework, _path = _make_processor(
        tmp_path,
        provider=" legacy ",
        providers=[" fallback-b ", "legacy", "", "fallback-c", "fallback-b"],
        global_provider="global-vlm",
        current_provider="current-vlm",
    )

    async def run():
        service = processor._vlm_call_service
        assert await service._resolve_vision_providers() == [
            "legacy", "fallback-b", "fallback-c"
        ]
        assert await processor._resolve_vision_provider() == "legacy"

    asyncio.run(run())
    current.assert_not_awaited()


def test_multiple_models_work_without_legacy_single_selection(tmp_path):
    processor, _llm, _current, _framework, _path = _make_processor(
        tmp_path, providers=["backup-a", "backup-b"]
    )
    assert asyncio.run(processor._vlm_call_service._resolve_vision_providers()) == [
        "backup-a", "backup-b"
    ]


@pytest.mark.parametrize("event", [None, SimpleNamespace(unified_msg_origin="qq:Group:42")])
def test_unconfigured_plugin_and_global_default_use_current_chat_model(tmp_path, event):
    processor, llm, current, _framework, path = _make_processor(
        tmp_path, current_provider="current-vlm"
    )
    assert asyncio.run(processor.classify_image(event, path))[0] == "happy"
    assert _requested_providers(llm) == ["current-vlm"]
    expected_origin = event.unified_msg_origin if event is not None else None
    assert current.await_count >= 1
    for call in current.await_args_list:
        supplied_origins = call.args + tuple(call.kwargs.values())
        assert supplied_origins == (expected_origin,)


def test_implicit_models_prefer_global_caption_and_deduplicate_current(tmp_path):
    processor, _llm, current, _framework, _path = _make_processor(
        tmp_path, global_provider=" global-vlm ", current_provider="current-vlm"
    )

    async def run():
        service = processor._vlm_call_service
        assert await service._resolve_vision_providers() == ["global-vlm", "current-vlm"]
        current.return_value = _provider("global-vlm")
        assert await service._resolve_vision_providers() == ["global-vlm"]

    asyncio.run(run())


def test_current_provider_lookup_failure_keeps_configured_global_model(tmp_path):
    processor, llm, current, _framework, path = _make_processor(
        tmp_path, global_provider="global-vlm"
    )
    current.side_effect = RuntimeError("session provider unavailable")
    assert asyncio.run(processor.classify_image(None, path))[0] == "happy"
    assert _requested_providers(llm) == ["global-vlm"]


def test_automatic_discovery_uses_only_explicit_image_capable_models(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(tmp_path)
    processor.plugin.context.get_all_providers = lambda: [
        _provider("text-only", ["text"]),
        _provider("unknown-capabilities"),
        _provider("image-vlm", ["text", "image"]),
    ]
    assert asyncio.run(processor.classify_image(None, path))[0] == "happy"
    assert _requested_providers(llm) == ["image-vlm"]


def test_automatic_discovery_follows_defaults_without_duplicate_calls(tmp_path):
    processor, _llm, _current, _framework, _path = _make_processor(
        tmp_path, global_provider="global-vlm", current_provider="current-vlm"
    )
    processor.plugin.context.get_all_providers = lambda: [
        _provider("current-vlm", ["image"]),
        _provider("discovered-vlm", ["image"]),
        _provider("global-vlm", ["image"]),
    ]
    assert asyncio.run(processor._vlm_call_service._resolve_vision_providers()) == [
        "global-vlm", "current-vlm", "discovered-vlm"
    ]


def test_no_available_model_reports_configuration_error_before_preprocessing(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(tmp_path)
    with pytest.raises(ValueError, match="视觉模型"):
        asyncio.run(processor.classify_image(None, path))
    llm.assert_not_awaited()
    processor._vlm_call_service._prepare_image_for_vlm.assert_not_awaited()


def test_older_context_current_provider_id_fallback_receives_event_origin(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(tmp_path)
    del processor.plugin.context.get_using_provider_async
    current_id = AsyncMock(return_value="session-vlm")
    processor.plugin.context.get_current_chat_provider_id = current_id
    event = SimpleNamespace(unified_msg_origin="qq:Group:42")
    assert asyncio.run(processor.classify_image(event, path))[0] == "happy"
    current_id.assert_awaited_once_with(event.unified_msg_origin)
    assert _requested_providers(llm) == ["session-vlm"]


@pytest.mark.parametrize("first", [RuntimeError("provider down"), "{}", "", "service busy"])
def test_failed_or_invalid_first_model_falls_through_to_valid_second(tmp_path, first):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path,
        {"first": [first], "second": [VALID_RESPONSE]},
        providers=["first", "second"],
    )
    assert asyncio.run(processor.classify_image(None, path))[0] == "happy"
    assert _requested_providers(llm) == ["first", "second"]
    processor._vlm_call_service._prepare_image_for_vlm.assert_awaited_once()


def test_each_model_uses_its_finite_retry_budget_before_fallback(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path,
        {"first": ["{}", "{}"], "second": [VALID_RESPONSE]},
        providers=["first", "second"],
        attempts=2,
    )
    assert asyncio.run(processor.classify_image(None, path))[0] == "happy"
    assert _requested_providers(llm) == ["first", "first", "second"]


def test_all_invalid_models_raise_classification_error(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path,
        {"first": ["{}"], "second": ['{"category":"happy"}']},
        providers=["first", "second"],
    )
    with pytest.raises(ClassificationResponseError):
        asyncio.run(processor.classify_image(None, path))
    assert _requested_providers(llm) == ["first", "second"]


@pytest.mark.parametrize("failure", ["{}", RuntimeError("provider unavailable")])
def test_all_provider_failures_never_enter_image_cache(tmp_path, failure):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path,
        {"first": [failure], "second": [failure]},
        providers=["first", "second"],
    )
    processor._compute_hash = AsyncMock(return_value="failed-image-hash")
    processor._phash_service.compute_phash = AsyncMock(return_value="")
    processor._is_duplicate_or_blacklisted = AsyncMock(return_value=False)
    processor._move_to_raw = AsyncMock(return_value=path)
    processor._handle_classification_result = AsyncMock(return_value=(False, None))

    async def run():
        try:
            result = await processor.process_image(None, path)
        except Exception:
            result = None
        assert result in (None, (False, None))
        assert processor._image_cache == {}
        assert processor._processing_hashes == set()

    asyncio.run(run())
    assert _requested_providers(llm) == ["first", "second"]


def test_moderation_rejection_finishes_without_asking_next_model(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path,
        {"first": ['{"approved":false,"reason":"不适合"}']},
        providers=["first", "second"],
        attempts=3,
    )
    assert asyncio.run(processor.classify_image(None, path))[0] == "过滤不通过"
    assert _requested_providers(llm) == ["first"]


def test_cancellation_propagates_and_cleans_prepared_temp_image(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path,
        {"first": [asyncio.CancelledError()]},
        providers=["first", "second"],
    )
    prepared = tmp_path / "prepared.png"
    prepared.write_bytes(b"prepared image")
    processor._vlm_call_service._prepare_image_for_vlm.return_value = (str(prepared), None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(processor.classify_image(None, path))
    assert _requested_providers(llm) == ["first"]
    assert not prepared.exists()


def test_timeout_cancels_stuck_request_and_tries_next_model(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path, providers=["stuck", "second"], timeout=0.01
    )
    cancelled = []

    async def generate(**kwargs):
        if kwargs["chat_provider_id"] == "stuck":
            try:
                await asyncio.Future()
            finally:
                cancelled.append("stuck")
        return SimpleNamespace(completion_text=VALID_RESPONSE)

    llm.side_effect = generate

    async def run():
        return await asyncio.wait_for(processor.classify_image(None, path), timeout=1.0)

    assert asyncio.run(run())[0] == "happy"
    assert cancelled == ["stuck"]
    assert _requested_providers(llm) == ["stuck", "second"]


def test_preprocessing_and_mime_temp_files_survive_until_fallback_finishes(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path, providers=["first", "second"]
    )
    prepared = tmp_path / "prepared.png"
    converted = tmp_path / "converted.jpg"
    prepared.write_bytes(b"prepared image")
    converted.write_bytes(b"converted image")
    service = processor._vlm_call_service
    service._prepare_image_for_vlm.return_value = (str(prepared), None)
    service._force_convert_for_vlm = AsyncMock(return_value=str(converted))
    first_calls = 0

    async def generate(**kwargs):
        nonlocal first_calls
        if kwargs["chat_provider_id"] == "first":
            first_calls += 1
            if first_calls == 1:
                raise RuntimeError("unsupported mime")
            raise RuntimeError("provider unavailable")
        assert prepared.exists()
        assert converted.exists()
        return SimpleNamespace(completion_text=VALID_RESPONSE)

    llm.side_effect = generate
    assert asyncio.run(processor.classify_image(None, path))[0] == "happy"
    assert _requested_providers(llm) == ["first", "first", "second"]
    assert not prepared.exists()
    assert not converted.exists()
    service._prepare_image_for_vlm.assert_awaited_once()


def test_live_model_list_update_changes_next_request_and_cache_signature(tmp_path):
    processor, llm, _current, _framework, path = _make_processor(
        tmp_path, provider="first", providers=["old-backup"]
    )

    async def run():
        old_signature = await processor._resolve_vision_model_signature()
        processor._put_image_cache(
            "cached-image", "happy", ["猫"], "猫", "happy", [], old_signature
        )
        processor.update_config(vision_provider_ids=["new-backup"])
        new_signature = await processor._resolve_vision_model_signature()
        assert old_signature != new_signature
        assert processor._get_valid_cache("cached-image", new_signature) is None
        llm.side_effect = [RuntimeError("first unavailable"), SimpleNamespace(
            completion_text=VALID_RESPONSE
        )]
        assert (await processor.classify_image(None, path))[0] == "happy"
        assert _requested_providers(llm) == ["first", "new-backup"]
        processor.update_config(vision_provider_id="", vision_provider_ids=[])
        processor.plugin.context.get_using_provider_async.return_value = _provider("current-vlm")
        assert await processor._resolve_vision_provider() == "current-vlm"

    asyncio.run(run())


def test_single_model_signature_preserves_existing_cache_keys(tmp_path):
    processor, _llm, _current, _framework, _path = _make_processor(
        tmp_path, provider="legacy"
    )
    assert asyncio.run(processor._resolve_vision_model_signature()) == "legacy"


def test_changing_fallback_order_changes_cache_signature(tmp_path):
    processor, _llm, _current, _framework, _path = _make_processor(
        tmp_path, providers=["first", "second"]
    )

    async def run():
        signature = await processor._resolve_vision_model_signature()
        processor.update_config(vision_provider_ids=["second", "first"])
        assert await processor._resolve_vision_model_signature() != signature

    asyncio.run(run())
