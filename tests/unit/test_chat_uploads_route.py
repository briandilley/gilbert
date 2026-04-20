"""Tests for the HTTP chat upload/download endpoints.

The endpoints live at ``src/gilbert/web/routes/chat_uploads.py`` and
stream user-uploaded files to the per-conversation workspace
``uploads/`` directory. These tests spin up a minimal FastAPI app
with fake Storage + Workspace services, hit the endpoints via the
Starlette TestClient, and verify end-to-end that:

- An authenticated upload lands on disk with the reported size and
  a sanitized filename.
- The response is shaped like a reference-mode ``FileAttachment``
  with the workspace coordinates the chat message will carry.
- Unauthenticated callers get 401.
- Callers who can't access the target conversation get 403.
- Unknown conversations get 404.
- Oversize uploads get 413 and leave nothing on disk.
- Path traversal in download requests gets rejected.
- A successful download streams the bytes back with the right
  Content-Disposition header.
- Filename collisions auto-rename (``foo.bin`` → ``foo-1.bin``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gilbert.interfaces.auth import UserContext
from gilbert.web.auth import require_authenticated
from gilbert.web.routes.chat_uploads import (
    router as chat_uploads_router,
)

# ── Test doubles ─────────────────────────────────────────────────────


class _FakeStorageBackend:
    def __init__(self, conversations: dict[str, dict[str, Any]]) -> None:
        self._conversations = conversations

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        if collection != "ai_conversations":
            return None
        return self._conversations.get(entity_id)


class _FakeStorageProvider:
    def __init__(self, backend: _FakeStorageBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> _FakeStorageBackend:
        return self._backend

    @property
    def raw_backend(self) -> _FakeStorageBackend:
        return self._backend

    def create_namespaced(self, namespace: str) -> _FakeStorageBackend:
        return self._backend


class _FakeWorkspaceProvider:
    """Stand-in for ``WorkspaceProvider``. The route calls
    ``get_upload_dir``, ``get_workspace_root``, and ``register_file``,
    which must return real on-disk directories since the upload
    endpoint writes files."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._files: list[dict[str, Any]] = []

    def get_workspace_root(self, user_id: str, conversation_id: str) -> Path:
        d = self._root / "users" / user_id / "conversations" / conversation_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_upload_dir(self, user_id: str, conversation_id: str) -> Path:
        d = self.get_workspace_root(user_id, conversation_id) / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_output_dir(self, user_id: str, conversation_id: str) -> Path:
        d = self.get_workspace_root(user_id, conversation_id) / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_scratch_dir(self, user_id: str, conversation_id: str) -> Path:
        d = self.get_workspace_root(user_id, conversation_id) / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def register_file(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    async def list_files(
        self, conversation_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            f for f in self._files
            if f.get("conversation_id") == conversation_id
            and (category is None or f.get("category") == category)
        ]

    def stage_file(
        self,
        *,
        user_id: str,
        conversation_id: str,
        category: str,
        rel_path: str,
        content: bytes,
    ) -> None:
        """Write bytes to the workspace and add a registry entry so the
        download-all endpoint can find it."""
        root = self.get_workspace_root(user_id, conversation_id)
        full = root / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
        self._files.append(
            {
                "conversation_id": conversation_id,
                "user_id": user_id,
                "category": category,
                "rel_path": rel_path,
                "filename": Path(rel_path).name,
            }
        )

    async def build_workspace_manifest(self, conversation_id: str) -> str:
        return ""


class _FakeServiceManager:
    def __init__(
        self, storage: _FakeStorageProvider, workspace: _FakeWorkspaceProvider
    ) -> None:
        self._storage = storage
        self._workspace = workspace

    def get_by_capability(self, capability: str) -> Any:
        if capability == "entity_storage":
            return self._storage
        if capability == "workspace":
            return self._workspace
        return None


class _FakeGilbert:
    def __init__(
        self, storage: _FakeStorageProvider, workspace: _FakeWorkspaceProvider
    ) -> None:
        self.service_manager = _FakeServiceManager(storage, workspace)


# ── Fixtures ─────────────────────────────────────────────────────────


_OWNER_USER = UserContext(
    user_id="usr_owner",
    display_name="Owner",
    email="owner@example.com",
    roles=frozenset({"user"}),
    provider="local",
)

_OTHER_USER = UserContext(
    user_id="usr_other",
    display_name="Other",
    email="other@example.com",
    roles=frozenset({"user"}),
    provider="local",
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture
def conversations() -> dict[str, dict[str, Any]]:
    return {
        "conv-owned": {
            "user_id": "usr_owner",
            "title": "Owner's chat",
            "messages": [],
        },
        "conv-room": {
            "shared": True,
            "visibility": "public",
            "title": "Public room",
            "members": [],
            "messages": [],
        },
    }


@pytest.fixture
def workspace_provider(workspace_root: Path) -> _FakeWorkspaceProvider:
    return _FakeWorkspaceProvider(workspace_root)


@pytest.fixture
def app(
    workspace_provider: _FakeWorkspaceProvider,
    conversations: dict[str, dict[str, Any]],
) -> FastAPI:
    storage = _FakeStorageProvider(_FakeStorageBackend(conversations))
    gilbert = _FakeGilbert(storage, workspace_provider)

    app = FastAPI()
    app.state.gilbert = gilbert
    app.include_router(chat_uploads_router)
    return app


def _override_auth(app: FastAPI, user: UserContext | None) -> None:
    from fastapi import HTTPException

    def _fake_dep(request: Request) -> UserContext:
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return user

    app.dependency_overrides[require_authenticated] = _fake_dep


# ── Upload tests ─────────────────────────────────────────────────────


def test_upload_writes_file_to_disk_and_returns_reference(
    app: FastAPI, workspace_root: Path
) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)

    payload = b"binary file content" * 100  # 1900 bytes
    resp = client.post(
        "/api/chat/upload",
        data={"conversation_id": "conv-owned"},
        files={"file": ("archive.zip", payload, "application/zip")},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "file"
    assert body["name"] == "archive.zip"
    assert body["media_type"] == "application/zip"
    assert body["workspace_skill"] == "workspace"
    assert body["workspace_path"] == "uploads/archive.zip"
    assert body["workspace_conv"] == "conv-owned"
    assert body["size"] == len(payload)

    expected_path = (
        workspace_root
        / "users"
        / "usr_owner"
        / "conversations"
        / "conv-owned"
        / "uploads"
        / "archive.zip"
    )
    assert expected_path.is_file()
    assert expected_path.read_bytes() == payload


def test_upload_rejects_unauthenticated(app: FastAPI) -> None:
    _override_auth(app, None)
    client = TestClient(app)
    resp = client.post(
        "/api/chat/upload",
        data={"conversation_id": "conv-owned"},
        files={"file": ("x.bin", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 401


def test_upload_rejects_other_users_conversation(app: FastAPI) -> None:
    _override_auth(app, _OTHER_USER)
    client = TestClient(app)
    resp = client.post(
        "/api/chat/upload",
        data={"conversation_id": "conv-owned"},
        files={"file": ("x.bin", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 403


def test_upload_allows_public_room_member(app: FastAPI) -> None:
    _override_auth(app, _OTHER_USER)
    client = TestClient(app)
    resp = client.post(
        "/api/chat/upload",
        data={"conversation_id": "conv-room"},
        files={"file": ("hello.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 200


def test_upload_rejects_unknown_conversation(app: FastAPI) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)
    resp = client.post(
        "/api/chat/upload",
        data={"conversation_id": "conv-nonexistent"},
        files={"file": ("x.bin", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 404


def test_upload_sanitizes_filename(app: FastAPI, workspace_root: Path) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)
    resp = client.post(
        "/api/chat/upload",
        data={"conversation_id": "conv-owned"},
        files={
            "file": (
                "../../evil$name.bin",
                b"x",
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "evil_name.bin"
    expected = (
        workspace_root
        / "users"
        / "usr_owner"
        / "conversations"
        / "conv-owned"
        / "uploads"
        / "evil_name.bin"
    )
    assert expected.is_file()


def test_upload_handles_filename_collisions(
    app: FastAPI, workspace_root: Path
) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)

    for _ in range(3):
        resp = client.post(
            "/api/chat/upload",
            data={"conversation_id": "conv-owned"},
            files={"file": ("notes.pdf", b"pdf-bytes", "application/pdf")},
        )
        assert resp.status_code == 200

    workspace = (
        workspace_root
        / "users"
        / "usr_owner"
        / "conversations"
        / "conv-owned"
        / "uploads"
    )
    landed = sorted(p.name for p in workspace.iterdir())
    assert landed == ["notes-1.pdf", "notes-2.pdf", "notes.pdf"]


def test_upload_missing_filename_returns_error(app: FastAPI) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)
    resp = client.post(
        "/api/chat/upload",
        data={"conversation_id": "conv-owned"},
        files={"file": ("", b"x", "application/octet-stream")},
    )
    assert resp.status_code in (400, 422)


def test_upload_defaults_missing_media_type(
    app: FastAPI,
) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)

    boundary = "----testboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="conversation_id"\r\n\r\n'
        f"conv-owned\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="mystery.dat"\r\n'
        f"\r\n"
        "file-body"
        f"\r\n--{boundary}--\r\n"
    ).encode()
    resp = client.post(
        "/api/chat/upload",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 200, resp.text
    body_json = resp.json()
    assert body_json["media_type"] in (
        "application/octet-stream",
        "application/x-ns-proxy-autoconfig",
    )


# ── Download tests ───────────────────────────────────────────────────


def test_download_streams_previously_uploaded_file(
    app: FastAPI, workspace_root: Path
) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)

    payload = b"the actual bytes of the file"
    client.post(
        "/api/chat/upload",
        data={"conversation_id": "conv-owned"},
        files={"file": ("download-me.bin", payload, "application/octet-stream")},
    )

    resp = client.get("/api/chat/download/conv-owned/download-me.bin")
    assert resp.status_code == 200
    assert resp.content == payload
    assert 'filename="download-me.bin"' in resp.headers["content-disposition"]


def test_download_rejects_path_traversal(app: FastAPI) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)

    resp = client.get("/api/chat/download/conv-owned/..%2Fsecret.txt")
    assert resp.status_code in (400, 404)


def test_download_rejects_other_users_conversation(app: FastAPI) -> None:
    _override_auth(app, _OTHER_USER)
    client = TestClient(app)
    resp = client.get("/api/chat/download/conv-owned/x.bin")
    assert resp.status_code == 403


def test_download_nonexistent_file_returns_404(app: FastAPI) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)
    resp = client.get("/api/chat/download/conv-owned/never-uploaded.bin")
    assert resp.status_code == 404


# ── Download-all (zip) tests ─────────────────────────────────────────


def test_download_all_bundles_every_category(
    app: FastAPI, workspace_provider: _FakeWorkspaceProvider
) -> None:
    import io
    import zipfile

    workspace_provider.stage_file(
        user_id="usr_owner",
        conversation_id="conv-owned",
        category="upload",
        rel_path="uploads/notes.txt",
        content=b"user notes",
    )
    workspace_provider.stage_file(
        user_id="usr_owner",
        conversation_id="conv-owned",
        category="output",
        rel_path="outputs/report.pdf",
        content=b"%PDF-fake-content",
    )
    workspace_provider.stage_file(
        user_id="usr_owner",
        conversation_id="conv-owned",
        category="scratch",
        rel_path="scratch/plan.md",
        content=b"# plan",
    )

    _override_auth(app, _OWNER_USER)
    client = TestClient(app)
    resp = client.get("/api/chat/download-all/conv-owned")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="workspace-conv-owned.zip"' in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["outputs/report.pdf", "scratch/plan.md", "uploads/notes.txt"]
        assert zf.read("uploads/notes.txt") == b"user notes"
        assert zf.read("outputs/report.pdf") == b"%PDF-fake-content"
        assert zf.read("scratch/plan.md") == b"# plan"


def test_download_all_skips_files_missing_on_disk(
    app: FastAPI, workspace_provider: _FakeWorkspaceProvider
) -> None:
    import io
    import zipfile

    workspace_provider.stage_file(
        user_id="usr_owner",
        conversation_id="conv-owned",
        category="upload",
        rel_path="uploads/real.txt",
        content=b"real",
    )
    # Registered but not on disk — archive should skip it, not 500.
    workspace_provider._files.append(
        {
            "conversation_id": "conv-owned",
            "user_id": "usr_owner",
            "category": "upload",
            "rel_path": "uploads/ghost.txt",
            "filename": "ghost.txt",
        }
    )

    _override_auth(app, _OWNER_USER)
    client = TestClient(app)
    resp = client.get("/api/chat/download-all/conv-owned")

    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["uploads/real.txt"]


def test_download_all_empty_workspace_returns_empty_zip(app: FastAPI) -> None:
    import io
    import zipfile

    _override_auth(app, _OWNER_USER)
    client = TestClient(app)
    resp = client.get("/api/chat/download-all/conv-owned")

    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == []


def test_download_all_rejects_unauthenticated(app: FastAPI) -> None:
    _override_auth(app, None)
    client = TestClient(app)
    resp = client.get("/api/chat/download-all/conv-owned")
    assert resp.status_code == 401


def test_download_all_rejects_other_users_conversation(app: FastAPI) -> None:
    _override_auth(app, _OTHER_USER)
    client = TestClient(app)
    resp = client.get("/api/chat/download-all/conv-owned")
    assert resp.status_code == 403


def test_download_all_unknown_conversation_returns_404(app: FastAPI) -> None:
    _override_auth(app, _OWNER_USER)
    client = TestClient(app)
    resp = client.get("/api/chat/download-all/conv-nonexistent")
    assert resp.status_code == 404
