"""work（作品名）维度端到端覆盖：检索打分、语义文本、数据库读写。"""

import asyncio
import sys
import tempfile
import types
from pathlib import Path


def _install_stubs():
    logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    api_module.AstrBotConfig = object
    sys.modules["astrbot.api"] = api_module

    star_module = types.ModuleType("astrbot.api.star")
    star_module.StarTools = types.SimpleNamespace(
        get_data_dir=lambda name: str(Path(tempfile.gettempdir()) / "astrbot_test" / name)
    )
    star_module.Context = object
    star_module.Star = object
    sys.modules["astrbot.api.star"] = star_module


_install_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db.database_service import DatabaseService
from core.processing.semantic_schema import build_meme_search_text
from core.search.meme_search_engine import MemeSearchEngine
from core.search.meme_smart_select_service import MemeSmartSelectService


def _engine():
    """真实打分器 + 最小 selector 桦（打分尾巴会回调 selector）。"""
    selector = types.SimpleNamespace(
        _collect_phrase_words=lambda tags: frozenset(),
    )
    return MemeSearchEngine(types.SimpleNamespace(), selector)


def _score(engine, query, **kwargs):
    params = {
        "query_lower": query,
        "query_tokens": [],
        "category": "开心",
        "desc": "",
        "tags": [],
        "max_str_len": 32,
    }
    params.update(kwargs)
    return engine._score_entry(**params)


# ── 检索打分中的作品名 ──────────────────────────────────


class TestWorkScoring:
    def test_work_hit_returns_18(self):
        engine = _engine()
        assert _score(engine, "来张孤独摇滚的图", work="孤独摇滚") == 18

    def test_character_outranks_work(self):
        engine = _engine()
        got = _score(engine, "孤独摇滚的后藤一里", character="后藤一里", work="孤独摇滚")
        assert got == 20

    def test_overlay_outranks_character_and_work(self):
        engine = _engine()
        got = _score(
            engine, "孤独摇滚 后藤一里 我不想努力了",
            overlay="我不想努力了", character="后藤一里", work="孤独摇滚",
        )
        assert got == 22

    def test_single_char_work_is_ignored(self):
        engine = _engine()
        assert _score(engine, "看看火", work="火") != 18

    def test_work_not_in_query_is_ignored(self):
        engine = _engine()
        assert _score(engine, "随便一张图", work="孤独摇滚") != 18

    def test_work_matching_is_case_insensitive(self):
        engine = _engine()
        assert _score(engine, "来点 vocaloid", work="VOCALOID") == 18

    def test_empty_work_does_not_crash(self):
        engine = _engine()
        assert _score(engine, "开心", work="") == 8
        assert _score(engine, "开心", work=None) == 8

    def test_work_beats_exact_desc_match(self):
        # 作品名命中要早退，优先级高于描述精确匹配（15 分）。
        engine = _engine()
        assert _score(engine, "孤独摇滚", desc="孤独摇滚", work="孤独摇滚") == 18


# ── 语义检索文本 ────────────────────────────────────────


class TestSemanticSearchText:
    def test_work_is_included(self):
        text = build_meme_search_text(
            {"category": "开心", "desc": "在弹吉他", "work": "孤独摇滚", "character": "后藤一里"}
        )
        assert "孤独摇滚" in text
        assert "后藤一里" in text

    def test_missing_work_is_harmless(self):
        text = build_meme_search_text({"category": "开心", "desc": "笑"})
        assert "笑" in text

    def test_work_survives_bm25_variant(self):
        text = build_meme_search_text({"work": "孤独摇滚", "desc": "弹吉他"}, bm25=True)
        assert "孤独摇滚" in text


# ── 智能选择中的作品权重 ────────────────────────────────


class TestWorkMatchScore:
    def test_hit(self):
        assert MemeSmartSelectService._work_match_score("聊聊孤独摇滚", {"work": "孤独摇滚"}) == 1.0

    def test_miss(self):
        assert MemeSmartSelectService._work_match_score("聊聊别的", {"work": "孤独摇滚"}) == 0.0

    def test_empty_query(self):
        assert MemeSmartSelectService._work_match_score("", {"work": "孤独摇滚"}) == 0.0

    def test_short_work_ignored(self):
        assert MemeSmartSelectService._work_match_score("看看火", {"work": "火"}) == 0.0

    def test_missing_field(self):
        assert MemeSmartSelectService._work_match_score("随便", {}) == 0.0


# ── 数据库中的 work 列 ──────────────────────────────────


def _rows(base):
    return [
        {
            "path": str(base / "a.png"), "hash": "h1", "category": "开心",
            "desc": "弹吉他", "character": "后藤一里", "work": "孤独摇滚",
            "created_at": "2026-01-01", "tags": ["吉他"], "scenes": ["演出"],
        },
        {
            "path": str(base / "b.png"), "hash": "h2", "category": "开心",
            "desc": "唱歌", "character": "喜多郁代", "work": "孤独摇滚",
            "created_at": "2026-01-02", "tags": ["唱歌"], "scenes": [],
        },
        {
            "path": str(base / "c.png"), "hash": "h3", "category": "难过",
            "desc": "没有作品名", "created_at": "2026-01-03", "tags": [], "scenes": [],
        },
    ]


def _seed(tmp_path):
    db = DatabaseService(tmp_path / "emoji.db")
    assert asyncio.run(db.insert_batch(_rows(tmp_path))) == 3
    return db


class TestWorkColumn:
    def test_schema_version_is_7(self, tmp_path: Path):
        db = DatabaseService(tmp_path / "emoji.db")
        assert db.SCHEMA_VERSION >= 7
        with db._get_connection() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(emoji)").fetchall()}
            pending = {r[1] for r in conn.execute("PRAGMA table_info(emoji_pending)").fetchall()}
        assert "work" in cols
        assert "work" in pending

    def test_work_round_trips(self, tmp_path: Path):
        db = _seed(tmp_path)
        index = db.get_index_cache_readonly()
        works = {meta.get("work", "") for meta in index.values()}
        assert works == {"孤独摇滚", ""}

    def test_filter_by_work(self, tmp_path: Path):
        db = _seed(tmp_path)
        items, total, _ = db.get_emojis_paginated(work="孤独摇滚")
        assert total == 2
        assert {i["hash"] for i in items} == {"h1", "h2"}

    def test_filter_unlabelled_only(self, tmp_path: Path):
        db = _seed(tmp_path)
        items, total, _ = db.get_emojis_paginated(work="__none__")
        assert total == 1
        assert items[0]["hash"] == "h3"

    def test_no_work_filter_returns_all(self, tmp_path: Path):
        db = _seed(tmp_path)
        _items, total, _ = db.get_emojis_paginated()
        assert total == 3

    def test_work_counts(self, tmp_path: Path):
        db = _seed(tmp_path)
        counts = db.get_work_counts()
        assert counts.get("孤独摇滚") == 2
        assert counts.get("") == 1

    def test_update_work_via_update_path(self, tmp_path: Path):
        db = _seed(tmp_path)
        target = str(tmp_path / "c.png")
        assert asyncio.run(db.update_path(target, {"work": "赛马娘"})) is True
        _items, total, _ = db.get_emojis_paginated(work="赛马娘")
        assert total == 1

    def test_clear_work_keeps_images(self, tmp_path: Path):
        db = _seed(tmp_path)
        assert db.clear_work("孤独摇滚") == 2
        assert db.clear_work("不存在的作品") == 0
        assert db.clear_work("") == 0
        _items, total, _ = db.get_emojis_paginated()
        assert total == 3
        _items, labelled, _ = db.get_emojis_paginated(work="孤独摇滚")
        assert labelled == 0

    def test_search_query_covers_work(self, tmp_path: Path):
        db = _seed(tmp_path)
        _items, total, _ = db.get_emojis_paginated(search_query="孤独摇滚")
        assert total == 2

    def test_pending_accepts_work(self, tmp_path: Path):
        db = DatabaseService(tmp_path / "emoji.db")
        meta = {
            "path": str(tmp_path / "p.png"), "hash": "p1", "category": "开心",
            "desc": "待审核", "work": "孤独摇滚", "created_at": 1767225600,
        }
        assert asyncio.run(db.insert_pending(meta)) is not None
        with db._get_connection() as conn:
            row = conn.execute("SELECT work FROM emoji_pending WHERE hash = ?", ("p1",)).fetchone()
        assert row is not None
        assert row["work"] == "孤独摇滚"

    def test_corpus_signature_reacts_to_work(self, tmp_path: Path):
        db = _seed(tmp_path)
        before = db.get_corpus_signature()
        asyncio.run(db.update_path(str(tmp_path / "c.png"), {"work": "赛马娘"}))
        assert db.get_corpus_signature() != before
