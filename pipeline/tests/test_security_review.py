"""Security review fixes (AI-Video-Content-Factory-review.pdf): host-config
admin gate, path containment, SSRF allowlist, credit ledger, key scoping."""

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import ui.server as srv
from core.state import StateDB
from ui.server import app

client = TestClient(app)


@pytest.fixture
def hosted_ws(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "ui_settings.yaml").write_text(yaml.safe_dump({
        "edition": "web", "hosted_mode": True, "registration_open": True,
        "workers_enabled": True,
    }), encoding="utf-8")
    monkeypatch.setattr(srv, "WORKSPACE", tmp_path)
    monkeypatch.setenv("WORKER_TOKEN", "wt-sec")
    import ui.api_auth as _auth
    monkeypatch.setattr(_auth, "AUTH_MAX", 1000)
    return tmp_path


def _register(client, email="sec@test.io"):
    r = client.post("/api/auth/register", json={
        "accepted_terms": True, "email": email, "password": "secret1"})
    assert r.status_code == 200
    return r.json()["user"]


def test_tenant_cannot_edit_host_settings(hosted_ws):
    """Review crit #1: POST /api/ui_settings must be admin-only."""
    _register(TestClient(app))  # admin (first user)
    regular = TestClient(app)
    regular.post("/api/auth/register", json={"accepted_terms": True,
                                             "email": "pleb@test.io", "password": "secret1"})
    r = regular.post("/api/ui_settings", json={"hosted_mode": False})
    assert r.status_code == 403
    st = yaml.safe_load((hosted_ws / "config" / "ui_settings.yaml").read_text(encoding="utf-8"))
    assert st["hosted_mode"] is True, "tenant must not flip hosted_mode off"


def test_admin_can_edit_host_settings(hosted_ws):
    c = TestClient(app)
    _register(c)
    r = c.post("/api/ui_settings", json={"max_concurrent_runs": 2})
    assert r.status_code == 200


def test_tenant_cannot_save_env(hosted_ws):
    """Review crit #1b: /api/env_save writes the HOST .env — admin only."""
    _register(TestClient(app))  # admin (first user)
    regular = TestClient(app)
    regular.post("/api/auth/register", json={"accepted_terms": True,
                                             "email": "pleb@test.io", "password": "secret1"})
    r = regular.post("/api/env_save", json={"key": "FISH_AUDIO_API_KEY", "value": "stolen"})
    assert r.status_code == 403


def test_cover_set_blocks_traversal(hosted_ws):
    """Review crit #2: 'runs/../.env' must not be copyable into a thumbnail."""
    c = TestClient(app)
    _register(c)
    run_id = "ep-sec"
    (hosted_ws / "data" / "tenants" / "x" / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    # put the run where the owner session can reach it: register owner instead
    r = c.post("/api/cover/set", json={"run_id": run_id,
                                       "path": "runs/../.env"})
    assert r.status_code == 400
    r = c.post("/api/cover/set", json={"run_id": run_id,
                                       "path": "runs/../data/users.db"})
    assert r.status_code == 400


def test_transcript_blocks_non_youtube(hosted_ws):
    """Review crit #7 (SSRF): hostname allowlist, not a substring check."""
    c = TestClient(app)
    _register(c)
    for url in ("https://evil.example.com/youtube.com/watch", "http://127.0.0.1:8765/api/",
                "ftp://youtube.com/x"):
        r = c.post("/api/transcript", json={"url": url})
        assert r.status_code == 400, url


def test_export_dir_must_stay_in_tenant_root(hosted_ws):
    """Review crit #6: export_dir must not escape the tenant data root."""
    c = TestClient(app)
    _register(c)
    r = c.post("/api/episode", json={"topic": "Escape test", "target_minutes": 1,
                                     "export_dir": "C:/Windows/Temp/stolen"})
    assert r.status_code == 400


def test_refund_once_per_run(hosted_ws):
    """Review crit #5: refund must be ledger-backed, exactly once per run."""
    from core import tenants

    # admin (first user, unlimited) + a regular user
    tenants.register_user(hosted_ws, "seed@test.io", "secret1")
    u = tenants.register_user(hosted_ws, "refund@test.io", "secret1")
    run_id = "ep-refund-test"
    st = StateDB(hosted_ws / "data" / "state.db")
    st.create_run(run_id, {"run_id": run_id, "topic": "x"})
    st.close()
    assert tenants.refund_credit_once(hosted_ws, u["id"], run_id) is True
    assert tenants.refund_credit_once(hosted_ws, u["id"], run_id) is False, \
        "second refund for the same run must be blocked by the ledger"
    conn = __import__("sqlite3").connect(hosted_ws / "data" / "users.db")
    creds = conn.execute("SELECT credits FROM users WHERE id=?", (u["id"],)).fetchone()[0]
    conn.close()
    assert creds == 6.0, "5 initial + exactly 1 refund"


def test_worker_artifact_streams_with_cap(hosted_ws, monkeypatch):
    """Review (workers): artifacts must stream with a size cap enforced DURING
    the upload, not read fully into memory first."""
    import ui.api_worker as aw

    monkeypatch.setenv("WORKER_TOKEN", "wt-sec")
    monkeypatch.setattr(aw, "_MAX_ARTIFACT_BYTES", 1024)  # 1 KB cap for the test
    c = TestClient(app)
    c.post("/api/auth/register", json={"accepted_terms": True,
                                       "email": "w@test.io", "password": "secret1"})
    run_id = c.post("/api/episode", json={"topic": "Cap test", "target_minutes": 1}).json()["run_id"]
    claimed = TestClient(app).post("/api/worker/claim", json={"worker_id": "rig"},
                                   headers={"Authorization": "Bearer wt-sec"}).json()
    assert claimed["job"]["run_id"] == run_id
    big = b"x" * 4096  # > 1 KB cap
    r = TestClient(app).put(f"/api/worker/artifact/{run_id}/video/episode.mp4",
                            content=big, headers={"Authorization": "Bearer wt-sec"})
    assert r.status_code == 413
    leftover = hosted_ws / "runs" / run_id / "video" / "episode.mp4.part"
    assert not leftover.exists(), "aborted upload must not leave a .part file"
