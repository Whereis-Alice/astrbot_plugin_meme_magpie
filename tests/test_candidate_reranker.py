"""Candidate decisions use framework stubs; never real model calls."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.search.candidate_reranker import MemeCandidateReranker, MemeDecision
from core.events.meme_sender_engine import MemeSenderEngine


class Event:
    unified_msg_origin = "test:group:1"

    def __init__(self):
        self.extra = {}

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def get_result(self):
        return SimpleNamespace(get_plain_text=lambda: "辛苦了")


def setup_plugin(tmp_path, answer='{"choice":"emoji_1"}'):
    image = tmp_path / "meme.png"
    image.write_bytes(b"test image")
    rows = {str(image): {"path": str(image), "hash": "same", "category": "happy", "work": "作品", "desc": "辛苦了", "tags": ["安慰"]}}
    calls = []

    async def search(query, **kwargs):
        return [(str(image), "辛苦了", "happy", "安慰")]

    async def llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text=answer)

    async def provider(origin):
        assert origin == Event.unified_msg_origin
        return "current-chat"

    plugin = SimpleNamespace(
        base_dir=tmp_path,
        plugin_config=SimpleNamespace(enable_meme_rerank=True, meme_rerank_timeout=1, emotion_analysis_provider_id=""),
        is_send_enabled_for_event=lambda event: True,
        db_service=SimpleNamespace(get_emoji=rows.get),
        meme_selector=SimpleNamespace(smart_search=search, is_path_allowed_for_event=lambda path, event: True),
        context=SimpleNamespace(llm_generate=llm, get_current_chat_provider_id=provider),
        enable_natural_emotion_analysis=False, meme_send_delay=0, auto_meme_attach_timeout=1,
    )
    return plugin, rows, calls, str(image)


def test_decision_contains_no_local_paths_in_prompt(tmp_path):
    plugin, rows, calls, path = setup_plugin(tmp_path)
    decision = asyncio.run(MemeCandidateReranker(plugin).choose(Event(), "辛苦了", "今天好累"))
    assert decision == MemeDecision(path, "same")
    assert calls[0]["chat_provider_id"] == "current-chat"
    assert path not in calls[0]["prompt"] and "path" not in calls[0]["prompt"]
    body = json.loads(calls[0]["prompt"].split("\n")[-1])
    assert body["candidates"][0]["work"] == "作品"
    assert len(rows) == 1


def test_configured_provider_and_fenced_json(tmp_path):
    plugin, _, calls, path = setup_plugin(tmp_path, '```json\n{"choice":"emoji_1"}\n```')
    plugin.plugin_config.emotion_analysis_provider_id = "small-model"
    assert asyncio.run(MemeCandidateReranker(plugin).choose(Event(), "reply")).path == path
    assert calls[0]["chat_provider_id"] == "small-model"


@pytest.mark.parametrize("answer", ['{"choice":"none"}', '{"choice":"emoji_999"}', '{}', 'garbage', '{"choice":1}'])
def test_abstention_is_distinct_from_invalid_output(tmp_path, answer):
    plugin, _, _, _ = setup_plugin(tmp_path, answer)
    decision = asyncio.run(MemeCandidateReranker(plugin).choose(Event(), "reply"))
    assert decision == (MemeDecision() if '"none"' in answer else None)


def test_disabled_does_no_search_or_model_call(tmp_path):
    plugin, _, calls, _ = setup_plugin(tmp_path)
    plugin.plugin_config.enable_meme_rerank = False
    plugin.meme_selector = None
    assert asyncio.run(MemeCandidateReranker(plugin).choose(Event(), "reply")) is None
    assert not calls


@pytest.mark.parametrize("change", ["missing", "scope", "disabled", "hash"])
def test_selected_candidate_is_rechecked(tmp_path, change):
    plugin, rows, _, path = setup_plugin(tmp_path)
    async def llm(**kwargs):
        if change == "missing":
            rows.clear()
        elif change == "scope":
            plugin.meme_selector.is_path_allowed_for_event = lambda path, event: False
        elif change == "disabled":
            plugin.is_send_enabled_for_event = lambda event: False
        else:
            rows[path]["hash"] = "changed"
        return SimpleNamespace(completion_text='{"choice":"emoji_1"}')
    plugin.context.llm_generate = llm
    assert asyncio.run(MemeCandidateReranker(plugin).choose(Event(), "reply")) == MemeDecision()


def test_timeout_falls_back_but_cancellation_propagates(tmp_path):
    plugin, _, _, _ = setup_plugin(tmp_path)
    plugin.plugin_config.meme_rerank_timeout = 0.01
    async def slow(**kwargs):
        await asyncio.sleep(1)
    plugin.context.llm_generate = slow
    assert asyncio.run(MemeCandidateReranker(plugin).choose(Event(), "reply")) is None
    async def cancelled(**kwargs):
        raise asyncio.CancelledError
    plugin.context.llm_generate = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(MemeCandidateReranker(plugin).choose(Event(), "reply"))


def test_no_visible_candidates_skips_model(tmp_path):
    plugin, _, calls, _ = setup_plugin(tmp_path)
    plugin.meme_selector.is_path_allowed_for_event = lambda path, event: False
    assert asyncio.run(MemeCandidateReranker(plugin).choose(Event(), "reply")) == MemeDecision()
    assert not calls


@pytest.mark.parametrize("attach", [True, False])
def test_none_never_falls_back_or_marks_sent(tmp_path, attach):
    plugin, _, calls, _ = setup_plugin(tmp_path, '{"choice":"none"}')
    engine = MemeSenderEngine(plugin)
    event = Event()
    async def unexpected(*args, **kwargs):
        pytest.fail("Abstention must not randomly pick, attach or send")
    plugin.meme_selector.attach_emoji_to_result = unexpected
    engine.resolve_emoji_query = unexpected
    engine.try_send_emoji = unexpected
    if attach:
        assert asyncio.run(engine.attach_emoji_to_result(event, object(), "reply", [])) is True
    else:
        asyncio.run(engine.async_analyze_and_send_emoji(event, "reply", []))
    assert len(calls) == 1
    assert not engine.emoji_turn_state(event).is_active_sent()
    assert not engine._auto_emoji_cooldowns


def test_attach_failure_reuses_decision_in_separate_fallback(tmp_path):
    plugin, _, calls, path = setup_plugin(tmp_path)
    engine = MemeSenderEngine(plugin)
    event = Event()
    sent = []
    async def failed_attach(*args):
        return None
    async def send(event, chosen, text):
        sent.append(chosen)
        return True
    plugin.meme_selector.attach_emoji_to_result = failed_attach
    plugin.meme_selector.attach_selected_emoji = failed_attach
    plugin.meme_selector.send_emoji_with_text = send
    async def run():
        assert await engine.attach_emoji_to_result(event, object(), "reply", []) is False
        await engine.async_analyze_and_send_emoji(event, "reply", [])
    asyncio.run(run())
    assert sent == [path] and len(calls) == 1
    assert engine.emoji_turn_state(event).is_active_sent()
