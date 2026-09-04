"""向量新鲜度自愈 与 容量清理保护 回归测试。

覆盖两类真实事故：
1. 回填时读不到「已有向量」，被当成一条都没有，于是整库重算、白烧嵌入额度；
2. 后台容量控制 / 孤儿清理静默永久删除用户攒下来的表情包文件。
"""

import os
import sqlite3
import types

import pytest

from core.db.database_service import DatabaseService
from core.events.event_handler import EventHandler
from core.maintenance import service as maintenance_module
from core.maintenance.service import MaintenanceService
from core.search.embedding_service import EmbeddingService

# ═══════════════════════════════════════════════════
#  向量回填与新鲜度
# ═══════════════════════════════════════════════════

ENTRIES = {
    "/a.png": {"category": "happy", "desc": "猫猫贴贴"},
    "/b.png": {"category": "sad", "desc": "狗狗流泪"},
}


class _FakeDB:
    """只实现 EmbeddingService 回填用到的那几个方法。"""

    def __init__(self, entries, hashes=None):
        self.entries = dict(entries)
        self.hashes = dict(hashes or {})
        self.embedded: set[str] = set()
        self.hash_writes: list[list[tuple[str, str]]] = []
        self.forgotten: list[str] = []

    # emoji 元数据
    def get_all_paths(self):
        return list(self.entries)

    def count_total(self):
        return len(self.entries)

    def get_index_cache_readonly(self):
        return dict(self.entries)

    def get_emoji(self, path):
        return self.entries.get(path)

    # 向量本体
    def get_all_embedding_paths(self):
        return sorted(self.embedded)

    def delete_embedding(self, path):
        self.embedded.discard(path)

    # 文本指纹
    def get_all_embedding_hashes(self):
        return dict(self.hashes)

    def set_embedding_hash(self, path, text_hash):
        self.hashes[path] = text_hash
        self.hash_writes.append([(path, text_hash)])

    def set_embedding_hashes(self, items):
        items = list(items)
        self.hash_writes.append(items)
        for path, text_hash in items:
            self.hashes[path] = text_hash

    def delete_embedding_hash(self, path):
        self.forgotten.append(path)
        self.hashes.pop(path, None)


def _make_service(tmp_path, entries=None, hashes=None):
    """走 SQLite 降级后端、不触碰真实嵌入模型的 EmbeddingService。"""
    db = _FakeDB(entries if entries is not None else ENTRIES, hashes)
    plugin = types.SimpleNamespace(
        plugin_config=types.SimpleNamespace(
            category_info={}, character_info={}, data_dir=str(tmp_path)
        ),
        db_service=db,
        enable_embedding_search=True,
    )
    svc = EmbeddingService(plugin)
    svc._init_faiss = lambda: False
    svc._load_fallback_matrix = lambda: None

    inserted: list[str] = []

    async def _fake_insert(path, text):
        inserted.append(path)
        db.embedded.add(path)
        svc._record_text_hash(path, text)
        return True

    svc._fallback_insert = _fake_insert
    return svc, db, inserted


@pytest.mark.asyncio
async def test_backfill_aborts_when_existing_vectors_unreadable(tmp_path):
    """读不到已有向量必须中止，不能当成「一条都没有」去重算全库。"""
    svc, db, inserted = _make_service(tmp_path)
    db.embedded = set(ENTRIES)

    def _boom():
        raise RuntimeError("database is locked")

    db.get_all_embedding_paths = _boom

    assert await svc.backfill_existing() == 0
    assert inserted == []


@pytest.mark.asyncio
async def test_backfill_fills_everything_on_fresh_install(tmp_path):
    """读到空集合是全新安装的正常情况，照常全量回填并记下指纹。"""
    svc, db, inserted = _make_service(tmp_path)

    assert await svc.backfill_existing() == 2
    assert sorted(inserted) == ["/a.png", "/b.png"]
    assert set(db.hashes) == {"/a.png", "/b.png"}


@pytest.mark.asyncio
async def test_backfill_only_records_hashes_for_legacy_library(tmp_path):
    """老库升级上来：向量都在、指纹表是空的，只补写指纹，一次模型都不调。"""
    svc, db, inserted = _make_service(tmp_path)
    db.embedded = set(ENTRIES)

    assert await svc.backfill_existing() == 0
    assert inserted == []
    assert len(db.hash_writes) == 1
    assert sorted(p for p, _ in db.hash_writes[0]) == ["/a.png", "/b.png"]


@pytest.mark.asyncio
async def test_backfill_refreshes_only_the_stale_vector(tmp_path):
    """描述改过的那条重算，没改过的不动。"""
    svc, db, inserted = _make_service(tmp_path)
    db.embedded = set(ENTRIES)
    fresh = svc._build_search_text(ENTRIES["/b.png"])
    db.hashes = {
        "/a.png": svc._text_fingerprint("这是改之前的旧文本"),
        "/b.png": svc._text_fingerprint(fresh),
    }

    deleted: list[str] = []

    async def _fake_delete(path):
        deleted.append(path)
        return True

    svc.delete_by_path = _fake_delete

    assert await svc.backfill_existing() == 1
    assert deleted == ["/a.png"]
    assert inserted == ["/a.png"]


@pytest.mark.asyncio
async def test_backfill_skips_stale_when_old_vector_cannot_be_deleted(tmp_path):
    """删不掉旧向量就跳过；否则新旧两条都留着，检索会一直命中旧内容。"""
    svc, db, inserted = _make_service(tmp_path)
    db.embedded = set(ENTRIES)
    db.hashes = {p: svc._text_fingerprint("旧文本") for p in ENTRIES}

    async def _fake_delete(path):
        return False

    svc.delete_by_path = _fake_delete

    assert await svc.backfill_existing() == 0
    assert inserted == []


# ── FaissVecDB 分支 ──


class _FakeDocStore:
    def __init__(self, docs):
        self.docs = docs
        self.limits: list[int] = []

    async def get_documents(self, metadata_filters=None, limit=1):
        self.limits.append(limit)
        return list(self.docs)


class _FakeFaiss:
    def __init__(self, docs):
        self.document_storage = _FakeDocStore(docs)
        self.deleted: list[str] = []

    async def delete(self, doc_id):
        self.deleted.append(doc_id)


def _make_faiss_service(tmp_path, docs):
    svc, db, _inserted = _make_service(tmp_path)
    fake = _FakeFaiss(docs)
    svc._init_faiss = lambda: True
    svc._faiss_db = fake
    return svc, db, fake


@pytest.mark.asyncio
async def test_delete_by_path_removes_all_duplicate_docs(tmp_path):
    """历史上只删「查到的第一条」，重复向量永远清不完。"""
    svc, db, fake = _make_faiss_service(tmp_path, [{"doc_id": "d1"}, {"doc_id": "d2"}])

    assert await svc.delete_by_path("/a.png") is True
    assert fake.deleted == ["d1", "d2"]
    assert db.forgotten == ["/a.png"]
    assert fake.document_storage.limits[0] == EmbeddingService.MAX_DUPLICATE_DOCS


@pytest.mark.asyncio
async def test_delete_by_path_reports_failure_when_doc_id_missing(tmp_path):
    """删不干净要返回 False，调用方才知道不能紧接着 insert。"""
    svc, db, fake = _make_faiss_service(tmp_path, [{"doc_id": "d1"}, {"metadata": {}}])

    assert await svc.delete_by_path("/a.png") is False
    assert fake.deleted == ["d1"]
    assert db.forgotten == []


@pytest.mark.asyncio
async def test_read_embedded_paths_parses_json_metadata(tmp_path):
    docs = [
        {"metadata": {"path": "/a.png"}},
        {"metadata": '{"path": "/b.png"}'},
        {"metadata": "not json at all"},
        "garbage",
    ]
    svc, db, fake = _make_faiss_service(tmp_path, docs)

    assert await svc._read_embedded_paths(True, db, len(ENTRIES)) == {
        "/a.png",
        "/b.png",
    }


@pytest.mark.asyncio
async def test_read_embedded_paths_limit_follows_library_size(tmp_path):
    """limit 写死会让超出部分每次启动都被误判成缺向量。"""
    svc, db, fake = _make_faiss_service(tmp_path, [])

    await svc._read_embedded_paths(True, db, 300000)
    assert fake.document_storage.limits[-1] == 600000


@pytest.mark.asyncio
async def test_read_embedded_paths_returns_none_on_error(tmp_path):
    """读失败返回 None，和「读到了但是空的」区分开。"""
    svc, db, fake = _make_faiss_service(tmp_path, [])

    async def _boom(metadata_filters=None, limit=1):
        raise RuntimeError("store closed")

    fake.document_storage.get_documents = _boom
    assert await svc._read_embedded_paths(True, db, 10) is None


@pytest.mark.asyncio
async def test_rebuild_vectors_wipes_then_refills(tmp_path):
    svc, db, inserted = _make_service(tmp_path)
    db.embedded = set(ENTRIES)
    db.hashes = {p: "stale" for p in ENTRIES}

    def _clear():
        db.embedded.clear()
        db.hashes.clear()

    db.clear_all_embeddings = _clear

    result = await svc.rebuild_vectors()
    assert result["ok"] is True
    assert result["total"] == 2
    assert result["written"] == 2
    assert sorted(inserted) == ["/a.png", "/b.png"]


@pytest.mark.asyncio
async def test_rebuild_vectors_reports_disabled(tmp_path):
    svc, db, inserted = _make_service(tmp_path)
    svc.plugin.enable_embedding_search = False

    assert await svc.rebuild_vectors() == {
        "ok": False,
        "reason": "disabled",
        "written": 0,
        "total": 0,
    }


# ═══════════════════════════════════════════════════
#  v8 指纹表
# ═══════════════════════════════════════════════════


def _table_exists(db_path, name):
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    return row is not None


def _schema_version(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    return row[0] if row else None


def test_fresh_library_has_embedding_state_table(tmp_path):
    db_path = tmp_path / "emoji.db"
    DatabaseService(str(db_path))

    assert _table_exists(db_path, "emoji_embedding_state")
    assert _schema_version(db_path) == str(DatabaseService.SCHEMA_VERSION)


def test_v7_library_gains_embedding_state_table(tmp_path):
    """模拟 v7 老库：删掉指纹表、版本号退回 7，重开应自动补建。"""
    db_path = tmp_path / "emoji.db"
    DatabaseService(str(db_path))
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE emoji_embedding_state")
        conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
        conn.commit()
    assert not _table_exists(db_path, "emoji_embedding_state")

    DatabaseService(str(db_path))
    assert _table_exists(db_path, "emoji_embedding_state")
    assert _schema_version(db_path) == str(DatabaseService.SCHEMA_VERSION)


@pytest.mark.asyncio
async def test_embedding_hash_roundtrip_and_cascade(tmp_path):
    db = DatabaseService(str(tmp_path / "emoji.db"))
    await db.insert_batch(
        [
            {"path": "/x.png", "hash": "h1", "category": "happy"},
            {"path": "/y.png", "hash": "h2", "category": "sad"},
        ]
    )

    # 混进一个已不存在的 path：被外键挡掉，但不能拖累同批的正常行
    db.set_embedding_hashes([("/x.png", "f1"), ("/y.png", "f2"), ("/ghost.png", "f3")])
    assert db.get_all_embedding_hashes() == {"/x.png": "f1", "/y.png": "f2"}

    db.set_embedding_hash("/x.png", "f1-new")
    assert db.get_all_embedding_hashes()["/x.png"] == "f1-new"

    db.delete_embedding_hash("/y.png")
    assert "/y.png" not in db.get_all_embedding_hashes()

    # 表情包被删时指纹跟着走，不留脏数据
    await db.delete_paths(["/x.png"])
    assert db.get_all_embedding_hashes() == {}


@pytest.mark.asyncio
async def test_clear_all_embeddings_also_clears_fingerprints(tmp_path):
    db = DatabaseService(str(tmp_path / "emoji.db"))
    await db.insert_batch([{"path": "/x.png", "hash": "h1", "category": "happy"}])
    db.set_embedding_hash("/x.png", "f1")

    db.clear_all_embeddings()
    assert db.get_all_embedding_hashes() == {}


# ═══════════════════════════════════════════════════
#  容量控制与孤儿清理
# ═══════════════════════════════════════════════════


def _pick_for_removal(max_reg, index):
    plugin = types.SimpleNamespace(
        plugin_config=types.SimpleNamespace(max_reg_num=max_reg)
    )
    return EventHandler._select_items_for_removal(
        types.SimpleNamespace(plugin=plugin), index
    )


def _sample_index(count=10):
    return {f"/{i}.png": {"created_at": i} for i in range(count)}


def test_capacity_zero_means_unlimited():
    """0 = 不限制。旧代码会把 0 当无效值并悄悄改回 100，然后开始删图。"""
    assert _pick_for_removal(0, _sample_index()) == []


def test_capacity_removes_only_the_oldest_overflow():
    picked = _pick_for_removal(8, _sample_index())
    assert [p for p, _ in picked] == ["/0.png", "/1.png"]


def test_capacity_never_removes_favorites():
    index = _sample_index()
    index["/0.png"]["is_favorite"] = True
    picked = _pick_for_removal(9, index)
    assert [p for p, _ in picked] == ["/1.png"]


class _FakeIndexManager:
    def __init__(self, idx):
        self.idx = idx
        self.loads = 0
        self.saves = 0

    async def load_index(self):
        self.loads += 1
        return dict(self.idx)

    async def save_index(self, idx):
        self.saves += 1


class _FakeCapacityHandler:
    def __init__(self):
        self.calls = 0

    async def _enforce_capacity(self, idx):
        self.calls += 1


def _make_maintenance(count, *, max_reg, auto):
    index_manager = _FakeIndexManager(_sample_index(count))
    handler = _FakeCapacityHandler()
    plugin = types.SimpleNamespace(
        plugin_config=types.SimpleNamespace(
            max_reg_num=max_reg, capacity_auto_cleanup=auto
        ),
        index_manager=index_manager,
        event_handler=handler,
        cmd=lambda sub: f"/mp {sub}",
    )
    return MaintenanceService(plugin), index_manager, handler


@pytest.mark.asyncio
async def test_hourly_capacity_check_only_warns_by_default():
    """默认不再后台静默删图：只告警，等用户自己决定。"""
    maint, index_manager, handler = _make_maintenance(134, max_reg=100, auto=False)

    await maint._check_capacity_once()

    assert handler.calls == 0
    assert index_manager.saves == 0


@pytest.mark.asyncio
async def test_hourly_capacity_check_deletes_when_opted_in():
    maint, index_manager, handler = _make_maintenance(134, max_reg=100, auto=True)

    await maint._check_capacity_once()

    assert handler.calls == 1
    assert index_manager.saves == 1


@pytest.mark.asyncio
async def test_hourly_capacity_check_skips_when_unlimited():
    maint, index_manager, handler = _make_maintenance(134, max_reg=0, auto=True)

    await maint._check_capacity_once()

    assert index_manager.loads == 0
    assert handler.calls == 0


@pytest.mark.asyncio
async def test_orphan_cleanup_refuses_suspiciously_large_batch(monkeypatch):
    """一次要删 60/100 个文件，几乎只可能是索引读坏了，宁可不删。"""
    removed: list[str] = []

    async def _fake_remove(path):
        removed.append(path)

    monkeypatch.setattr(maintenance_module, "safe_remove_file", _fake_remove)
    maint = MaintenanceService(types.SimpleNamespace())

    await maint._remove_orphan_files([f"/f{i}.png" for i in range(60)], 100, "categories")

    assert removed == []


@pytest.mark.asyncio
async def test_orphan_cleanup_still_removes_small_batch(monkeypatch):
    removed: list[str] = []

    async def _fake_remove(path):
        removed.append(path)

    monkeypatch.setattr(maintenance_module, "safe_remove_file", _fake_remove)
    maint = MaintenanceService(types.SimpleNamespace())
    orphans = ["/f1.png", "/f2.png", "/f3.png"]

    await maint._remove_orphan_files(orphans, 100, "categories")

    assert removed == orphans


def test_norm_path_treats_equivalent_spellings_as_same(tmp_path):
    """路径写法不同就判成孤儿的话，正常表情包会被当垃圾删掉。"""
    base = str(tmp_path)
    direct = MaintenanceService._norm_path(os.path.join(base, "sub", "a.png"))
    round_trip = MaintenanceService._norm_path(
        os.path.join(base, "sub", "..", "sub", "a.png")
    )

    assert direct == round_trip
    assert MaintenanceService._norm_path("a.png") == MaintenanceService._norm_path(
        os.path.abspath("a.png")
    )
    if os.path.normcase("A") == "a":  # Windows：大小写不敏感
        assert direct == MaintenanceService._norm_path(
            os.path.join(base, "SUB", "A.PNG")
        )
