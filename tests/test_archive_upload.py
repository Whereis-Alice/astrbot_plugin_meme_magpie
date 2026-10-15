"""Exercise real HTTP limits as well as chunk assembly and backup preflight."""

import asyncio
import base64
import hashlib
import importlib
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage

from astrbot_plugin_meme_magpie import plugin_api as plugin_api_module
from astrbot_plugin_meme_magpie.core.sources.archive_upload import ArchiveUploadService
from astrbot_plugin_meme_magpie.core.sources.models import ExternalSourceError
from astrbot_plugin_meme_magpie.core.sources.source_service import SourceService
from astrbot_plugin_meme_magpie.plugin_api import PluginAPI


def test_chunk_order_retry_limits_and_cleanup(tmp_path):
    async def run():
        service = ArchiveUploadService(tmp_path)

        async def call(action, **payload):
            return await service.handle({"action": action, **payload}, max_bytes=10)

        for size in (0, -1, True, 11, "4"):
            with pytest.raises(ExternalSourceError):
                await call("start", filename="test.zip", size=size)
        with pytest.raises(ExternalSourceError):
            await call("start", filename="test.exe", size=4)
        session = await call("start", filename="../../test.zip", size=4)
        uid = session["upload_id"]
        data = base64.b64encode(b"ab").decode()
        with pytest.raises(ExternalSourceError, match="in order"):
            await call("chunk", upload_id=uid, offset=2, data=data)
        await call("chunk", upload_id=uid, offset=0, data=data)
        assert (await call("chunk", upload_id=uid, offset=0, data=data))[
            "received"
        ] == 2
        for offset, encoded in (
            (0, "Y2Q="),
            (2, "!bad!"),
            (4, data),
            (2, "x" * 700000),
        ):
            with pytest.raises(ExternalSourceError):
                await call("chunk", upload_id=uid, offset=offset, data=encoded)
        with pytest.raises(ExternalSourceError, match="incomplete"):
            await call("finish", upload_id=uid)
        await call("chunk", upload_id=uid, offset=2, data="Y2Q=")
        result = await call("finish", upload_id=uid)
        assert await call("finish", upload_id=uid) == result
        assert Path(result["path"]).parent == tmp_path
        assert Path(result["path"]).read_bytes() == b"abcd"
        await call("cancel", upload_id=uid)
        assert Path(
            result["path"]
        ).exists()  # cancellation never deletes a completed archive
        first = await call("start", filename="a.zip", size=1)
        await call("start", filename="b.zip", size=1)
        with pytest.raises(ExternalSourceError, match="too many"):
            await call("start", filename="c.zip", size=1)
        service._sessions[first["upload_id"]].touched = (
            time.monotonic() - service.TTL_SECONDS - 1
        )
        await call("start", filename="c.zip", size=1)
        assert not (tmp_path / f"{first['upload_id']}.part").exists()
        for key in list(service._sessions):
            await call("cancel", upload_id=key)
        assert not list(tmp_path.glob("*.part"))
        with pytest.raises(ExternalSourceError, match="not found"):
            await call("finish", upload_id="../elsewhere")

    asyncio.run(run())


@pytest.fixture
def real_quart(monkeypatch):
    # Most legacy tests intentionally use a tiny Quart stub. Bind the real
    # framework only for these HTTP tests and restore their globals afterwards.
    for name in ("quart", "aiofiles", "aiofiles.os"):
        module = sys.modules.get(name)
        if module is not None and not getattr(module, "__file__", None):
            monkeypatch.delitem(sys.modules, name)
    quart = importlib.import_module("quart")
    monkeypatch.setattr(plugin_api_module, "request", quart.request)
    monkeypatch.setattr(plugin_api_module, "jsonify", quart.jsonify)
    return quart


def _app(data_dir, quart):
    plugin = SimpleNamespace(base_dir=data_dir, plugin_config=SimpleNamespace())
    plugin.source_service = SourceService(plugin)
    api = PluginAPI(plugin)
    app = quart.Quart(__name__)
    app.add_url_rule("/upload", view_func=api.handle_source_upload, methods=["POST"])
    app.add_url_rule(
        "/chunked", view_func=api.handle_source_chunked_upload, methods=["POST"]
    )
    app.add_url_rule("/inspect", view_func=api.handle_source_inspect, methods=["POST"])
    return app


async def _upload_file(client, path):
    async def post(**payload):
        # Each request fits under even a common 1 MiB reverse proxy limit.
        assert len(json.dumps(payload).encode()) < 1024 * 1024
        response = await client.post("/chunked", json=payload)
        assert response.status_code == 200, await response.get_data(as_text=True)
        result = await response.get_json()
        assert result["success"], result
        return result

    started = await post(action="start", filename=path.name, size=path.stat().st_size)
    uid = started["upload_id"]
    digest = hashlib.sha256()
    offset = 0
    with path.open("rb") as source:
        while chunk := source.read(started["chunk_size"]):
            digest.update(chunk)
            result = await post(
                action="chunk",
                upload_id=uid,
                offset=offset,
                data=base64.b64encode(chunk).decode(),
            )
            offset += len(chunk)
            assert result["received"] == offset
    completed = await post(action="finish", upload_id=uid)
    with Path(completed["path"]).open("rb") as uploaded:
        assert hashlib.file_digest(uploaded, "sha256").digest() == digest.digest()
    response = await client.post(
        "/inspect", json={"source_type": "meme_pack", "path": completed["path"]}
    )
    assert response.status_code == 200, await response.get_data(as_text=True)
    inspected = await response.get_json()
    assert inspected["success"], inspected
    assert not inspected["inspection"]["errors"], inspected["inspection"]["errors"]
    return inspected["inspection"], completed["path"]


def test_archive_over_host_limit_uploads_and_inspects(tmp_path, real_quart):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (4, 4)).save(image, format="PNG")
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w") as pack:
        pack.writestr("memes/happy/a.png", image.getvalue())
        pack.writestr("padding.bin", b"\0" * (17 * 1024 * 1024))

    async def run():
        app = _app(tmp_path / "data", real_quart)
        assert app.config["MAX_CONTENT_LENGTH"] == 16 * 1024 * 1024
        client = app.test_client()
        with archive.open("rb") as stream:
            failed = await client.post(
                "/upload", files={"file": FileStorage(stream, archive.name)}
            )
        assert (
            failed.status_code == 413
        )  # reproduce the original failure with real multipart parsing
        inspection, _ = await _upload_file(client, archive)
        assert inspection["item_count"] == 1

    asyncio.run(run())


@pytest.mark.skipif(
    not os.environ.get("MAGPIE_UPLOAD_TEST_ARCHIVE"),
    reason="optional local backup validation",
)
def test_local_backup_upload_and_preflight(tmp_path, real_quart):
    """Opt-in read-only original; all uploaded bytes go to pytest's temporary directory."""
    path = Path(os.environ["MAGPIE_UPLOAD_TEST_ARCHIVE"])

    async def run():
        from tests.test_backup_export import DatabaseService, _plugin

        inspection, uploaded = await _upload_file(
            _app(tmp_path, real_quart).test_client(), path
        )
        assert inspection["backup"] is True
        destination = tmp_path / "restored"
        destination.mkdir()
        db = DatabaseService(destination / "emoji.db")
        plugin = _plugin(destination, db)
        cfg = plugin.plugin_config
        cfg.external_source_max_archive_bytes = 1024**3
        cfg.external_source_max_uncompressed_bytes = 4 * 1024**3
        cfg.external_source_max_image_bytes = 32 * 1024**2
        cfg.external_source_max_pixels = 50_000_000
        cfg.external_source_max_items = 20_000
        service = SourceService(plugin)
        job = await service.import_now(
            {
                "source_type": "meme_pack",
                "path": uploaded,
                "restore_pending": True,
                "restore_blacklist": True,
            }
        )
        assert job["status"] == "completed"
        assert job["failed"] == 0
        assert job["imported"] == inspection["item_count"]
        with zipfile.ZipFile(uploaded) as archive:
            metadata = json.loads(archive.read("magpie_export.json"))
        restored = {row["hash"]: row for row in db.get_index_cache_readonly().values()}
        for original in metadata["library"]:
            row = restored[original["hash"]]
            for key in (
                "desc",
                "tags",
                "scenes",
                "emotions",
                "overlay_text",
                "character",
                "work",
            ):
                assert row.get(key) == original.get(key), key
        print(
            f"Validated backup: {path.stat().st_size} bytes; {job['imported']} images restored with metadata"
        )

    asyncio.run(run())
