"""attach 投递模式（表情并入本条回复）与分段回复兼容路径的测试。

覆盖三层：
- MemeSmartSelectService：路径比对、兼容目录生成/清理、attach 幂等与门控
- MemeSenderEngine：超时兜底与成功后的副作用（冷却、本轮已发标记）
- Main：投递模式归一化、文转图时让位给独立消息模式
"""

import asyncio
import importlib
import os
import sys
import types
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1].name
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_svc_module = importlib.import_module(f"{_PKG}.core.search.meme_smart_select_service")
MemeSmartSelectService = _svc_module.MemeSmartSelectService
MemeSenderEngine = importlib.import_module(
    f"{_PKG}.core.events.meme_sender_engine"
).MemeSenderEngine
Main = importlib.import_module(f"{_PKG}.main").Main


class _Plain:
    """纯文本组件桩。故意也带 path 字段，用来验证 isinstance 过滤真的生效。"""

    def __init__(self, text: str = "", path: str = ""):
        self.text = text
        self.path = path


class _Result:
    def __init__(self, text: str = "hi", chain=None):
        self.chain = [_Plain(text)] if chain is None else chain

    def is_llm_result(self):
        return True

    def get_plain_text(self):
        return "".join(str(getattr(comp, "text", "")) for comp in self.chain)


class _Event:
    def __init__(self):
        self.sent: list = []
        self._extras: dict = {}

    def get_session_id(self):
        return "session-attach"

    def get_extra(self, key=None, default=None):
        if key is None:
            return dict(self._extras)
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_message_str(self):
        return "user says"

    async def send(self, message):
        self.sent.append(message)


def _image(path: str = "", file: str = ""):
    # 其它测试模块会重装 astrbot stubs，Image 类的身份可能被换掉。
    # 这里每次都从服务模块现取，保证 isinstance 判断和被测代码看到的是同一个类。
    img = _svc_module.MessageImage()
    if path:
        img.path = path
    if file:
        img.file = file
    return img


def _service(**plugin_kwargs):
    """不走 __init__，避免顺带构造 EmbeddingService。"""
    svc = MemeSmartSelectService.__new__(MemeSmartSelectService)
    svc.plugin = types.SimpleNamespace(**plugin_kwargs)
    svc._selector = None
    svc._search_engine = None
    svc._embedding_service = None
    return svc


def _abs(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class TestSameLocalPath:
    def test_empty_inputs(self, tmp_path):
        target = _abs(tmp_path / "a.png")
        assert MemeSmartSelectService._same_local_path("", target) is False
        assert MemeSmartSelectService._same_local_path(None, target) is False
        assert MemeSmartSelectService._same_local_path(str(tmp_path / "a.png"), "") is False

    def test_plain_path_match(self, tmp_path):
        path = str(tmp_path / "a.png")
        assert MemeSmartSelectService._same_local_path(path, _abs(path)) is True

    def test_redundant_segments_normalized(self, tmp_path):
        target = _abs(tmp_path / "a.png")
        noisy = os.path.join(str(tmp_path), ".", "sub", "..", "a.png")
        assert MemeSmartSelectService._same_local_path(noisy, target) is True

    def test_base64_never_matches(self, tmp_path):
        target = _abs(tmp_path / "a.png")
        assert MemeSmartSelectService._same_local_path("base64://AAAA", target) is False

    def test_different_file(self, tmp_path):
        target = _abs(tmp_path / "a.png")
        assert MemeSmartSelectService._same_local_path(str(tmp_path / "b.png"), target) is False

    @pytest.mark.skipif(os.name != "nt", reason="file:/// 剥离按 Windows 盘符路径设计")
    def test_file_uri_prefix_stripped(self, tmp_path):
        path = os.path.abspath(str(tmp_path / "a.png"))
        uri = "file:///" + path.replace(os.sep, "/")
        assert MemeSmartSelectService._same_local_path(uri, _abs(path)) is True


class TestResultHasImagePath:
    def test_non_list_chain(self, tmp_path):
        result = types.SimpleNamespace(chain=None)
        assert MemeSmartSelectService._result_has_image_path(result, str(tmp_path / "a.png")) is False

    def test_missing_chain_attr(self, tmp_path):
        assert MemeSmartSelectService._result_has_image_path(object(), str(tmp_path / "a.png")) is False

    def test_empty_emoji_path(self, tmp_path):
        result = _Result(chain=[_image(path=str(tmp_path / "a.png"))])
        assert MemeSmartSelectService._result_has_image_path(result, "") is False

    def test_match_by_path_field(self, tmp_path):
        path = str(tmp_path / "a.png")
        result = _Result(chain=[_Plain("hi"), _image(path=path)])
        assert MemeSmartSelectService._result_has_image_path(result, path) is True

    def test_match_by_file_field(self, tmp_path):
        path = str(tmp_path / "a.png")
        result = _Result(chain=[_image(file=path)])
        assert MemeSmartSelectService._result_has_image_path(result, path) is True

    def test_non_image_component_skipped(self, tmp_path):
        path = str(tmp_path / "a.png")
        result = _Result(chain=[_Plain("hi", path=path)])
        assert MemeSmartSelectService._result_has_image_path(result, path) is False

    def test_other_image_not_matched(self, tmp_path):
        result = _Result(chain=[_image(path=str(tmp_path / "b.png"))])
        assert MemeSmartSelectService._result_has_image_path(result, str(tmp_path / "a.png")) is False


def _boom(*args, **kwargs):
    raise OSError("boom")


class TestSplitCompatPath:
    def test_marker_constants(self):
        assert "plugin_stealer" in MemeSmartSelectService.SPLIT_COMPAT_DIRNAME
        assert MemeSmartSelectService.SPLIT_COMPAT_KEEP == 64

    def test_disabled_returns_original(self, tmp_path):
        src = tmp_path / "a.png"
        src.write_bytes(b"x")
        svc = _service(auto_meme_attach_compat_split=False, base_dir=tmp_path)
        assert svc._split_compat_path(str(src)) == str(src)
        assert not (tmp_path / MemeSmartSelectService.SPLIT_COMPAT_DIRNAME).exists()

    def test_flag_absent_returns_original(self, tmp_path):
        src = tmp_path / "a.png"
        src.write_bytes(b"x")
        svc = _service(base_dir=tmp_path)
        assert svc._split_compat_path(str(src)) == str(src)

    def test_missing_file_returns_original(self, tmp_path):
        svc = _service(auto_meme_attach_compat_split=True, base_dir=tmp_path)
        ghost = str(tmp_path / "ghost.png")
        assert svc._split_compat_path(ghost) == ghost
        assert svc._split_compat_path("") == ""

    def test_no_base_dir_returns_original(self, tmp_path):
        src = tmp_path / "a.png"
        src.write_bytes(b"x")
        svc = _service(auto_meme_attach_compat_split=True, base_dir=None)
        assert svc._split_compat_path(str(src)) == str(src)

    def test_creates_marked_twin(self, tmp_path):
        src = tmp_path / "data" / "a.png"
        src.parent.mkdir()
        src.write_bytes(b"x")
        base = tmp_path / "base"
        base.mkdir()
        svc = _service(auto_meme_attach_compat_split=True, base_dir=base)

        out = svc._split_compat_path(str(src))
        assert out != str(src)
        assert "plugin_stealer" in out.replace(os.sep, "/").lower()
        assert os.path.basename(out) == "a.png"
        assert Path(out).read_bytes() == b"x"
        # 原文件与原目录都不动
        assert src.exists() and src.read_bytes() == b"x"
        # 第二次调用复用同一个文件，不重复建
        assert svc._split_compat_path(str(src)) == out

    def test_link_failure_falls_back_to_copy(self, tmp_path, monkeypatch):
        src = tmp_path / "a.png"
        src.write_bytes(b"x")
        base = tmp_path / "base"
        base.mkdir()
        svc = _service(auto_meme_attach_compat_split=True, base_dir=base)
        monkeypatch.setattr(os, "link", _boom)

        out = svc._split_compat_path(str(src))
        assert out != str(src)
        assert Path(out).read_bytes() == b"x"

    def test_unexpected_error_returns_original(self, tmp_path, monkeypatch):
        src = tmp_path / "a.png"
        src.write_bytes(b"x")
        svc = _service(auto_meme_attach_compat_split=True, base_dir=tmp_path / "base")
        monkeypatch.setattr(os, "makedirs", _boom)
        assert svc._split_compat_path(str(src)) == str(src)


class TestPruneSplitCompatDir:
    @staticmethod
    def _fill(directory: Path, count: int) -> None:
        for index in range(count):
            item = directory / f"f{index}.png"
            item.write_bytes(b"x")
            os.utime(item, (1_000_000 + index, 1_000_000 + index))

    def test_under_keep_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(MemeSmartSelectService, "SPLIT_COMPAT_KEEP", 3)
        self._fill(tmp_path, 3)
        _service()._prune_split_compat_dir(str(tmp_path))
        assert len(list(tmp_path.iterdir())) == 3

    def test_keeps_newest_by_mtime(self, tmp_path, monkeypatch):
        monkeypatch.setattr(MemeSmartSelectService, "SPLIT_COMPAT_KEEP", 3)
        self._fill(tmp_path, 6)
        _service()._prune_split_compat_dir(str(tmp_path))
        assert sorted(item.name for item in tmp_path.iterdir()) == ["f3.png", "f4.png", "f5.png"]

    def test_subdirectories_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(MemeSmartSelectService, "SPLIT_COMPAT_KEEP", 1)
        self._fill(tmp_path, 2)
        (tmp_path / "sub").mkdir()
        _service()._prune_split_compat_dir(str(tmp_path))
        names = sorted(item.name for item in tmp_path.iterdir())
        assert names == ["f1.png", "sub"]

    def test_missing_dir_is_silent(self, tmp_path):
        _service()._prune_split_compat_dir(str(tmp_path / "nope"))


def _attach_service(*, picked, append_ok=True, compat=None):
    """装配 service.attach_emoji_to_result 需要的三个协作方法。"""
    svc = _service()
    log: dict = {"picked": [], "appended": []}

    async def pick_emoji_only(event, emotions, cleaned_text):
        log["picked"].append((list(emotions or []), cleaned_text))
        return picked

    async def append_emoji(event, result, path):
        log["appended"].append(path)
        return append_ok

    svc.pick_emoji_only = pick_emoji_only
    svc._append_emoji_to_result = append_emoji
    svc._split_compat_path = lambda path: compat or path
    return svc, log


class TestServiceAttachEmojiToResult:
    @pytest.mark.asyncio
    async def test_no_emoji_picked(self):
        svc, log = _attach_service(picked=None)
        assert await svc.attach_emoji_to_result(_Event(), _Result(), ["开心"], "哈哈") is None
        assert log["appended"] == []

    @pytest.mark.asyncio
    async def test_happy_path(self, tmp_path):
        path = str(tmp_path / "a.png")
        svc, log = _attach_service(picked=path)
        result = _Result()
        assert await svc.attach_emoji_to_result(_Event(), result, ["开心"], "哈哈") == path
        assert log["appended"] == [path]
        assert log["picked"] == [(["开心"], "哈哈")]

    @pytest.mark.asyncio
    async def test_compat_path_is_the_one_attached(self, tmp_path):
        path = str(tmp_path / "a.png")
        compat = str(tmp_path / "plugin_stealer_split_compat" / "a.png")
        svc, log = _attach_service(picked=path, compat=compat)
        assert await svc.attach_emoji_to_result(_Event(), _Result(), [], "哈哈") == compat
        assert log["appended"] == [compat]

    @pytest.mark.asyncio
    async def test_idempotent_when_already_in_chain(self, tmp_path):
        path = str(tmp_path / "a.png")
        svc, log = _attach_service(picked=path)
        result = _Result(chain=[_Plain("hi"), _image(path=path)])
        assert await svc.attach_emoji_to_result(_Event(), result, [], "哈哈") == path
        assert log["appended"] == []

    @pytest.mark.asyncio
    async def test_append_failure_returns_none(self, tmp_path):
        path = str(tmp_path / "a.png")
        svc, log = _attach_service(picked=path, append_ok=False)
        assert await svc.attach_emoji_to_result(_Event(), _Result(), [], "哈哈") is None
        assert log["appended"] == [path]


def _pick_service(*, group_allowed=True, active_sent=False, selected="a.png"):
    svc = _service()
    calls: list[dict] = []

    async def select_emoji(primary, cleaned_text, event=None, extra_categories=None):
        calls.append(
            {
                "primary": primary,
                "text": cleaned_text,
                "extra": list(extra_categories or []),
            }
        )
        return selected

    svc._selector = types.SimpleNamespace(_check_group_allowed=lambda event: group_allowed)
    svc.plugin = types.SimpleNamespace(
        _emoji_turn_state=lambda event: types.SimpleNamespace(
            is_active_sent=lambda: active_sent
        ),
        meme_selector=types.SimpleNamespace(select_emoji=select_emoji),
    )
    return svc, calls


class TestPickEmojiOnly:
    @pytest.mark.asyncio
    async def test_group_not_allowed(self):
        svc, calls = _pick_service(group_allowed=False)
        assert await svc.pick_emoji_only(_Event(), ["开心"], "哈哈") is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_skips_when_already_sent_this_turn(self):
        svc, calls = _pick_service(active_sent=True)
        assert await svc.pick_emoji_only(_Event(), ["开心"], "哈哈") is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_passes_priors_to_selector(self):
        svc, calls = _pick_service(selected="picked.png")
        assert await svc.pick_emoji_only(_Event(), ["开心", "无奈"], "哈哈") == "picked.png"
        assert calls == [{"primary": "开心", "text": "哈哈", "extra": ["开心", "无奈"]}]

    @pytest.mark.asyncio
    async def test_blank_priors_filtered(self):
        svc, calls = _pick_service()
        await svc.pick_emoji_only(_Event(), ["", None, "生气"], "哈哈")
        assert calls[0]["primary"] == "生气"
        assert calls[0]["extra"] == ["生气"]

    @pytest.mark.asyncio
    async def test_no_priors_at_all(self):
        svc, calls = _pick_service()
        await svc.pick_emoji_only(_Event(), None, "哈哈")
        assert calls[0]["primary"] == ""
        assert calls[0]["extra"] == []

    @pytest.mark.asyncio
    async def test_selector_miss_returns_none(self):
        svc, _calls = _pick_service(selected="")
        assert await svc.pick_emoji_only(_Event(), ["开心"], "哈哈") is None


class TestAttachTimeout:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (2.5, 2.5),
            ("3", 3.0),
            (0, 10.0),
            (-1, 10.0),
            ("abc", 10.0),
            (None, 10.0),
            ("", 10.0),
        ],
    )
    def test_normalizes(self, value, expected):
        engine = MemeSenderEngine(types.SimpleNamespace(auto_meme_attach_timeout=value))
        assert engine.attach_timeout() == expected

    def test_missing_config_uses_default(self):
        engine = MemeSenderEngine(types.SimpleNamespace())
        assert engine.attach_timeout() == 10.0


def _selector_stub(attached, *, delay=0.0, usage_log=None, raises=None):
    async def attach_emoji_to_result(event, result, emotions, text):
        if delay:
            await asyncio.sleep(delay)
        if raises is not None:
            raise raises
        return attached

    async def record_emoji_usage(path, trigger=""):
        if usage_log is not None:
            usage_log.append((path, trigger))

    return types.SimpleNamespace(
        attach_emoji_to_result=attach_emoji_to_result,
        record_emoji_usage=record_emoji_usage,
    )


def _engine(selector, *, timeout=10.0):
    return MemeSenderEngine(
        types.SimpleNamespace(
            enable_natural_emotion_analysis=False,
            meme_selector=selector,
            auto_meme_attach_timeout=timeout,
        )
    )


class TestEngineAttachEmojiToResult:
    @pytest.mark.asyncio
    async def test_selector_missing(self):
        engine = _engine(None)
        assert await engine.attach_emoji_to_result(_Event(), _Result(), "哈哈", ["开心"]) is False

    @pytest.mark.asyncio
    async def test_selector_without_attach_support(self):
        engine = _engine(types.SimpleNamespace())
        assert await engine.attach_emoji_to_result(_Event(), _Result(), "哈哈", ["开心"]) is False

    @pytest.mark.asyncio
    async def test_no_query_material(self):
        engine = _engine(_selector_stub("a.png"))
        assert await engine.attach_emoji_to_result(_Event(), _Result(), "", []) is False

    @pytest.mark.asyncio
    async def test_timeout_falls_back(self):
        engine = _engine(_selector_stub("a.png", delay=0.5), timeout=0.01)
        assert await engine.attach_emoji_to_result(_Event(), _Result(), "哈哈", ["开心"]) is False

    @pytest.mark.asyncio
    async def test_selector_error_falls_back(self):
        engine = _engine(_selector_stub("a.png", raises=RuntimeError("boom")))
        assert await engine.attach_emoji_to_result(_Event(), _Result(), "哈哈", ["开心"]) is False

    @pytest.mark.asyncio
    async def test_nothing_attached(self):
        engine = _engine(_selector_stub(""))
        event = _Event()
        assert await engine.attach_emoji_to_result(event, _Result(), "哈哈", ["开心"]) is False
        assert engine._auto_emoji_cooldowns == {}
        assert engine.emoji_turn_state(event).is_active_sent() is False

    @pytest.mark.asyncio
    async def test_success_records_usage_and_marks_turn(self, tmp_path):
        path = str(tmp_path / "a.png")
        usage: list = []
        engine = _engine(_selector_stub(path, usage_log=usage))
        event = _Event()

        assert await engine.attach_emoji_to_result(event, _Result(), "哈哈", ["开心"]) is True
        assert usage == [(path, "auto")]
        assert list(engine._auto_emoji_cooldowns) == ["session-attach"]
        assert engine.emoji_turn_state(event).is_active_sent() is True

    @pytest.mark.asyncio
    async def test_usage_record_failure_still_succeeds(self, tmp_path):
        async def boom(path, trigger=""):
            raise RuntimeError("db down")

        selector = _selector_stub(str(tmp_path / "a.png"))
        selector.record_emoji_usage = boom
        engine = _engine(selector)
        event = _Event()
        assert await engine.attach_emoji_to_result(event, _Result(), "哈哈", ["开心"]) is True
        assert engine.emoji_turn_state(event).is_active_sent() is True


def _main(**config):
    main = Main.__new__(Main)
    main.plugin_config = types.SimpleNamespace(**config)
    return main


class TestMainDeliveryMode:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("attach", "attach"),
            ("  ATTACH  ", "attach"),
            ("separate", "separate"),
            ("Separate", "separate"),
            ("", "separate"),
            (None, "separate"),
            ("inline", "separate"),
        ],
    )
    def test_normalizes(self, value, expected):
        assert _main(auto_meme_delivery_mode=value)._auto_meme_delivery_mode() == expected

    def test_missing_config_defaults_to_separate(self):
        assert Main.__new__(Main)._auto_meme_delivery_mode() == "separate"


class TestMainT2iActive:
    def test_explicit_false_wins_over_global(self):
        main = _main()
        main.context = types.SimpleNamespace(get_config=lambda: {"t2i": True})
        assert main._t2i_active(types.SimpleNamespace(use_t2i_=False)) is False

    def test_explicit_true(self):
        main = _main()
        main.context = types.SimpleNamespace(get_config=lambda: {"t2i": False})
        assert main._t2i_active(types.SimpleNamespace(use_t2i_=True)) is True

    def test_falls_back_to_global_on(self):
        main = _main()
        main.context = types.SimpleNamespace(get_config=lambda: {"t2i": True})
        assert main._t2i_active(types.SimpleNamespace(use_t2i_=None)) is True
        assert main._t2i_active(_Result()) is True

    def test_falls_back_to_global_off(self):
        main = _main()
        main.context = types.SimpleNamespace(get_config=lambda: {"t2i": False})
        assert main._t2i_active(types.SimpleNamespace(use_t2i_=None)) is False

    def test_config_error_is_treated_as_off(self):
        main = _main()
        main.context = types.SimpleNamespace(get_config=_boom)
        assert main._t2i_active(types.SimpleNamespace(use_t2i_=None)) is False
