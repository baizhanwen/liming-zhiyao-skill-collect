"""Explicit mocked Feishu contract tests. They do not assert live authorization."""
import base64
import copy
import hashlib
import io
import json
import zipfile

import httpx
import pytest

from backend.connectors.feishu import DEFAULT_MAPPING, FeishuConnector, FeishuError, normalize_config
from backend.skills import SkillService
from backend.store import Store
from backend.sync import SyncService, inspect_package


CONFIG = {"app_id": "cli_contract", "app_token": "base_contract", "table_id": "tbl_contract"}


def envelope(data=None, **kwargs):
    return httpx.Response(200, json={"code": 0, "data": data or {}, **kwargs})


def test_connector_token_cache_pagination_and_field_test():
    paths = []
    def handler(request):
        paths.append(str(request.url))
        if request.url.path.endswith("/internal"):
            assert json.loads(request.content) == {"app_id": "cli_contract", "app_secret": "contract_secret"}
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "contract_token", "expire": 7200})
        assert request.headers["authorization"] == "Bearer contract_token"
        if request.url.path.endswith("/fields"):
            return envelope({"items": [{"field_id": "fld_" + k, "field_name": v, "type": 17 if k == "package" else 1}
                for k, v in DEFAULT_MAPPING.items()], "has_more": False})
        if "page_token" not in request.url.params:
            return envelope({"items": [{"record_id": "rec1", "fields": {}}], "has_more": True, "page_token": "next"})
        assert request.url.params["page_token"] == "next"
        return envelope({"items": [{"record_id": "rec2", "fields": {}}], "has_more": False})
    connector = FeishuConnector(CONFIG, "contract_secret", httpx.Client(transport=httpx.MockTransport(handler)))
    result = connector.test_connection()
    assert result["record_count"] == 2 and result["compatible"] and result["write"] == "not_tested"
    assert len([p for p in paths if p.endswith("/internal")]) == 1


def test_oauth_mode_is_explicit():
    def handler(request):
        assert str(request.url) == "https://accounts.feishu.cn/oauth/v3/token"
        assert json.loads(request.content)["grant_type"] == "client_credentials"
        return httpx.Response(200, json={"code": 0, "access_token": "tenant_token", "expires_in": 3600})
    connector = FeishuConnector({**CONFIG, "auth_mode": "oauth3"}, "secret", httpx.Client(transport=httpx.MockTransport(handler)))
    assert connector._access_token() == "tenant_token"


def test_connector_official_attachment_shape_and_download_permission():
    def handler(request):
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "x", "expire": 3600})
        if request.url.path.endswith("upload_all"):
            assert b'name="parent_type"\r\n\r\nbitable_file' in request.content
            assert b'name="parent_node"\r\n\r\nbase_contract' in request.content
            assert b'PK-contract' in request.content
            return envelope({"file_token": "box_contract"})
        if request.url.path.endswith("/fields"):
            return envelope({"items": [{"field_name": "技能包附件", "field_id": "fld_pkg", "type": 17}]})
        assert json.loads(request.url.params["extra"])["bitablePerm"]["attachments"] == {"fld_pkg": {"rec_contract": ["box_contract"]}}
        return httpx.Response(200, content=b"PK-contract", headers={"content-type": "application/zip"})
    c = FeishuConnector(CONFIG, "secret", httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.upload_package(b"PK-contract", "test.zip") == "box_contract"
    assert c.download_package("box_contract", "rec_contract") == b"PK-contract"


def test_connector_write_test_record_is_marked_and_removed():
    seen = []
    def handler(request):
        seen.append((request.method, str(request.url)))
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "x", "expire": 3600})
        if request.url.path.endswith("/fields"):
            return envelope({"items": [{"field_name": v, "type": 17 if k == "package" else 1} for k, v in DEFAULT_MAPPING.items()]})
        if request.method == "POST":
            assert json.loads(request.content)["fields"]["Skill ID"].startswith("__skill_collect_write_test__")
            return envelope({"record": {"record_id": "rec_write_test"}})
        return envelope({"items": []})
    c = FeishuConnector(CONFIG, "secret", httpx.Client(transport=httpx.MockTransport(handler)))
    result = c.test_connection(write_test=True)
    assert result["write"] == "passed_and_test_record_deleted"
    assert seen[-1][0] == "DELETE" and seen[-1][1].endswith("rec_write_test")


def test_connector_errors_are_sanitized_and_config_is_not_arbitrary_url():
    c = FeishuConnector(CONFIG, "do_not_log_secret", httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"code": 99991663, "msg": "do_not_log_secret"}))))
    with pytest.raises(FeishuError) as caught:
        c.list_records()
    assert caught.value.code == "permission" and "do_not_log_secret" not in str(caught.value)
    with pytest.raises(FeishuError):
        normalize_config({**CONFIG, "table_url": "http://127.0.0.1/base/x"})
    assert normalize_config({**CONFIG, "table_url": "https://example.feishu.cn/base/abc?table=tbl_xyz"})["table_id"] == "tbl_xyz"


class Vault:
    def get(self, key):
        return "contract-secret"


class MockBase:
    """Stateful explicitly mocked remote with full ZIP attachments."""
    def __init__(self):
        self.config, self.mapping = CONFIG, DEFAULT_MAPPING
        self.records, self.packages = {}, {}
        self.creates = self.updates = 0
        self.timeout_after_create = False
    def close(self):
        pass
    def list_records(self):
        return copy.deepcopy(list(self.records.values()))
    def fields(self):
        return [{"field_name": v, "type": 17 if k == "package" else 1} for k, v in self.mapping.items()]
    def upload_package(self, data, name):
        token = "box" + str(len(self.packages))
        self.packages[token] = data
        return token
    def download_package(self, token, record_id=None):
        if token not in self.packages:
            raise FeishuError("attachment_missing", "契约测试：附件不可用")
        return self.packages[token]
    def create_record(self, fields):
        self.creates += 1
        record = {"record_id": "rec" + str(self.creates), "fields": copy.deepcopy(fields)}
        self.records[record["record_id"]] = record
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise FeishuError("timeout", "契约测试：服务已创建但客户端超时", True)
        return record
    def update_record(self, record_id, fields):
        self.updates += 1
        self.records[record_id]["fields"].update(copy.deepcopy(fields))
        return copy.deepcopy(self.records[record_id])


@pytest.fixture
def sync_env(tmp_path):
    store = Store(tmp_path)
    store.put("connections", {"id": "feishu", **CONFIG})
    skills = SkillService(store)
    local = skills.create({"name": "contract-skill", "body": "# Contract\n\n真实测试材料\n", "tags": ["test"],
        "files": [{"path": "scripts/demo.py", "content": base64.b64encode(b"print('not executed')\n").decode(), "encoding": "base64"}]})
    remote = MockBase()
    service = SyncService(store, Vault(), skills, lambda config, secret: remote)
    return store, skills, local, remote, service


def test_full_package_push_no_duplicate_and_preserves_scripts(sync_env):
    store, skills, local, remote, service = sync_env
    assert service.preview()["items"][0]["action"] == "create"
    first = service.push(local["id"])
    assert first["action"] == "created"
    assert service.push(local["id"])["action"] == "noop" and remote.creates == 1
    package = next(iter(remote.packages.values()))
    assert "scripts/demo.py" in inspect_package(package)["file_hashes"]
    # Actual remote body was edited without touching app-written hash/time fields.
    remote.records[first["record_id"]]["fields"]["Skill 正文"] += "远端直接编辑\n"
    assert service.preview()["items"][0]["action"] == "pull"
    pulled = service.pull(first["record_id"])
    assert "远端直接编辑" in pulled["skill"]["body"]
    assert any(f["path"] == "scripts/demo.py" for f in pulled["skill"]["files"])
    assert service.preview()["items"][0]["action"] == "noop"
    assert len(service.logs()) >= 3


def test_two_sides_modified_conflict_and_resolution(sync_env):
    store, skills, local, remote, service = sync_env
    record_id = service.push(local["id"])["record_id"]
    skills.update(local["id"], {"body": local["body"] + "个人修改\n"})
    remote.records[record_id]["fields"]["Skill 正文"] += "企业修改\n"
    preview = service.preview()["items"][0]
    assert preview["action"] == "conflict" and "企业修改" in preview["diff"] and "个人修改" in preview["diff"]
    with pytest.raises(FeishuError) as error:
        service.push(local["id"])
    assert error.value.code == "conflict"
    assert service.push(local["id"], resolution="local")["action"] == "updated"
    assert "个人修改" in remote.records[record_id]["fields"]["Skill 正文"]
    assert service.preview()["items"][0]["action"] == "noop"


def test_metadata_change_detected_and_copy_does_not_overwrite(sync_env):
    store, skills, local, remote, service = sync_env
    record_id = service.push(local["id"])["record_id"]
    skills.update(local["id"], {"name": "personal-renamed"})
    assert service.preview()["items"][0]["action"] == "push"
    remote.records[record_id]["fields"]["名称"] = "企业改名"
    assert service.preview()["items"][0]["action"] == "conflict"
    copied = service.pull(record_id, resolution="copy")["skill"]
    assert copied["id"] != local["id"] and copied["name"] == "企业改名"
    assert skills.get(local["id"])["name"] == "personal-renamed"
    assert copied["origin"]["record_id"] == record_id


def test_timeout_reconciles_by_stable_id_without_duplicate_create(sync_env):
    store, skills, local, remote, service = sync_env
    remote.timeout_after_create = True
    with pytest.raises(FeishuError):
        service.push(local["id"])
    assert remote.creates == 1
    assert store.get("sync_pending", "feishu:" + local["id"])["uncertain"]
    # A new service simulates retry after application restart.
    retried = SyncService(store, Vault(), skills, lambda cfg, secret: remote)
    retried.push(local["id"])
    assert remote.creates == 1 and len(remote.records) == 1
    assert retried.preview()["items"][0]["action"] == "noop"


def test_missing_attachment_is_blocked_not_success(sync_env):
    store, skills, local, remote, service = sync_env
    record_id = service.push(local["id"])["record_id"]
    remote.records[record_id]["fields"]["技能包附件"] = [{"file_token": "box_missing"}]
    preview = service.preview()["items"][0]
    assert preview["action"] == "blocked" and preview["error_code"] == "attachment_missing"
    with pytest.raises(FeishuError):
        service.pull(record_id, resolution="remote")
    assert skills.get(local["id"])["body"] == local["body"]


def test_two_distinct_remote_imports_keep_distinct_origin(sync_env):
    store, skills, local, remote, service = sync_env
    first = service.push(local["id"])["record_id"]
    second_local = skills.create({"name": "second", "body": "# Second\n"})
    second = service.push(second_local["id"])["record_id"]
    # Simulate new personal workspace that can only see the same enterprise table.
    other_store = Store(store.root / "other")
    other_store.put("connections", {"id": "feishu", **CONFIG})
    other_skills = SkillService(other_store)
    other = SyncService(other_store, Vault(), other_skills, lambda cfg, secret: remote)
    a, b = other.pull(first)["skill"], other.pull(second)["skill"]
    assert a["id"] != b["id"] and len(other_store.list("skills")) == 2


def test_deleted_remote_is_not_automatically_recreated_or_deleted(sync_env):
    store, skills, local, remote, service = sync_env
    record_id = service.push(local["id"])["record_id"]
    del remote.records[record_id]
    assert service.preview()["items"][0]["action"] == "remote_missing"
    with pytest.raises(FeishuError):
        service.push(local["id"])
    assert skills.get(local["id"])["body"]


def test_package_manifest_tamper_and_path_traversal(sync_env):
    _, skills, local, _, _ = sync_env
    package = skills.export_zip(local["id"])
    with zipfile.ZipFile(io.BytesIO(package)) as original:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as broken:
            for item in original.infolist():
                content = original.read(item.filename)
                broken.writestr(item.filename, content + b"tampered" if item.filename.endswith("SKILL.md") else content)
    with pytest.raises(FeishuError) as error:
        inspect_package(out.getvalue())
    assert error.value.code == "package_hash"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("../SKILL.md", "unsafe")
    with pytest.raises(FeishuError):
        inspect_package(out.getvalue())


def test_feishu_api_with_real_store_skill_package_and_sync_services(tmp_path):
    """Only the remote is mocked; production API/store/import/export/sync all execute."""
    from fastapi.testclient import TestClient
    from backend.main import create_app
    remote = MockBase()
    app = create_app(tmp_path, start_worker=False, connector_factory=lambda cfg, secret: remote)
    with TestClient(app) as client:
        saved = client.put("/api/settings", json={"feishu": {**CONFIG, "app_secret": "synthetic-api-secret"}})
        assert saved.status_code == 200
        assert saved.json()["feishu"]["secret_configured"]
        assert "synthetic-api-secret" not in saved.text
        local = client.post("/api/skills", json={"name": "api-contract", "body": "# API\n\nOriginal\n",
            "files": [{"path": "scripts/example.py", "content": base64.b64encode(b"print('never executed')\n").decode(), "encoding": "base64"}]}).json()
        sid = local["id"]
        assert client.post("/api/feishu/preview", json={"skill_ids": [sid]}).json()["items"][0]["action"] == "create"
        first = client.post("/api/feishu/sync", json={"skill_ids": [sid]}).json()["items"][0]
        assert first["action"] == "created"
        assert client.post("/api/feishu/sync", json={"skill_ids": [sid]}).json()["items"][0]["action"] == "noop"
        assert remote.creates == 1
        rid = first["record_id"]
        remote.records[rid]["fields"]["Skill 正文"] += "Remote direct edit\n"
        assert client.post("/api/feishu/preview", json={"skill_ids": [sid]}).json()["items"][0]["action"] == "pull"
        pulled = client.post("/api/feishu/import", json={"record_ids": [rid]}).json()["items"][0]
        assert "Remote direct edit" in pulled["skill"]["body"]
        restored = inspect_package(client.get(f"/api/skills/{sid}/export").content)
        assert "scripts/example.py" in restored["file_hashes"]
        assert "Remote direct edit" in restored["body"]
        assert client.get("/api/feishu/logs").json()


def test_remote_changed_while_package_uploading_is_not_overwritten(sync_env):
    _, skills, local, remote, service = sync_env
    record_id = service.push(local["id"])["record_id"]
    skills.update(local["id"], {"body": local["body"] + "Personal change\n"})
    original_upload = remote.upload_package
    def changed_during_upload(data, name):
        remote.records[record_id]["fields"]["Skill 正文"] += "Concurrent enterprise edit\n"
        return original_upload(data, name)
    remote.upload_package = changed_during_upload
    with pytest.raises(FeishuError) as error:
        service.push(local["id"])
    assert error.value.code == "conflict"
    assert remote.updates == 0
    assert "Concurrent enterprise edit" in remote.records[record_id]["fields"]["Skill 正文"]
    assert "Personal change" not in remote.records[record_id]["fields"]["Skill 正文"]
