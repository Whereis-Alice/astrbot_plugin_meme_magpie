"""Vision provider configuration preserves ordered fallback and legacy settings."""

import json
from pathlib import Path
from types import SimpleNamespace

def _build_config(monkeypatch, tmp_path, values):
    from core.config import config as config_module

    monkeypatch.setattr(
        config_module,
        "StarTools",
        SimpleNamespace(get_data_dir=lambda name: tmp_path / name),
    )
    return config_module.PluginConfig(values)


def test_legacy_provider_remains_first_and_duplicates_are_removed(monkeypatch, tmp_path):
    config = _build_config(
        monkeypatch,
        tmp_path,
        {
            "vision_provider_id": " primary ",
            "vision_provider_ids": ["backup", "primary", "", " backup ", "third"],
        },
    )
    assert config.get_vision_provider_ids() == ["primary", "backup", "third"]
    # Returning the chain must not expose mutable configuration state.
    config.get_vision_provider_ids().append("external")
    assert config.vision_provider_ids == ["backup", "primary", "", " backup ", "third"]


def test_fallback_list_can_supply_primary_without_legacy_setting(monkeypatch, tmp_path):
    config = _build_config(monkeypatch, tmp_path, {"vision_provider_ids": ["first", "second"]})
    assert config.get_vision_provider_ids() == ["first", "second"]


def test_dashboard_order_survives_config_reload_and_can_clear_to_automatic(monkeypatch, tmp_path):
    class SavedConfig(dict):
        def save_config(self, updates=None):
            self.update(updates or {})

    raw = SavedConfig(vision_provider_id="old", steal_meme=True)
    config = _build_config(monkeypatch, tmp_path, raw)
    assert config.update_config({"vision_provider_id": "new", "vision_provider_ids": ["old"]})
    reloaded = _build_config(monkeypatch, tmp_path, raw)
    assert reloaded.get_vision_provider_ids() == ["new", "old"]
    assert reloaded.steal_meme is True
    assert reloaded.update_config({"vision_provider_id": "", "vision_provider_ids": []})
    assert _build_config(monkeypatch, tmp_path, raw).get_vision_provider_ids() == []


def test_new_vision_settings_have_native_multiselect_and_all_locales():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["vision_provider_ids"]["type"] == "list"
    assert schema["vision_provider_ids"]["_special"] == "select_providers"
    assert schema["vision_timeout_seconds"]["default"] == 60.0
    for locale in ("zh-CN", "en-US", "ru-RU"):
        messages = json.loads((root / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
        for key in ("vision_provider_ids", "vision_timeout_seconds"):
            assert messages["config"][key]["description"]
            assert messages["config"][key]["hint"]
        assert messages["pages"]["dashboard"]["vision"]["title"]
