"""Opt-in real Feishu acceptance against the configured dedicated table.

Default prints a plan and performs no I/O. --check-config reads localhost only.
--run requires the exact target fingerprint; it calls real Feishu and cleans only
records proven to be created by this run. Never calls a model or prints secrets.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from backend.connectors.feishu import FeishuConnector, normalize_config
from backend.security import Vault
from backend.sync import digest, inspect_package, text

PRIVATE_ROOT = ROOT / "artifacts" / "private"
PUBLIC_OUTPUT = ROOT / "artifacts" / "verification" / "feishu-live.json"
PLAN = [
    "Read local configuration booleans and bind the exact target SHA-256",
    "Authenticate/read fields; run a marked write test and confirm cleanup",
    "Create one self-authored acceptance skill with a reference and 256-byte binary",
    "First sync, repeated noop, real attachment download and per-file hash recovery",
    "Update the owned personal skill and sync; import a separate personal copy",
    "Edit only this run's remote body, import read-only, explicitly refresh its ZIP",
    "Create two-sided conflicts; reject unchosen writes; resolve local and remote",
    "Check three-way body/file consistency, stable remote Skill ID and sync logs",
    "Delete only owned remote records; archive only owned local skills",
    "Confirm all pre-existing records, including blank placeholder rows, unchanged",
]


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def sha(value: bytes | str):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class VerificationFailure(Exception):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def require(condition, code):
    if not condition:
        raise VerificationFailure(code)


def safe_code(exc):
    # Never stringify provider responses, target identifiers or arbitrary errors.
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code.replace("_", "").isalnum() else type(exc).__name__


def local_base(value):
    parsed = urlsplit(value)
    require(parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
        and not parsed.username and not parsed.password and not parsed.query
        and not parsed.fragment and parsed.path in ("", "/"), "local_api_only")
    return value.rstrip("/")


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


def target_config(settings):
    config = normalize_config(settings.get("feishu", {}))
    identity = {k: config[k] for k in ("app_id", "app_token", "table_id")}
    return config, sha(canonical(identity))


class LocalAPI:
    def __init__(self, base):
        self.client = httpx.Client(base_url=local_base(base), timeout=180,
            follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def request(self, method, path, *, raw=False, **kwargs):
        require(path.startswith("/api/"), "local_api_path")
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise VerificationFailure("local_api_network") from None
        if response.is_error:
            try:
                code = response.json().get("error", {}).get("code", "local_api_error")
            except (ValueError, AttributeError):
                code = "local_api_error"
            raise VerificationFailure(code)
        if raw:
            return response.content
        try:
            result = response.json()
        except ValueError:
            raise VerificationFailure("local_api_protocol") from None
        if isinstance(result, dict) and result.get("error"):
            raise VerificationFailure(result["error"].get("code", "local_api_error"))
        return result


def check_configuration(base):
    api = LocalAPI(base)
    try:
        settings = api.request("GET", "/api/settings")
        config, fingerprint = target_config(settings)
        return {"target_sha256": fingerprint,
            "app_id_configured": bool(config.get("app_id")),
            "app_token_configured": bool(config.get("app_token")),
            "table_id_configured": bool(config.get("table_id")),
            "secret_configured": bool(settings["feishu"].get("secret_configured")),
            "external_requests": False}
    finally:
        api.close()


class LiveVerifier:
    def __init__(self, base, expected_target, state_path=None):
        self.api = LocalAPI(base)
        settings = self.api.request("GET", "/api/settings")
        self.config, fingerprint = target_config(settings)
        require(fingerprint == expected_target, "target_fingerprint_mismatch")
        require(settings["feishu"].get("secret_configured") is True, "secret_not_configured")
        data_root = Path(settings.get("storage", {}).get("data_path", "")).resolve()
        require(data_root.is_dir() and (data_root / "app.sqlite3").is_file()
            and (data_root / ".secrets.key").is_file() and (data_root / "secrets.enc").is_file(),
            "local_credential_store_missing")
        secret = Vault(data_root).get("feishu_secret")
        require(bool(secret), "secret_not_configured")
        self.connector = FeishuConnector(self.config, secret)
        if state_path:
            resolved = Path(state_path).resolve()
            require(resolved.is_relative_to(PRIVATE_ROOT.resolve()) and resolved.name == "state.json", "private_state_path_required")
            self.state_path = resolved
            self.state = json.loads(resolved.read_text())
            require(self.state.get("target_sha256") == fingerprint, "cleanup_target_mismatch")
        else:
            run_id = uuid.uuid4().hex
            self.state_path = PRIVATE_ROOT / ("feishu-live-" + run_id) / "state.json"
            self.state = {"format_version": 1, "run_id": run_id, "created_at": timestamp(),
                "target_sha256": fingerprint, "target": {k: self.config[k] for k in ("app_id", "app_token", "table_id")},
                "marker": "SELF_AUTHORED_SKILL_COLLECT_ACCEPTANCE_" + run_id,
                "local_skill_ids": [], "remote_record_ids": [], "write_test_record_ids": [],
                "baseline_record_hashes": {}, "steps": [], "status": "running"}
            self.persist()

    def persist(self):
        self.state["updated_at"] = timestamp()
        write_private(self.state_path, self.state)

    def ensure_target(self):
        config, fingerprint = target_config(self.api.request("GET", "/api/settings"))
        require(fingerprint == self.state["target_sha256"], "target_changed_during_run")
        require(config["field_mapping"] == self.config["field_mapping"]
            and config.get("auth_mode", "legacy") == self.config.get("auth_mode", "legacy"),
            "connection_mapping_changed_during_run")

    def step(self, name, **evidence):
        # Callers supply only step results, hashes, booleans and times here.
        self.state["steps"].append({"step": name, "status": "passed", "at": timestamp(), **evidence})
        self.persist()
        print(name + ": passed", flush=True)

    def records(self):
        self.ensure_target()
        return self.connector.list_records()

    def own(self, record):
        fields = record.get("fields", {})
        stable_id = text(fields.get(self.connector.mapping["skill_id"]))
        body = text(fields.get(self.connector.mapping["body"]))
        return (record["record_id"] not in self.state["baseline_record_hashes"]
            and stable_id in self.state["local_skill_ids"] and self.state["marker"] in body)

    def owned_record(self, record_id):
        require(record_id in self.state["remote_record_ids"], "record_not_registered_to_run")
        record = next((r for r in self.records() if r["record_id"] == record_id), None)
        require(record is not None and self.own(record), "record_ownership_not_proven")
        return record

    def register_local(self, skill):
        require(self.state["marker"] in skill.get("body", ""), "local_skill_ownership_not_proven")
        if skill["id"] not in self.state["local_skill_ids"]:
            self.state["local_skill_ids"].append(skill["id"])
            self.persist()
        return skill

    def preview(self, skill_id):
        self.ensure_target()
        result = self.api.request("POST", "/api/feishu/preview", json={"skill_ids": [skill_id]})
        rows = result.get("items", [])
        require(len(rows) == 1, "preview_expected_one_owned_skill")
        return rows[0]

    def sync(self, skill_id, resolution=None, expected_error=None):
        require(skill_id in self.state["local_skill_ids"], "local_skill_not_owned")
        self.ensure_target()
        result = self.api.request("POST", "/api/feishu/sync", json={"skill_ids": [skill_id],
            "resolutions": {skill_id: resolution} if resolution else {}})
        rows = result.get("items", [])
        require(len(rows) == 1, "sync_expected_one_result")
        item = rows[0]
        if expected_error:
            require(item.get("error", {}).get("code") == expected_error, "expected_sync_error_missing")
            return item
        if item.get("error"):
            self.capture_failure(skill_id, item)
            raise VerificationFailure(item["error"].get("code", "sync_item_error"))
        if item.get("record_id") and item["record_id"] not in self.state["remote_record_ids"]:
            self.state["remote_record_ids"].append(item["record_id"])
            self.persist()
        if item.get("skill"):
            self.register_local(item["skill"])
        return item

    def capture_failure(self, skill_id, api_result):
        """Owned fixture diagnostics only, written before finally-cleanup runs."""
        diagnostic = {"captured_at": timestamp(), "api_result": api_result}
        try:
            self.ensure_target()
            diagnostic["local_skill"] = self.api.request("GET", "/api/skills/" + skill_id)
            diagnostic["preview"] = self.preview(skill_id)
            owned = [r for r in self.connector.list_records() if self.own(r)]
            diagnostic["owned_remote"] = []
            for index, record in enumerate(owned):
                fields = record.get("fields", {})
                normalized = {k: text(fields.get(v)) for k, v in self.connector.mapping.items()
                    if v and k != "package"}
                raw = {k: fields.get(v) for k, v in self.connector.mapping.items()
                    if v and k != "package"}
                entry = {"record_id": record["record_id"], "raw_fields": fields,
                    "normalized_fields": normalized,
                    "raw_field_shapes": {k: type(v).__name__ for k, v in raw.items()}}
                attachments = fields.get(self.connector.mapping["package"], [])
                if isinstance(attachments, list) and len(attachments) == 1 and isinstance(attachments[0], dict):
                    data = self.connector.download_package(attachments[0]["file_token"], record["record_id"])
                    package = inspect_package(data)
                    entry.update(file_hashes=package["file_hashes"],
                        raw_remote_sha256=digest({"fields": raw, "files": package["file_hashes"]}),
                        normalized_remote_sha256=digest({"fields": normalized, "files": package["file_hashes"]}))
                    package_path = self.state_path.parent / ("failure-owned-attachment-" + str(index) + ".zip")
                    package_path.write_bytes(data)
                    if os.name != "nt":
                        package_path.chmod(0o600)
                diagnostic["owned_remote"].append(entry)
        except Exception as exc:
            diagnostic["diagnostic_error_code"] = safe_code(exc)
        filename = "failure-diagnostics-" + str(len(self.state.get("failure_diagnostics", []))) + ".json"
        write_private(self.state_path.parent / filename, diagnostic)
        self.state.setdefault("failure_diagnostics", []).append(filename)
        self.persist()

    def import_record(self, record_id, resolution=None, expected_error=None):
        self.owned_record(record_id)
        result = self.api.request("POST", "/api/feishu/import", json={"record_ids": [record_id],
            "resolutions": {record_id: resolution} if resolution else {}})
        rows = result.get("items", [])
        require(len(rows) == 1, "import_expected_one_result")
        item = rows[0]
        if expected_error:
            require(item.get("error", {}).get("code") == expected_error, "expected_import_error_missing")
            return item
        if item.get("error"):
            raise VerificationFailure(item["error"].get("code", "import_item_error"))
        return {**item, "skill": self.register_local(item["skill"])}

    def update_local(self, skill_id, body):
        require(skill_id in self.state["local_skill_ids"] and self.state["marker"] in body, "local_write_not_owned")
        self.ensure_target()
        old = self.api.request("GET", "/api/skills/" + skill_id)
        require(self.state["marker"] in old.get("body", ""), "local_ownership_changed")
        return self.api.request("PUT", "/api/skills/" + skill_id,
            json={"body": body, "expected_version": old["version"]})

    def update_remote_body(self, record_id, body):
        self.owned_record(record_id)
        require(self.state["marker"] in body, "remote_write_not_owned")
        self.ensure_target()
        self.connector.update_record(record_id, {self.connector.mapping["body"]: body})

    def compare_attachment(self, skill_id, record_id, label):
        record = self.owned_record(record_id)
        local = self.api.request("GET", "/api/skills/" + skill_id)
        exported = self.api.request("GET", "/api/skills/" + skill_id + "/export", raw=True)
        local_package = inspect_package(exported)
        attachments = record["fields"].get(self.connector.mapping["package"], [])
        require(len(attachments) == 1, "expected_one_complete_attachment")
        data = self.connector.download_package(attachments[0]["file_token"], record_id)
        restored = inspect_package(data)
        require(restored["file_hashes"] == local_package["file_hashes"], "attachment_file_hash_mismatch")
        require(restored["body"] == local["body"] == text(record["fields"].get(self.connector.mapping["body"])),
            "three_way_body_mismatch")
        require(restored["file_hashes"].get("references/acceptance.txt") == sha(self.reference()), "reference_hash_mismatch")
        require(restored["file_hashes"].get("templates/probe.bin") == sha(bytes(range(256))), "binary_hash_mismatch")
        require(text(record["fields"].get(self.connector.mapping["skill_id"])) == self.state["primary_skill_id"],
            "remote_stable_skill_id_changed")
        path = self.state_path.parent / (label + ".zip")
        path.write_bytes(data)
        if os.name != "nt":
            path.chmod(0o600)
        self.step(label, file_hashes=restored["file_hashes"], zip_sha256=sha(data),
            body_sha256=sha(local["body"]), content_sha256=local["content_hash"])

    def reference(self):
        return ("Self-authored Feishu acceptance reference.\n" + self.state["marker"] + "\n").encode()

    def run(self):
        baseline = self.records()
        self.state["baseline_record_hashes"] = {r["record_id"]: sha(canonical(r.get("fields", {}))) for r in baseline}
        self.persist()
        self.step("target_bound_and_preexisting_rows_snapshotted", target_sha256=self.state["target_sha256"])
        self.ensure_target()
        read = self.api.request("POST", "/api/feishu/test", json={"write_test": False})
        require(read.get("authentication") == "passed" and read.get("read") == "passed" and read.get("compatible"),
            "read_or_field_compatibility_failed")
        self.step("real_authentication_read_and_field_types")
        self.ensure_target()
        write = self.api.request("POST", "/api/feishu/test", json={"write_test": True})
        if write.get("write_test_record"):
            self.state["write_test_record_ids"].append(write["write_test_record"])
            self.persist()
        require(write.get("write") == "passed_and_test_record_deleted", "write_test_or_cleanup_failed")
        self.step("marked_write_test_created_and_deleted")
        slug = "acceptance-" + self.state["run_id"][:12]
        body = (f"---\nname: {slug}\ndescription: Self-authored Feishu sync acceptance fixture.\n---\n\n"
            "# Self-authored acceptance skill\n\n" + self.state["marker"] + "\n\n"
            "Trigger: maintainers explicitly run the dedicated-table acceptance.\n"
            "Inputs: this fixture and its reference/binary files.\n"
            "Steps: inspect content hashes; never execute imported scripts.\n"
            "Output: an auditable sync result. Verification: compare all file hashes.\n"
            "Failure: stop and clean up only this run's records.\n"
            "Source: repository-authored acceptance fixture; no model involved.\n")
        skill = self.register_local(self.api.request("POST", "/api/skills", json={"name": slug,
            "description": "明确标记、自行编写的飞书验收技能；不调用模型、不执行脚本。",
            "body": body, "tags": ["acceptance-fixture"], "category": "验收材料",
            "source": {"platform": "self_authored_acceptance"},
            "files": [{"path": "references/acceptance.txt", "content": base64.b64encode(self.reference()).decode(), "encoding": "base64"},
                {"path": "templates/probe.bin", "content": base64.b64encode(bytes(range(256))).decode(), "encoding": "base64"}]}))
        sid = skill["id"]
        self.state["primary_skill_id"] = sid
        self.persist()
        require(self.preview(sid)["action"] == "create", "first_preview_not_create")
        first = self.sync(sid)
        require(first.get("action") == "created", "first_sync_not_created")
        rid = first["record_id"]
        self.owned_record(rid)
        self.step("first_owned_record_created")
        require(self.sync(sid).get("action") == "noop", "duplicate_sync_not_noop")
        matches = [r for r in self.records() if self.own(r)]
        require(len(matches) == 1, "duplicate_owned_record_created")
        self.step("duplicate_sync_no_new_record")
        self.compare_attachment(sid, rid, "first_attachment_download_and_hash_recovery")

        updated = self.update_local(sid, skill["body"] + "\nPersonal revision one.\n")
        require(self.preview(sid)["action"] == "push", "personal_update_not_detected")
        require(self.sync(sid).get("action") == "updated", "personal_update_not_written")
        self.compare_attachment(sid, rid, "personal_update_roundtrip")

        self.update_remote_body(rid, updated["body"] + "\nRemote-only revision.\n")
        require(self.preview(sid)["action"] == "pull", "remote_direct_edit_not_detected")
        remote_before_pull = sha(canonical(self.owned_record(rid)["fields"]))
        pulled = self.import_record(rid)
        require(pulled.get("package_refresh_required") is True, "stale_attachment_not_flagged")
        require(sha(canonical(self.owned_record(rid)["fields"])) == remote_before_pull, "ordinary_pull_wrote_remote")
        require(self.preview(sid)["action"] == "refresh_package", "stale_package_claimed_noop")
        self.step("remote_direct_edit_import_is_read_only_and_flags_stale_package")
        require(self.sync(sid).get("action") == "package_refreshed", "explicit_package_refresh_failed")
        self.compare_attachment(sid, rid, "explicit_refresh_makes_three_bodies_consistent")

        current = self.api.request("GET", "/api/skills/" + sid)
        self.update_local(sid, current["body"] + "\nPersonal conflict A.\n")
        self.update_remote_body(rid, current["body"] + "\nRemote conflict A.\n")
        require(self.preview(sid)["action"] == "conflict", "two_sided_conflict_not_detected")
        before_blocked = sha(canonical(self.owned_record(rid)["fields"]))
        self.sync(sid, expected_error="conflict")
        require(sha(canonical(self.owned_record(rid)["fields"])) == before_blocked, "unresolved_conflict_wrote_remote")
        require(self.sync(sid, "local").get("action") == "updated", "local_resolution_failed")
        self.compare_attachment(sid, rid, "local_conflict_resolution_roundtrip")
        self.step("two_sided_conflict_blocks_unchosen_write")

        current = self.api.request("GET", "/api/skills/" + sid)
        self.update_local(sid, current["body"] + "\nPersonal conflict B.\n")
        chosen_remote = current["body"] + "\nChosen remote conflict B.\n"
        self.update_remote_body(rid, chosen_remote)
        require(self.preview(sid)["action"] == "conflict", "second_conflict_not_detected")
        resolved = self.sync(sid, "remote")
        require(resolved.get("action") == "resolved_remote" and resolved.get("package_refreshed"), "remote_resolution_package_not_refreshed")
        require(self.api.request("GET", "/api/skills/" + sid)["body"] == chosen_remote, "remote_resolution_body_not_kept")
        self.compare_attachment(sid, rid, "remote_conflict_resolution_and_complete_package_refresh")
        require(self.preview(sid)["action"] == "noop", "resolved_versions_not_converged")
        logs = self.api.request("GET", "/api/feishu/logs")
        require(any(log.get("record_id") == rid for log in logs), "owned_sync_logs_missing")
        self.step("final_noop_stable_remote_identity_and_audit_logs")
        # Copy at the end: it is intentionally never uploaded, and this verifier
        # never relies on the server choosing between two associated local copies.
        # A separate personal copy validates actual import through the production API.
        before_import = sha(canonical(self.owned_record(rid)["fields"]))
        copied = self.import_record(rid, "copy")["skill"]
        require(copied["id"] != sid, "personal_copy_not_distinct")
        require(sha(canonical(self.owned_record(rid)["fields"])) == before_import, "copy_import_wrote_remote")
        self.compare_attachment(copied["id"], rid, "complete_attachment_imported_as_personal_copy")
        primary_before = self.api.request("GET", "/api/skills/" + sid)
        copy_before = self.api.request("GET", "/api/skills/" + copied["id"])
        remote_before = sha(canonical(self.owned_record(rid)["fields"]))
        self.import_record(rid, expected_error="ambiguous_local_copies")
        require(self.api.request("GET", "/api/skills/" + sid) == primary_before,
            "ambiguous_import_changed_primary")
        require(self.api.request("GET", "/api/skills/" + copied["id"]) == copy_before,
            "ambiguous_import_changed_copy")
        require(sha(canonical(self.owned_record(rid)["fields"])) == remote_before,
            "ambiguous_import_changed_remote")
        self.step("ambiguous_enterprise_import_refused_without_changing_personal_copies",
            primary_content_sha256=primary_before["content_hash"], copy_content_sha256=copy_before["content_hash"])

        self.update_local(sid, primary_before["body"] + "\nPrimary targeted personal conflict after copy.\n")
        chosen_targeted = primary_before["body"] + "\nTargeted enterprise choice with two personal copies.\n"
        self.update_remote_body(rid, chosen_targeted)
        require(self.preview(sid)["action"] == "conflict", "targeted_primary_conflict_not_detected")
        targeted = self.sync(sid, "remote")
        require(targeted.get("action") == "resolved_remote" and targeted.get("package_refreshed")
            and targeted.get("skill", {}).get("id") == sid, "remote_resolution_updated_wrong_personal_copy")
        require(self.api.request("GET", "/api/skills/" + sid)["body"] == chosen_targeted,
            "targeted_primary_body_not_kept")
        require(self.api.request("GET", "/api/skills/" + copied["id"]) == copy_before,
            "targeted_primary_resolution_changed_other_copy")
        self.compare_attachment(sid, rid, "specific_primary_resolution_preserves_other_copy_and_complete_package")
        require(self.preview(sid)["action"] == "noop", "targeted_primary_resolution_not_converged")
        self.step("multiple_personal_copies_primary_target_is_exact_and_remote_identity_stable",
            unchanged_copy_content_sha256=copy_before["content_hash"])
        self.state["acceptance_steps_passed"] = True
        self.persist()

    def cleanup(self):
        """Never deletes by name, by prefix alone, or by table-wide iteration."""
        self.ensure_target()
        records = self.connector.list_records()
        # Recover uncertain create responses using this run's exact random skill IDs
        # plus its marker. Blank rows and all initial records remain ineligible.
        eligible = [r for r in records if self.own(r)]
        for record in eligible:
            rid = record["record_id"]
            if rid not in self.state["remote_record_ids"]:
                self.state["remote_record_ids"].append(rid)
                self.persist()
            self.owned_record(rid)
            self.connector.delete_record(rid)
        after_delete = {r["record_id"]: r for r in self.records()}
        for rid in self.state["remote_record_ids"]:
            require(rid not in after_delete, "owned_record_cleanup_incomplete")
        # A write-test record ID is accepted only if returned by this run's test API,
        # was absent in the baseline, and still has the test's exact marker/name.
        for rid in self.state["write_test_record_ids"]:
            record = after_delete.get(rid)
            if record is None:
                continue
            fields = record.get("fields", {})
            require(rid not in self.state["baseline_record_hashes"]
                and text(fields.get(self.connector.mapping["skill_id"])).startswith("__skill_collect_write_test__")
                and text(fields.get(self.connector.mapping["name"])) == "【可删除】skill collect 写权限测试", "write_test_cleanup_ownership_not_proven")
            self.connector.delete_record(rid)
        for sid in self.state["local_skill_ids"]:
            skill = self.api.request("GET", "/api/skills/" + sid)
            require(self.state["marker"] in skill.get("body", ""), "local_cleanup_ownership_not_proven")
            if not skill.get("archived"):
                self.api.request("DELETE", "/api/skills/" + sid)
        final = {r["record_id"]: r for r in self.records()}
        require(not any(self.own(r) for r in final.values()), "owned_record_still_present")
        require(not any(rid in final for rid in self.state["write_test_record_ids"]), "write_test_record_still_present")
        for rid, expected_hash in self.state["baseline_record_hashes"].items():
            require(rid in final and sha(canonical(final[rid].get("fields", {}))) == expected_hash,
                "preexisting_record_changed_during_acceptance")
        self.state["cleanup_passed"] = True
        self.persist()
        self.step("owned_data_cleaned_and_all_preexisting_rows_unchanged")

    def public_result(self):
        return {"status": self.state.get("status", "failed"), "test_type": "real_feishu_dedicated_table_self_authored_fixture",
            "started_at": self.state["created_at"], "finished_at": timestamp(),
            "target_sha256": self.state["target_sha256"], "model_calls": 0,
            "acceptance_steps_passed": bool(self.state.get("acceptance_steps_passed")),
            "cleanup_passed": bool(self.state.get("cleanup_passed")), "steps": self.state["steps"],
            **({"error_code": self.state["error_code"]} if self.state.get("error_code") else {})}

    def close(self):
        self.connector.close()
        self.api.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="read localhost configuration only; no Feishu request")
    mode.add_argument("--run", action="store_true", help="execute the real write/attachment/conflict acceptance and owned-data cleanup")
    mode.add_argument("--cleanup-state", metavar="PATH", help="retry only owned-data cleanup using a private state.json")
    parser.add_argument("--expected-target-sha256", help="required for every real run/cleanup; get it via --check-config")
    args = parser.parse_args(argv)
    if args.check_config:
        try:
            print(json.dumps(check_configuration(args.base), ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "error_code": safe_code(exc), "external_requests": False}))
            return 1
    if not args.run and not args.cleanup_state:
        print(json.dumps({"status": "plan_only_not_executed", "external_requests": False, "steps": PLAN}, ensure_ascii=False, indent=2))
        return 0
    if (not args.expected_target_sha256 or len(args.expected_target_sha256) != 64
        or any(c not in "0123456789abcdef" for c in args.expected_target_sha256)):
        parser.error("real runs require --expected-target-sha256 from --check-config")
    verifier = None
    try:
        verifier = LiveVerifier(args.base, args.expected_target_sha256, args.cleanup_state)
        try:
            if args.run:
                verifier.run()
        except Exception as exc:
            verifier.state["error_code"] = safe_code(exc)
            verifier.persist()
            if verifier.state.get("primary_skill_id") and not verifier.state.get("failure_diagnostics"):
                verifier.capture_failure(verifier.state["primary_skill_id"],
                    {"error": {"code": safe_code(exc)}, "context": "acceptance_assertion_or_request"})
        try:
            verifier.cleanup()
        except Exception as exc:
            verifier.state["cleanup_error_code"] = safe_code(exc)
            verifier.state.setdefault("error_code", "cleanup_incomplete")
            verifier.persist()
        verifier.state["status"] = "passed" if (verifier.state.get("acceptance_steps_passed")
            and verifier.state.get("cleanup_passed") and not verifier.state.get("error_code")) else "failed"
        verifier.persist()
        result = verifier.public_result()
        # Cleanup retries keep the original evidence file specific to their run.
        private_result = verifier.state_path.parent / "result-public-safe.json"
        write_private(private_result, result)
        if args.run:
            PUBLIC_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            # Preserve prior failed attempts as evidence before updating latest.
            if PUBLIC_OUTPUT.is_file():
                previous = PUBLIC_OUTPUT.read_bytes()
                try:
                    previous_result = json.loads(previous)
                except ValueError:
                    previous_result = {}
                if previous_result.get("test_type") == "real_feishu_dedicated_table_self_authored_fixture":
                    PUBLIC_OUTPUT.with_name("feishu-live-" + sha(previous)[:16] + ".json").write_bytes(previous)
            output = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
            PUBLIC_OUTPUT.with_name("feishu-live-" + sha(output)[:16] + ".json").write_bytes(output)
            PUBLIC_OUTPUT.write_bytes(output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("Private cleanup state: " + str(verifier.state_path))
        return 0 if result["status"] == "passed" else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_code": safe_code(exc)}, ensure_ascii=False))
        return 1
    finally:
        if verifier:
            verifier.close()


if __name__ == "__main__":
    raise SystemExit(main())
