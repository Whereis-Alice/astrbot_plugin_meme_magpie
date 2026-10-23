"""Vision model settings API contracts; all configuration and requests are local stubs."""

import asyncio
import json
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


api_module = import_module("astrbot_plugin_meme_magpie.plugin_api")
PluginAPI = api_module.PluginAPI


def _provider(provider_id, modalities):
    return SimpleNamespace(
        meta=lambda: SimpleNamespace(id=provider_id),
        provider_config={
            "modalities": modalities,
            "api_key": "never-expose-provider-secret",
            "api_base": "https://secret-provider.invalid",
        },
    )


def _make_api(monkeypatch, *, method="GET", payload=None, primary="legacy", fallbacks=()):
    request = SimpleNamespace(method=method, get_json=AsyncMock(return_value=payload))
    monkeypatch.setattr(api_module, "request", request)
    monkeypatch.setattr(api_module, "jsonify", lambda value: value)
    config = SimpleNamespace(
        vision_provider_id=primary,
        vision_provider_ids=list(fallbacks),
        api_key="never-expose-plugin-secret",
    )

    def update(updates):
        config.vision_provider_id = updates["vision_provider_id"]
        config.vision_provider_ids = list(updates["vision_provider_ids"])

    update_config = Mock(side_effect=update)
    plugin = SimpleNamespace(
        plugin_config=config,
        update_config=update_config,
        context=SimpleNamespace(
            get_config=lambda: {
                "provider_settings": {"default_image_caption_provider_id": "global-vlm"}
            },
            get_using_provider_async=AsyncMock(return_value=_provider("current-vlm", ["image"])),
            get_all_providers=lambda: [
                _provider("image-vlm", ["image", "text"]),
                _provider("unknown-vlm", []),
                _provider("text-only", ["text"]),
            ],
        ),
    )
    return PluginAPI(plugin), update_config, request


def test_get_returns_selection_effective_chain_and_safe_model_names(monkeypatch):
    api, update, request = _make_api(monkeypatch, fallbacks=["backup", "legacy", "backup"])
    response = asyncio.run(api.handle_vision_providers())
    assert response["success"] is True
    assert response["vision_provider_id"] == "legacy"
    assert response["vision_provider_ids"] == ["backup", "legacy", "backup"]
    assert response["effective_provider_ids"] == ["legacy", "backup"]
    assert response["providers"] == [
        {"id": "image-vlm", "name": "image-vlm"},
        {"id": "unknown-vlm", "name": "unknown-vlm"},
    ]
    encoded = json.dumps(response)
    assert "never-expose" not in encoded
    assert "api_key" not in encoded
    assert "secret-provider" not in encoded
    update.assert_not_called()
    request.get_json.assert_not_awaited()


def test_get_automatic_mode_reports_framework_current_and_discovered_models(monkeypatch):
    api, _update, _request = _make_api(monkeypatch, primary="")
    response = asyncio.run(api.handle_vision_providers())
    assert response["vision_provider_id"] == ""
    assert response["vision_provider_ids"] == []
    assert response["effective_provider_ids"] == ["global-vlm", "current-vlm", "image-vlm"]


def test_post_normalizes_order_deduplicates_and_returns_saved_selection(monkeypatch):
    api, update, _request = _make_api(
        monkeypatch,
        method="POST",
        payload={
            "vision_provider_id": " first ",
            "vision_provider_ids": [" backup-b ", "first", "", "backup-c", "backup-b"],
        },
    )
    response = asyncio.run(api.handle_vision_providers())
    update.assert_called_once_with({
        "vision_provider_id": "first",
        "vision_provider_ids": ["backup-b", "backup-c"],
    })
    assert response["success"] is True
    assert response["vision_provider_id"] == "first"
    assert response["vision_provider_ids"] == ["backup-b", "backup-c"]
    assert response["effective_provider_ids"] == ["first", "backup-b", "backup-c"]


def test_post_can_select_multiple_models_without_legacy_primary(monkeypatch):
    api, update, _request = _make_api(
        monkeypatch,
        method="POST",
        payload={"vision_provider_ids": ["first", "second"]},
    )
    response = asyncio.run(api.handle_vision_providers())
    update.assert_called_once_with({"vision_provider_id": "first", "vision_provider_ids": ["second"]})
    assert response["effective_provider_ids"] == ["first", "second"]


def test_post_empty_selection_returns_to_automatic_defaults(monkeypatch):
    api, update, _request = _make_api(
        monkeypatch, method="POST", payload={"vision_provider_id": "", "vision_provider_ids": []}
    )
    response = asyncio.run(api.handle_vision_providers())
    update.assert_called_once_with({"vision_provider_id": "", "vision_provider_ids": []})
    assert response["effective_provider_ids"] == ["global-vlm", "current-vlm", "image-vlm"]


@pytest.mark.parametrize("payload", [
    None, [], "first", 1,
    {"vision_provider_id": None},
    {"vision_provider_id": True},
    {"vision_provider_ids": "first"},
    {"vision_provider_ids": ["first", None]},
    {"vision_provider_ids": [1]},
    {"vision_provider_ids": [False]},
])
def test_post_rejects_invalid_types_without_changing_configuration(monkeypatch, payload):
    api, update, _request = _make_api(monkeypatch, method="POST", payload=payload)
    response, status = asyncio.run(api.handle_vision_providers())
    assert status == 400
    assert response["success"] is False
    update.assert_not_called()
    assert api.plugin.plugin_config.vision_provider_id == "legacy"


def test_post_rejects_unreadable_json_without_changing_configuration(monkeypatch):
    api, update, request = _make_api(monkeypatch, method="POST")
    request.get_json.side_effect = ValueError("malformed JSON")
    response, status = asyncio.run(api.handle_vision_providers())
    assert status == 400
    assert response["success"] is False
    update.assert_not_called()


def test_post_reports_save_failure_without_exposing_exception_details(monkeypatch):
    api, update, _request = _make_api(
        monkeypatch, method="POST", payload={"vision_provider_id": "new-vlm"}
    )
    update.side_effect = RuntimeError("never-expose-save-secret")
    response, status = asyncio.run(api.handle_vision_providers())
    assert status == 500
    assert response["success"] is False
    assert "never-expose" not in json.dumps(response)


def test_post_never_reports_success_when_update_silently_keeps_old_config(monkeypatch):
    api, update, _request = _make_api(
        monkeypatch, method="POST", payload={"vision_provider_id": "new-vlm"}
    )
    update.side_effect = None
    response, status = asyncio.run(api.handle_vision_providers())
    assert status == 500
    assert response["success"] is False
    assert api.plugin.plugin_config.vision_provider_id == "legacy"


def test_post_awaits_async_configuration_update_before_reporting_success(monkeypatch):
    api, _update, _request = _make_api(
        monkeypatch, method="POST", payload={"vision_provider_id": "new-vlm"}
    )

    async def save(updates):
        await asyncio.sleep(0)
        api.plugin.plugin_config.vision_provider_id = updates["vision_provider_id"]
        api.plugin.plugin_config.vision_provider_ids = updates["vision_provider_ids"]

    update = AsyncMock(side_effect=save)
    api.plugin.update_config = update
    response = asyncio.run(api.handle_vision_providers())
    assert response["success"] is True
    assert response["effective_provider_ids"] == ["new-vlm"]
    update.assert_awaited_once_with({"vision_provider_id": "new-vlm", "vision_provider_ids": []})


def test_post_persists_backing_config_before_syncing_runtime(monkeypatch):
    api, runtime_update, _request = _make_api(
        monkeypatch,
        method="POST",
        payload={"vision_provider_id": "new-vlm", "vision_provider_ids": ["backup"]},
    )
    events = []
    persisted = {}

    def save(updates):
        events.append("persist")
        persisted.update(updates)
        return True

    def sync_runtime(updates):
        assert persisted == updates
        events.append("runtime")
        api.plugin.plugin_config.vision_provider_id = updates["vision_provider_id"]
        api.plugin.plugin_config.vision_provider_ids = updates["vision_provider_ids"]

    persist = Mock(side_effect=save)
    api.plugin.plugin_config.update_config = persist
    runtime_update.side_effect = sync_runtime
    response = asyncio.run(api.handle_vision_providers())
    assert response["success"] is True
    assert events == ["persist", "runtime"]
    persist.assert_called_once_with({
        "vision_provider_id": "new-vlm", "vision_provider_ids": ["backup"]
    })


def test_post_awaits_backing_config_save_before_syncing_runtime(monkeypatch):
    api, runtime_update, _request = _make_api(
        monkeypatch, method="POST", payload={"vision_provider_id": "new-vlm"}
    )
    events = []

    async def save(updates):
        await asyncio.sleep(0)
        events.append("persisted")
        return True

    original_sync = runtime_update.side_effect

    def sync_runtime(updates):
        assert events == ["persisted"]
        events.append("runtime")
        original_sync(updates)

    persist = AsyncMock(side_effect=save)
    api.plugin.plugin_config.update_config = persist
    runtime_update.side_effect = sync_runtime
    response = asyncio.run(api.handle_vision_providers())
    assert response["success"] is True
    assert events == ["persisted", "runtime"]
    persist.assert_awaited_once_with({"vision_provider_id": "new-vlm", "vision_provider_ids": []})


def test_failed_backing_save_rolls_back_fields_without_runtime_sync(monkeypatch):
    api, runtime_update, _request = _make_api(
        monkeypatch,
        method="POST",
        payload={"vision_provider_id": "new-vlm", "vision_provider_ids": ["new-backup"]},
        fallbacks=["old-backup"],
    )

    def failed_save(updates):
        # PluginConfig sets its model fields before its backing save can fail.
        api.plugin.plugin_config.vision_provider_id = updates["vision_provider_id"]
        api.plugin.plugin_config.vision_provider_ids = updates["vision_provider_ids"]
        return False

    api.plugin.plugin_config.update_config = Mock(side_effect=failed_save)
    response, status = asyncio.run(api.handle_vision_providers())
    assert status == 500
    assert response["success"] is False
    runtime_update.assert_not_called()
    assert api.plugin.plugin_config.vision_provider_id == "legacy"
    assert api.plugin.plugin_config.vision_provider_ids == ["old-backup"]


def test_post_with_real_plugin_config_persists_ids_across_reload(monkeypatch, tmp_path):
    from core.config import config as config_module

    monkeypatch.setattr(
        config_module, "StarTools", SimpleNamespace(get_data_dir=lambda name: tmp_path / name)
    )
    saved_updates = []

    class SavedConfig(dict):
        def save_config(self, updates=None):
            saved_updates.append(dict(updates or {}))
            self.update(updates or {})

    raw = SavedConfig(vision_provider_id="legacy", vision_provider_ids=["old-backup"], steal_meme=True)
    config = config_module.PluginConfig(raw)
    saved_updates.clear()
    api, _runtime_update, _request = _make_api(
        monkeypatch,
        method="POST",
        payload={"vision_provider_id": "new-vlm", "vision_provider_ids": ["backup"]},
    )
    api.plugin.plugin_config = config

    def main_like_runtime_update(updates):
        # Main.update_config changes running services/model fields and does not save.
        for key, value in updates.items():
            setattr(config, key, value)

    runtime_update = Mock(side_effect=main_like_runtime_update)
    api.plugin.update_config = runtime_update
    response = asyncio.run(api.handle_vision_providers())
    assert response["success"] is True
    assert saved_updates == [{"vision_provider_id": "new-vlm", "vision_provider_ids": ["backup"]}]
    runtime_update.assert_called_once()
    reloaded = config_module.PluginConfig(raw)
    assert reloaded.get_vision_provider_ids() == ["new-vlm", "backup"]
    assert reloaded.steal_meme is True


def test_real_plugin_config_save_error_preserves_prior_selection(monkeypatch, tmp_path):
    from core.config import config as config_module

    monkeypatch.setattr(
        config_module, "StarTools", SimpleNamespace(get_data_dir=lambda name: tmp_path / name)
    )

    class FailedSaveConfig(dict):
        def save_config(self, updates=None):
            raise OSError("backing config is read-only")

    raw = FailedSaveConfig(vision_provider_id="legacy", vision_provider_ids=["old-backup"])
    config = config_module.PluginConfig(raw)
    api, runtime_update, _request = _make_api(
        monkeypatch, method="POST", payload={"vision_provider_id": "new-vlm"}
    )
    api.plugin.plugin_config = config
    response, status = asyncio.run(api.handle_vision_providers())
    assert status == 500
    assert response["success"] is False
    runtime_update.assert_not_called()
    assert config.get_vision_provider_ids() == ["legacy", "old-backup"]
    assert raw["vision_provider_id"] == "legacy"
    assert raw["vision_provider_ids"] == ["old-backup"]


def test_vision_provider_route_supports_get_and_post(monkeypatch):
    api, _update, _request = _make_api(monkeypatch)
    register = Mock()
    api.register(SimpleNamespace(register_web_api=register))
    matching = [call for call in register.call_args_list if call.args[0].endswith("/vision-providers")]
    assert len(matching) == 1
    assert matching[0].args[0] == "/astrbot_plugin_meme_magpie/vision-providers"
    assert matching[0].args[1] == api.handle_vision_providers
    assert matching[0].args[2] == ["GET", "POST"]
