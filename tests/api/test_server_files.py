"""Tests for API server file download and upload endpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("nacl", reason="PyNaCl not installed (optional: pip install dt-agent[api])")

from aiohttp import FormData, web
from aiohttp.test_utils import make_mocked_request
from yarl import URL

from ductor_bot.api.server import ApiServer, _parse_file_refs
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.config import AgentConfig, ApiConfig
from ductor_bot.config_reload import ConfigReloader
from ductor_bot.orchestrator.lifecycle import start_api_server

# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestParseFileRefs:
    def test_no_files(self) -> None:
        assert _parse_file_refs("just text") == []

    def test_single_file(self) -> None:
        refs = _parse_file_refs("result <file:/tmp/output.txt>")
        assert len(refs) == 1
        assert refs[0]["path"] == "/tmp/output.txt"
        assert refs[0]["name"] == "output.txt"
        assert refs[0]["is_image"] is False

    def test_image_file(self) -> None:
        refs = _parse_file_refs("<file:/tmp/photo.jpg>")
        assert refs[0]["is_image"] is True

    def test_multiple_files(self) -> None:
        refs = _parse_file_refs("<file:/a.txt> and <file:/b.png>")
        assert len(refs) == 2
        assert refs[0]["is_image"] is False
        assert refs[1]["is_image"] is True

    def test_windows_file_ref_is_normalized(self) -> None:
        with patch("ductor_bot.files.tags.is_windows", return_value=True):
            refs = _parse_file_refs("<file:/C/Users/alice/output_to_user/out.zip>")
        assert refs[0]["path"] == "C:/Users/alice/output_to_user/out.zip"
        assert refs[0]["name"] == "out.zip"


# ---------------------------------------------------------------------------
# Integration tests for HTTP endpoints
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path) -> web.Application:
    """Build an aiohttp app with ApiServer file handlers for testing."""
    from ductor_bot.api.server import ApiServer

    config = ApiConfig(
        enabled=True,
        host="127.0.0.1",
        port=0,
        token="test-token",
        allow_public=True,
    )
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    server = ApiServer(config, default_chat_id=1)
    server.set_message_handler(AsyncMock())
    server.set_abort_handler(AsyncMock(return_value=0))
    server.set_file_context(
        allowed_roots=[tmp_path],
        upload_dir=upload_dir,
        workspace=workspace,
    )

    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.router.add_get("/health", server._handle_health)
    app.router.add_get("/files", server._handle_file_download)
    app.router.add_post("/upload", server._handle_file_upload)

    return app


@pytest.fixture
async def api_client(tmp_path: Path, aiohttp_client):
    """Create an aiohttp test client with the API server app."""
    app = _make_app(tmp_path)
    client = await aiohttp_client(app)
    client._tmp_path = tmp_path
    return client


class TestFileDownload:
    async def test_no_auth_returns_401(self, api_client) -> None:
        resp = await api_client.get("/files", params={"path": "/tmp/test"})
        assert resp.status == 401

    async def test_wrong_token_returns_401(self, api_client) -> None:
        resp = await api_client.get(
            "/files",
            params={"path": "/tmp/test"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status == 401

    async def test_missing_path_returns_400(self, api_client) -> None:
        resp = await api_client.get(
            "/files",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    async def test_nonexistent_file_returns_404(self, api_client) -> None:
        tmp = api_client._tmp_path
        resp = await api_client.get(
            "/files",
            params={"path": str(tmp / "nonexistent.txt")},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 404

    async def test_valid_file_download(self, api_client) -> None:
        tmp = api_client._tmp_path
        test_file = tmp / "download_test.txt"
        test_file.write_text("hello world")

        resp = await api_client.get(
            "/files",
            params={"path": str(test_file)},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 200
        body = await resp.read()
        assert body == b"hello world"

    async def test_path_outside_allowed_roots_returns_403(self, api_client) -> None:
        resp = await api_client.get(
            "/files",
            params={"path": "/etc/hostname"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 403


class TestFileUpload:
    async def test_no_auth_returns_401(self, api_client) -> None:
        resp = await api_client.post("/upload")
        assert resp.status == 401

    async def test_upload_file(self, api_client) -> None:
        data = FormData()
        data.add_field("file", b"test content", filename="test.txt")

        resp = await api_client.post(
            "/upload",
            data=data,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["name"] == "test.txt"
        assert body["size"] == 12
        assert "prompt" in body
        assert "[INCOMING FILE]" in body["prompt"]
        assert "via API" in body["prompt"]

    async def test_upload_with_caption(self, api_client) -> None:
        data = FormData()
        data.add_field("file", b"img data", filename="photo.jpg", content_type="image/jpeg")
        data.add_field("caption", "Look at this photo")

        resp = await api_client.post(
            "/upload",
            data=data,
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert "Look at this photo" in body["prompt"]

    def test_uploaded_file_exists_on_disk(self, tmp_path: Path) -> None:
        """Verify prepare_destination creates the file in the right place."""
        from ductor_bot.files.storage import prepare_destination

        dest = prepare_destination(tmp_path, "data.csv")
        dest.write_bytes(b"saved content")
        assert dest.is_file()
        assert dest.read_bytes() == b"saved content"


class TestFileAccessReload:
    @pytest.mark.parametrize(
        ("initial", "updated", "outside_status"),
        [
            ("all", "workspace", 403),
            ("home", "workspace", 403),
            ("workspace", "all", 200),
            ("all", "unknown", 403),
        ],
    )
    async def test_download_uses_reloaded_policy(
        self, tmp_path: Path, monkeypatch, initial: str, updated: str, outside_status: int
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        inside = workspace / "inside.txt"
        outside = tmp_path / "outside.txt"
        payload = b"synthetic file contents"
        inside.write_bytes(payload)
        outside.write_bytes(payload)
        config = AgentConfig(file_access=initial, api=ApiConfig(token="test-token"))
        paths = SimpleNamespace(
            workspace=workspace,
            api_files_dir=workspace / "uploads",
            config_path=tmp_path / "config.json",
        )
        paths.config_path.write_text(config.model_dump_json())
        orch = SimpleNamespace(
            _config=config,
            _lock_pool=LockPool(),
            handle_message_streaming=AsyncMock(),
            abort=AsyncMock(),
            _providers=SimpleNamespace(build_provider_info=lambda _cache: []),
            _observers=SimpleNamespace(codex_cache_obs=None),
        )
        with patch.object(ApiServer, "start", new_callable=AsyncMock):
            await start_api_server(orch, config, paths)
        server = orch._api_stop.__self__
        reloader = ConfigReloader(paths.config_path, config)
        data = config.model_dump(mode="json")
        data["file_access"] = updated
        paths.config_path.write_text(json.dumps(data))
        mtime = reloader._last_mtime + 2
        os.utime(paths.config_path, (mtime, mtime))
        await reloader._check()
        assert config.file_access == updated

        for path, status in [(outside, outside_status), (inside, 200)]:
            request = make_mocked_request(
                "GET",
                str(URL("/files").with_query(path=str(path))),
                headers={"Authorization": "Bearer test-token"},
            )
            with patch(
                "ductor_bot.api.server.web.FileResponse", wraps=web.FileResponse
            ) as response:
                result = await server._handle_file_download(request)
            assert result.status == status
            if status == 403:
                response.assert_not_called()
                assert result.body.count(payload) == 0
            else:
                _, handle, _, _ = result._make_response(request, "")
                assert handle is not None
                try:
                    assert handle.read() == payload
                finally:
                    handle.close()

    @pytest.mark.parametrize(
        ("roots", "token", "status"), [([], "test-token", 403), (None, "wrong", 401)]
    )
    async def test_getter_respects_empty_roots_and_auth(
        self, tmp_path: Path, roots, token: str, status: int
    ) -> None:
        file = tmp_path / "file.txt"
        file.write_text("synthetic file contents")
        server = ApiServer(ApiConfig(token="test-token"))
        server.set_file_context(
            allowed_roots=None,
            upload_dir=tmp_path,
            workspace=tmp_path,
            allowed_roots_getter=lambda: roots,
        )
        request = make_mocked_request(
            "GET",
            str(URL("/files").with_query(path=str(file))),
            headers={"Authorization": f"Bearer {token}"},
        )
        result = await server._handle_file_download(request)
        assert result.status == status
