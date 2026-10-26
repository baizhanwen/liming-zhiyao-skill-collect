"""Regression cases found by independent review; all inputs are synthetic test data."""
import io
import zipfile

import pytest
from pydantic import ValidationError

from backend.models import Claim
from backend.security import AppError, Vault
from backend.skills import SkillService
from backend.store import Store
from backend.tasks import TaskRunner


def test_review_noncanonical_zip_paths_cannot_alias(tmp_path):
    service = SkillService(Store(tmp_path))
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("SKILL.md", "# Test\n")
        archive.writestr("references/a.txt", "first")
        archive.writestr("references//a.txt", "second")
    with pytest.raises(AppError):
        service.import_zip(data.getvalue())


def test_review_model_cannot_grant_execution_verification():
    with pytest.raises(ValidationError):
        Claim(text="generated claim", evidence_ids=["source1"], kind="runtime_verified")


def test_review_supplement_invalidates_media_and_draft_checkpoints(tmp_path):
    store = Store(tmp_path)
    runner = TaskRunner(store, Vault(tmp_path), object(), lambda: None)
    task = runner.create("https://www.douyin.com/video/contract")
    runner.checkpoint(task["id"], status="failed", media_complete=True, source={"text": "old"},
        media={"frames": []}, analysis={"overview": "old"}, skill_id="old_generated_skill")
    result = runner.attach(task["id"], {"path": str(tmp_path / "new.mp4"), "name": "new.mp4"})
    assert not result.get("media_complete")
    assert not result.get("analysis") and not result.get("skill_id")


def test_review_completed_retry_does_not_overwrite_user_knowledge(tmp_path):
    store = Store(tmp_path)
    runner = TaskRunner(store, Vault(tmp_path), object(), lambda: None)
    task = runner.create("https://github.com/example/repository")
    runner.checkpoint(task["id"], status="succeeded", knowledge_id=task["id"])
    try:
        result = runner.retry(task["id"])
    except AppError as exc:
        assert exc.code in ("task_complete", "task_state")
    else:
        assert result["status"] == "succeeded"


def test_review_vault_credentials_do_not_enter_plaintext_database(tmp_path):
    store = Store(tmp_path)
    vault = Vault(tmp_path)
    token = "synthetic-secret-only-for-contract-test"
    vault.set("github_token", token)
    assert vault.get("github_token") == token
    assert token.encode() not in store.path.read_bytes()
    assert token.encode() not in vault.path.read_bytes()
    assert token not in vault.redact("Bearer " + token)


def test_review_cancelled_active_request_cannot_be_retried_or_supplemented(tmp_path):
    import threading
    started, release = threading.Event(), threading.Event()
    class GitHub:
        def snapshot(self, url, depth):
            return {"title": "contract-source", "text": "synthetic source", "platform": "github", "files": [], "evidence": []}
    class Models:
        def analyze(self, source):
            started.set()
            assert release.wait(3), "test did not release mocked request"
            return {"overview": "synthetic analysis", "steps": ["test step"]}
    store = Store(tmp_path)
    runner = TaskRunner(store, Vault(tmp_path), Models(), GitHub)
    task = runner.create("https://github.com/example/repository")
    thread = threading.Thread(target=runner.run, args=(task["id"],), daemon=True)
    thread.start()
    try:
        assert started.wait(2)
        runner.cancel(task["id"])
        with pytest.raises(AppError) as retry_error:
            runner.retry(task["id"])
        assert retry_error.value.code == "task_stopping"
        with pytest.raises(AppError) as attach_error:
            runner.attach(task["id"], {"path": str(tmp_path / "new.srt"), "name": "new.srt"})
        assert attach_error.value.code == "task_stopping"
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert store.get("tasks", task["id"])["status"] == "cancelled"
    assert store.get("knowledge", task["id"]) is None
    # Once the old request has finished, explicit retry can be queued safely.
    assert runner.retry(task["id"])["status"] == "queued"


def test_review_public_transport_does_not_mutate_request_or_relative_redirects(monkeypatch):
    import httpx
    import backend.security as security
    monkeypatch.setattr(security, "public_url", lambda url: ["93.184.216.34"])
    seen = []
    def origin(request):
        seen.append(str(request.url))
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "public.example"
        assert request.extensions["sni_hostname"] == "public.example"
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/end"})
        return httpx.Response(200, text="synthetic public response")
    transport = security.PublicTransport()
    transport.transport.close()
    transport.transport = httpx.MockTransport(origin)
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        response = client.get("https://public.example/start")
    assert response.url.host == "public.example"
    assert str(response.url) == "https://public.example/end"
    assert len(seen) == 2


def test_review_fake_mp4_hls_cannot_fetch_loopback_resources(tmp_path):
    import http.server
    import shutil
    import threading
    from backend.media import inspect_video
    if not shutil.which("ffprobe"):
        pytest.skip("requires optional FFprobe; other core tests do not depend on it")
    seen = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"synthetic invalid media")
        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    media = tmp_path / "disguised.mp4"
    media.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
        + f"http://127.0.0.1:{server.server_port}/private-resource.ts\n#EXT-X-ENDLIST\n")
    try:
        with pytest.raises(AppError):
            inspect_video(media)
        assert seen == [], "uploaded manifest must not fetch any network resource"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
