"""旧插件数据迁移（MigrationService）回归测试。

覆盖：探测、预演不写盘、实际迁入、幂等、路径重映射、黑名单跳过、
缺文件计数、标签/场景合并、向量搬迁、分类合并、配置沿用。
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from core.db.database_service import DatabaseService
from core.maintenance.migration_service import MigrationService

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

_LEGACY_EMOJI_DDL = """
CREATE TABLE emoji (
    path TEXT PRIMARY KEY, hash TEXT, phash TEXT, category TEXT, desc TEXT,
    source TEXT, origin_target TEXT, scope_mode TEXT, created_at INTEGER,
    use_count INTEGER, last_used_at INTEGER, is_favorite INTEGER, reviewed_at INTEGER,
    source_url TEXT, original_name TEXT, width INTEGER, height INTEGER, format TEXT,
    bytes INTEGER, add_method TEXT, overlay_text TEXT, emotions_json TEXT, character TEXT
);
CREATE TABLE emoji_tag (path TEXT, tag TEXT, PRIMARY KEY (path, tag));
CREATE TABLE emoji_scene (path TEXT, scene TEXT, PRIMARY KEY (path, scene));
CREATE TABLE blacklist (hash TEXT PRIMARY KEY, created_at INTEGER);
CREATE TABLE emoji_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, hash TEXT, phash TEXT,
    category TEXT, desc TEXT, source TEXT, origin_target TEXT, scope_mode TEXT,
    review_status TEXT, created_at INTEGER, tags_text TEXT, scenes_text TEXT,
    source_url TEXT, original_name TEXT, width INTEGER, height INTEGER, format TEXT,
    bytes INTEGER, add_method TEXT, overlay_text TEXT, emotions_json TEXT, character TEXT
);
CREATE TABLE emoji_embedding (
    path TEXT PRIMARY KEY, vector BLOB, dim INTEGER, model_sig TEXT, updated_at INTEGER
);
"""


def _pick_bool_config_key() -> tuple[str, bool]:
    """从真实 _conf_schema.json 里挑一个布尔配置项，避免测试写死键名。"""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    for key, entry in schema.items():
        if key.startswith("_") or not isinstance(entry, dict):
            continue
        if isinstance(entry.get("default"), bool):
            return key, bool(entry["default"])
    raise AssertionError("_conf_schema.json 里没有布尔配置项")


class _FakeConfig:
    """最小可用的 PluginConfig 替身。"""

    model_fields: dict = {}

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.categories = ["happy", "sad"]
        self.category_info = {"happy": {"desc": "开心"}}
        self.characters = ["原有角色"]
        self.character_info = {"原有角色": {"work": "旧作品"}}
        self.applied_updates: dict = {}

    def normalize_category_strict(self, category):
        value = str(category or "").lower().strip()
        return value if value in set(self.categories) | {"angry", "其他"} else None

    def get_categories(self):
        return list(self.categories)

    def ensure_category_dirs(self, categories):
        for name in categories or []:
            (self.data_dir / "categories" / str(name)).mkdir(parents=True, exist_ok=True)

    def update_config(self, updates):
        self.applied_updates.update(updates)
        for key, value in updates.items():
            setattr(self, key, value)
        return True


class _FakePlugin:
    def __init__(self, config: _FakeConfig, db: DatabaseService):
        self.plugin_config = config
        self.db_service = db
        self.meme_selector = None
        self.cache_service = None


def _write_file(path: Path, payload: bytes = b"fake-image-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _emoji_row(path: str, hash_val: str, category: str, **over):
    row = {
        "path": path, "hash": hash_val, "phash": "p" + hash_val, "category": category,
        "desc": "一只猫在挥手", "source": "group", "origin_target": "12345",
        "scope_mode": "public", "created_at": 1700000000, "use_count": 3,
        "last_used_at": 1700000900, "is_favorite": 1, "reviewed_at": None,
        "source_url": "", "original_name": Path(path).name, "width": 100, "height": 100,
        "format": "png", "bytes": 16, "add_method": "auto", "overlay_text": "你好",
        "emotions_json": json.dumps(["开心"], ensure_ascii=False), "character": "初音未来",
    }
    row.update(over)
    return row


def _build_legacy(root: Path) -> Path:
    """在 root 下造一份仿真的旧插件数据目录。"""
    legacy = root / "plugin_data" / "astrbot_plugin_stealer"
    happy = _write_file(legacy / "categories" / "happy" / "1700000000_aaaa1111.png")
    sad = _write_file(legacy / "categories" / "sad" / "1700000001_bbbb2222.png")
    _write_file(legacy / "pending" / "1700000002_cccc3333.png")
    black = legacy / "categories" / "angry" / "1700000003_dddd4444.png"
    _write_file(black)

    (legacy / "categories.json").write_text(
        json.dumps(["happy", "sad", "angry"], ensure_ascii=False), encoding="utf-8"
    )
    (legacy / "characters.json").write_text(
        json.dumps(["初音未来", "原有角色"], ensure_ascii=False), encoding="utf-8"
    )
    (legacy / "character_info.json").write_text(
        json.dumps({"初音未来": {"work": "VOCALOID"}}, ensure_ascii=False), encoding="utf-8"
    )

    db_path = legacy / "cache" / "emoji.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_LEGACY_EMOJI_DDL)
    rows = [
        _emoji_row(str(happy), "aaaa1111", "happy"),
        _emoji_row(str(sad), "bbbb2222", "sad", character=""),
        _emoji_row(str(black), "dddd4444", "angry"),
        _emoji_row(str(legacy / "categories" / "happy" / "gone.png"), "eeee5555", "happy"),
    ]
    columns = list(rows[0])
    conn.executemany(
        f"INSERT INTO emoji ({','.join(columns)}) "
        f"VALUES ({','.join('?' * len(columns))})",
        [tuple(row[c] for c in columns) for row in rows],
    )
    conn.executemany(
        "INSERT INTO emoji_tag (path, tag) VALUES (?, ?)",
        [(str(happy), "猫"), (str(happy), "挥手"), (str(sad), "哭")],
    )
    conn.execute("INSERT INTO emoji_scene (path, scene) VALUES (?, ?)", (str(happy), "打招呼"))
    conn.execute("INSERT INTO blacklist (hash, created_at) VALUES (?, ?)", ("dddd4444", 1700))
    conn.execute(
        "INSERT INTO emoji_pending (path, hash, category, desc, scope_mode, review_status,"
        " created_at, tags_text, scenes_text, character) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            str(legacy / "pending" / "1700000002_cccc3333.png"), "cccc3333", "happy",
            "待审核图", "public", "pending", 1700000002, "标签A、标签B", "场景A,场景B", "洛天依",
        ),
    )
    conn.execute(
        "INSERT INTO emoji_embedding (path, vector, dim, model_sig, updated_at) VALUES (?,?,?,?,?)",
        (str(happy), sqlite3.Binary(b"\x00\x01\x02\x03"), 4, "test-model", 1700),
    )
    conn.commit()
    conn.close()
    return legacy


@pytest.fixture()
def env(tmp_path: Path):
    legacy = _build_legacy(tmp_path)
    target = tmp_path / "plugin_data" / "astrbot_plugin_meme_magpie"
    (target / "cache").mkdir(parents=True, exist_ok=True)
    config = _FakeConfig(target)
    db = DatabaseService(target / "cache" / "emoji.db")
    plugin = _FakePlugin(config, db)
    return {
        "legacy": legacy,
        "target": target,
        "config": config,
        "db": db,
        "service": MigrationService(plugin),
        "root": tmp_path,
    }


# ── 探测 ──


def test_detect_finds_sibling_legacy_dir(env):
    info = env["service"].detect_legacy()
    assert info["found"] is True
    assert Path(info["source_dir"]) == env["legacy"]
    assert info["emoji_count"] == 4
    assert info["pending_count"] == 1
    assert info["file_count"] == 4


def test_detect_reports_reason_when_absent(tmp_path: Path):
    target = tmp_path / "lonely" / "astrbot_plugin_meme_magpie"
    (target / "cache").mkdir(parents=True, exist_ok=True)
    config = _FakeConfig(target)
    db = DatabaseService(target / "cache" / "emoji.db")
    info = MigrationService(_FakePlugin(config, db)).detect_legacy()
    assert info["found"] is False
    assert "未找到旧插件数据目录" in info["reason"]


def test_detect_accepts_explicit_parent_dir(env):
    info = env["service"].detect_legacy(str(env["legacy"].parent))
    assert info["found"] is True
    assert Path(info["source_dir"]) == env["legacy"]


# ── 预演 ──


def test_dry_run_writes_nothing(env):
    report = asyncio.run(env["service"].migrate())
    assert report.dry_run is True
    assert report.emojis_imported == 2
    assert report.pending_imported == 1
    assert report.emojis_skipped_blacklisted == 1
    assert report.missing_files == 1
    assert env["db"].count_total() == 0
    assert not (env["target"] / "categories" / "happy").exists()
    assert "/magpie migrate apply" in report.summary()


def test_dry_run_counts_tags_scenes_and_vectors(env):
    report = asyncio.run(env["service"].migrate())
    assert report.tags_imported == 5  # 猫/挥手/哭 + 待审核 标签A/标签B
    assert report.scenes_imported == 3  # 打招呼 + 场景A/场景B
    assert report.embeddings_imported == 1
    assert report.blacklist_imported == 1


# ── 实际迁入 ──


def test_apply_imports_rows_files_and_extras(env):
    report = asyncio.run(env["service"].migrate(apply=True))
    db = env["db"]
    target = env["target"]

    assert report.dry_run is False
    assert report.emojis_imported == 2
    assert report.pending_imported == 1
    assert report.files_copied == 3
    assert db.count_total() == 2
    assert (target / "categories" / "happy" / "1700000000_aaaa1111.png").is_file()
    assert (target / "categories" / "sad" / "1700000001_bbbb2222.png").is_file()
    assert (target / "pending" / "1700000002_cccc3333.png").is_file()
    # 旧文件保留，可回滚
    assert (env["legacy"] / "categories" / "happy" / "1700000000_aaaa1111.png").is_file()
    assert "rebuild_index" in report.summary()


def test_apply_remaps_paths_into_target_dir(env):
    asyncio.run(env["service"].migrate(apply=True))
    paths = env["db"].get_all_paths()
    assert len(paths) == 2
    for path in paths:
        assert str(env["target"]) in path
        assert str(env["legacy"]) not in path


def test_apply_preserves_metadata_and_relations(env):
    asyncio.run(env["service"].migrate(apply=True))
    entries, total, _ = env["db"].get_emojis_paginated(page=1, page_size=50)
    assert total == 2
    happy = next(e for e in entries if e["hash"] == "aaaa1111")
    assert happy["category"] == "happy"
    assert happy["character"] == "初音未来"
    assert happy["overlay_text"] == "你好"
    assert happy["use_count"] == 3
    assert set(happy["tags"]) == {"猫", "挥手"}
    assert happy["scenes"] == ["打招呼"]
    assert happy["add_method"] == "auto"


def test_apply_splits_legacy_text_columns_for_pending(env):
    asyncio.run(env["service"].migrate(apply=True))
    rows, total, _ = env["db"].get_pending_paginated(page=1, page_size=50)
    assert total == 1
    assert set(rows[0]["tags"]) == {"标签A", "标签B"}
    assert set(rows[0]["scenes"]) == {"场景A", "场景B"}
    assert rows[0]["character"] == "洛天依"


def test_apply_copies_blacklist_and_embeddings(env):
    asyncio.run(env["service"].migrate(apply=True))
    assert "dddd4444" in env["db"].blacklisted_hashes()
    assert env["service"].plugin.db_service is env["db"]
    target_path = str(env["target"] / "categories" / "happy" / "1700000000_aaaa1111.png")
    with env["db"]._get_connection() as conn:
        row = conn.execute(
            "SELECT dim, model_sig FROM emoji_embedding WHERE path = ?", (target_path,)
        ).fetchone()
    assert row is not None
    assert row["dim"] == 4
    assert row["model_sig"] == "test-model"


def test_apply_merges_categories_and_characters(env):
    report = asyncio.run(env["service"].migrate(apply=True))
    config = env["config"]
    assert "angry" in config.categories
    assert report.added_category_names == ["angry"]
    assert "初音未来" in config.characters
    assert report.added_character_names == ["初音未来"]
    # 字典类资产只补缺失键，不覆盖已有内容
    assert config.character_info["原有角色"] == {"work": "旧作品"}
    assert config.character_info["初音未来"] == {"work": "VOCALOID"}


def test_apply_is_idempotent(env):
    first = asyncio.run(env["service"].migrate(apply=True))
    second = asyncio.run(env["service"].migrate(apply=True))
    assert first.emojis_imported == 2
    assert second.emojis_imported == 0
    assert second.pending_imported == 0
    assert second.emojis_skipped_duplicate == 2
    assert second.pending_skipped_duplicate == 1
    assert env["db"].count_total() == 2
    rows, total, _ = env["db"].get_pending_paginated(page=1, page_size=50)
    assert total == 1


def test_move_mode_relocates_source_files(env):
    report = asyncio.run(env["service"].migrate(apply=True, move=True))
    assert report.move is True
    assert not (env["legacy"] / "categories" / "happy" / "1700000000_aaaa1111.png").exists()
    assert (env["target"] / "categories" / "happy" / "1700000000_aaaa1111.png").is_file()
    assert "移动" in report.summary()


def test_move_flag_ignored_during_dry_run(env):
    report = asyncio.run(env["service"].migrate(move=True))
    assert report.move is False
    assert (env["legacy"] / "categories" / "happy" / "1700000000_aaaa1111.png").is_file()


def test_missing_source_dir_yields_readable_report(tmp_path: Path):
    target = tmp_path / "solo" / "astrbot_plugin_meme_magpie"
    (target / "cache").mkdir(parents=True, exist_ok=True)
    service = MigrationService(
        _FakePlugin(_FakeConfig(target), DatabaseService(target / "cache" / "emoji.db"))
    )
    report = asyncio.run(service.migrate(apply=True))
    assert report.total_imported == 0
    assert report.errors
    assert "未找到旧插件数据目录" in report.summary()


# ── 配置沿用 ──


def test_config_adopts_legacy_value_when_user_untouched(env):
    key, default = _pick_bool_config_key()
    setattr(env["config"], key, default)
    type(env["config"]).model_fields = {key: object()}
    (env["root"] / "config").mkdir(parents=True, exist_ok=True)
    (env["root"] / "config" / "astrbot_plugin_stealer_config.json").write_text(
        json.dumps({key: (not default), "categories": ["不应被沿用"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        report = asyncio.run(env["service"].migrate(apply=True))
        assert report.imported_config_keys == [key]
        assert env["config"].applied_updates == {key: (not default)}
    finally:
        type(env["config"]).model_fields = {}


def test_config_skips_key_user_already_changed(env):
    key, default = _pick_bool_config_key()
    setattr(env["config"], key, not default)  # 用户已改过
    type(env["config"]).model_fields = {key: object()}
    (env["root"] / "config").mkdir(parents=True, exist_ok=True)
    (env["root"] / "config" / "astrbot_plugin_stealer_config.json").write_text(
        json.dumps({key: (not default)}, ensure_ascii=False), encoding="utf-8"
    )
    try:
        report = asyncio.run(env["service"].migrate())
        assert report.config_keys_imported == 0
    finally:
        type(env["config"]).model_fields = {}


# ── 内部工具 ──


def test_merge_multi_dedupes_and_keeps_order():
    merged = MigrationService._merge_multi(["猫", "猫", " 狗 "], "狗、鸟|鱼")
    assert merged == ["猫", "狗", "鸟", "鱼"]


def test_dedupe_target_appends_suffix(tmp_path: Path):
    taken = {str(tmp_path / "a.png").lower()}
    result = MigrationService._dedupe_target(tmp_path / "a.png", taken)
    assert result.name == "a_mig1.png"
    again = MigrationService._dedupe_target(tmp_path / "a.png", taken)
    assert again.name == "a_mig2.png"


def test_target_relpath_keeps_category_layout(tmp_path: Path):
    src = tmp_path / "categories" / "happy" / "x.png"
    rel = MigrationService._target_relpath(src, tmp_path, "happy", pending=False)
    assert rel == Path("categories") / "happy" / "x.png"


def test_target_relpath_routes_raw_files_into_category(tmp_path: Path):
    src = tmp_path / "raw" / "y.png"
    rel = MigrationService._target_relpath(src, tmp_path, "sad", pending=False)
    assert rel == Path("categories") / "sad" / "y.png"
    assert MigrationService._target_relpath(src, tmp_path, "sad", pending=True) == (
        Path("pending") / "y.png"
    )


def test_report_as_dict_is_json_serializable():
    from core.maintenance.migration_service import MigrationReport

    payload = MigrationReport(source_dir="x").as_dict()
    assert json.loads(json.dumps(payload))["source_dir"] == "x"
    assert payload["total_imported"] == 0
