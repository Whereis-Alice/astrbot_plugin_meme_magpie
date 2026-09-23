"""VLM 热更新与有限响应校验；共享 conftest 的 AstrBot stub，无外部请求。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.processing.classification_parser import (
    ClassificationParser,
    ClassificationResponseError,
)
from core.processing.image_processor_service import ImageProcessorService


VALID_RESPONSE = '{"category":"happy","description":"一只笑着的猫","tags":["猫"]}'


def _make_processor(tmp_path, script=(), *, provider="vlm-a", attempts=3):
    responses = list(script)
    framework = {"provider_settings": {"default_image_caption_provider_id": "global-a"}}

    async def generate(**kwargs):
        item = responses.pop(0) if responses else VALID_RESPONSE
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(completion_text=item)

    llm = AsyncMock(side_effect=generate)
    config = SimpleNamespace(
        raw_dir=tmp_path / "raw",
        categories_dir=tmp_path / "categories",
        categories=["happy", "sad", "confused"],
        category_info={},
        content_filtration=False,
        vision_provider_id=provider,
        normalize_category_strict=lambda raw: {"开心": "happy"}.get(raw, raw)
        if raw in {"happy", "sad", "confused", "开心"} else None,
        closest_category=lambda raw: "confused",
    )
    plugin = SimpleNamespace(
        plugin_config=config,
        context=SimpleNamespace(get_config=lambda: framework, llm_generate=llm),
        vision_max_retries=attempts,
        vision_retry_delay=0,
    )
    processor = ImageProcessorService(plugin)
    plugin.image_processor_service = processor
    path = tmp_path / "test.png"
    path.write_bytes(b"the image decoder is mocked in these response tests")
    processor._vlm_call_service._prepare_image_for_vlm = AsyncMock(return_value=(str(path), None))
    return processor, llm, framework, str(path)


@pytest.mark.parametrize(
    "response",
    [
        "", "   ", "{}", "[]", "null", "the service is temporarily busy",
        '{"approved":true}', '{"category":"happy"}',
        '{"description":"a cat"}', '{"category":[],"description":"a cat"}',
        '{"category":"happy","description":{}}',
        '{"category":"happy","description":" "}',
        "happy", "happy|cat", "happy|cat|",
    ],
)
def test_invalid_classification_never_becomes_fallback_success(response):
    parser = ClassificationParser()
    with pytest.raises(ClassificationResponseError):
        parser._parse_classification_response(response, "test.png")


@pytest.mark.parametrize(
    "response",
    ['{"approved":false}', '{"approved":"false","reason":"不适合"}', "审核不通过"],
)
def test_moderation_rejections_remain_valid_results(response):
    parsed = ClassificationParser()._parse_classification_response(response, "test.png")
    assert parsed == ("过滤不通过", [], "", "过滤不通过", [], "", [])


def test_complete_legacy_payload_and_aliases_remain_supported(tmp_path):
    processor, _llm, _framework, path = _make_processor(tmp_path)
    parsed = processor._parse_classification_response("开心|猫、笑|猫在笑|真开心|哈哈", path)
    assert parsed[0] == "happy"
    assert parsed[1] == ["猫", "笑"]
    assert parsed[2] == "猫在笑"
    assert parsed[5] == "哈哈"
    with pytest.raises(ClassificationResponseError):
        processor._parse_classification_response("not a category|cat|a cat", path)


def test_fenced_json_and_optional_fields_remain_supported():
    response = f"分析结果如下：\n```json\n{VALID_RESPONSE}\n```"
    parsed = ClassificationParser()._parse_classification_response(response, "test.png")
    assert parsed[0] == "happy"
    assert parsed[2] == "一只笑着的猫"


def test_provider_update_changes_both_cache_signature_and_actual_request(tmp_path):
    processor, llm, framework, path = _make_processor(tmp_path)

    async def run():
        assert await processor._resolve_vision_provider() == "vlm-a"
        processor.update_config(vision_provider_id=" vlm-b ")
        assert await processor._resolve_vision_provider() == "vlm-b"
        await processor.classify_image(None, path)
        assert llm.call_args.kwargs["chat_provider_id"] == "vlm-b"
        processor.update_config(vision_provider_id="")
        await processor.classify_image(None, path)
        assert llm.call_args.kwargs["chat_provider_id"] == "global-a"
        framework["provider_settings"]["default_image_caption_provider_id"] = "global-b"
        assert await processor._resolve_vision_provider() == "global-b"
        await processor.classify_image(None, path)
        assert llm.call_args.kwargs["chat_provider_id"] == "global-b"

    asyncio.run(run())


def test_missing_framework_provider_is_not_cached_forever(tmp_path):
    processor, _llm, framework, _path = _make_processor(tmp_path, provider="")

    async def run():
        framework["provider_settings"]["default_image_caption_provider_id"] = ""
        assert await processor._resolve_vision_provider() is None
        framework["provider_settings"]["default_image_caption_provider_id"] = "configured-later"
        assert await processor._resolve_vision_provider() == "configured-later"

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["{}", "service busy", '{"category":"happy"}', ""])
def test_invalid_response_retries_within_one_budget_then_succeeds(tmp_path, invalid):
    processor, llm, _framework, path = _make_processor(tmp_path, [invalid, VALID_RESPONSE])
    result = asyncio.run(processor.classify_image(None, path, known={"character": "已知角色"}))
    assert result[0] == "happy"
    assert llm.call_count == 2
    assert "已知角色" in llm.call_args.kwargs["prompt"]
    processor._vlm_call_service._prepare_image_for_vlm.assert_awaited_once()


@pytest.mark.parametrize("invalid", ["{}", "service busy", '{"category":"happy"}', ""])
def test_invalid_response_exhaustion_raises_and_does_not_cache(tmp_path, invalid):
    processor, llm, _framework, path = _make_processor(tmp_path, [invalid] * 5, attempts=2)
    with pytest.raises(ClassificationResponseError, match="已尝试2次"):
        asyncio.run(processor.classify_image(None, path))
    assert llm.call_count == 2
    assert processor._image_cache == {}


def test_approved_false_does_not_retry(tmp_path):
    processor, llm, _framework, path = _make_processor(tmp_path, ['{"approved":false}'])
    assert asyncio.run(processor.classify_image(None, path))[0] == "过滤不通过"
    assert llm.call_count == 1


def test_response_validation_keeps_one_mime_conversion_fallback(tmp_path):
    processor, llm, _framework, path = _make_processor(
        tmp_path, [RuntimeError("unsupported mime"), "{}", VALID_RESPONSE]
    )
    converted = tmp_path / "converted.png"
    converted.write_bytes(b"mocked conversion output")
    processor._vlm_call_service._force_convert_for_vlm = AsyncMock(return_value=str(converted))
    assert asyncio.run(processor.classify_image(None, path))[0] == "happy"
    assert llm.call_count == 3
    processor._vlm_call_service._force_convert_for_vlm.assert_awaited_once()
    assert not converted.exists()
    assert all(call.kwargs["image_urls"] == [str(converted)] for call in llm.call_args_list[1:])


@pytest.mark.parametrize("attempts,expected", [(0, 1), (-1, 1), (1000, 10)])
def test_retry_configuration_has_a_finite_positive_bound(tmp_path, attempts, expected):
    processor, llm, _framework, path = _make_processor(tmp_path, ["{}"] * 20, attempts=attempts)
    with pytest.raises(ClassificationResponseError):
        asyncio.run(processor.classify_image(None, path))
    assert llm.call_count == expected
