"""Manual, conservative two-way Base synchronization of complete skill packages.

No background sync and no deletion propagation. Remote contents are fingerprinted,
not trusted merely because an app-written timestamp/hash field says unchanged.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import stat
import threading
import uuid
import zipfile
from contextlib import nullcontext
from pathlib import PurePosixPath

from .connectors.feishu import FeishuConnector, FeishuError, MAX_PACKAGE

SYNC_LOCK = threading.RLock()
TRUNCATED_BODY = "[完整正文见技能包附件]\n"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(str(v.get("text", "")) if isinstance(v, dict) else str(v) for v in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "")
    return str(value)


def inspect_package(data: bytes) -> dict:
    """Validate package before using metadata or importing. Never extracts files."""
    if len(data) > MAX_PACKAGE:
        raise FeishuError("package_size", "远端技能包超过 20 MiB。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 256 or sum(x.file_size for x in infos) > MAX_PACKAGE:
                raise FeishuError("package_size", "远端技能包文件数量或解压体积超过限制。")
            paths = []
            for info in infos:
                path = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (path.is_absolute() or ".." in path.parts or "\\" in info.filename
                    or ":" in info.filename or "\x00" in info.filename
                    or str(path) != info.filename.rstrip("/") or stat.S_ISLNK(mode)):
                    raise FeishuError("package_unsafe", "远端技能包含不安全路径或符号链接。")
                if not info.is_dir():
                    paths.append(info.filename)
            manifests = [p for p in paths if PurePosixPath(p).name == "manifest.json"]
            if len(manifests) != 1:
                raise FeishuError("package_manifest", "远端包必须包含唯一 manifest.json。")
            prefix = str(PurePosixPath(manifests[0]).parent)
            prefix = "" if prefix == "." else prefix + "/"
            manifest = json.loads(archive.read(manifests[0]))
            if manifest.get("format_version") != 1 or not isinstance(manifest.get("files"), list):
                raise FeishuError("package_manifest", "远端技能包 manifest 格式不兼容。")
            declared = {f["path"]: f for f in manifest["files"]}
            actual = {p[len(prefix):] for p in paths if p != manifests[0] and p.startswith(prefix)}
            if len(declared) != len(manifest["files"]) or set(declared) != actual or len(paths) != len(actual) + 1:
                raise FeishuError("package_manifest", "远端技能包文件清单与实际文件不一致。")
            files = []
            body = None
            for name in sorted(actual):
                content = archive.read(prefix + name)
                sha = hashlib.sha256(content).hexdigest()
                if sha != declared[name].get("sha256") or len(content) != declared[name].get("size"):
                    raise FeishuError("package_hash", "远端技能包文件哈希或大小不一致。")
                files.append({"path": name, "sha256": sha, "size": len(content),
                    "encoding": "base64", "content": base64.b64encode(content).decode()})
                if name == "SKILL.md":
                    body = content.decode("utf-8")
            if body is None:
                raise FeishuError("package_manifest", "远端包缺少 SKILL.md。")
            return {"manifest": manifest, "files": files, "body": body,
                "file_hashes": {f["path"]: f["sha256"] for f in files}}
    except FeishuError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError, TypeError, RuntimeError):
        raise FeishuError("package_invalid", "远端技能包损坏或清单无效。") from None


class SyncService:
    def __init__(self, store, vault, skills, connector_factory=None):
        self.store, self.vault, self.skills = store, vault, skills
        self.connector_factory = connector_factory or FeishuConnector

    def connector(self):
        config = self.store.get("connections", "feishu") or {}
        # Settings may wrap connector config; accepting both keeps migration simple.
        config = config.get("config", config)
        return self.connector_factory(config, self.vault.get("feishu_secret") or "")

    def logs(self):
        return sorted(self.store.list("sync_logs"), key=lambda x: x.get("created_at", ""), reverse=True)

    def _log(self, action, status, **kwargs):
        return self.store.put("sync_logs", {"id": str(uuid.uuid4()), "connection_id": "feishu",
            "action": action, "status": status, **kwargs})

    def _get_skill(self, skill_id):
        if not skill_id:
            return None
        try:
            return self.skills.get(skill_id)
        except Exception as exc:
            if getattr(exc, "code", None) == "not_found":
                return None
            raise

    @staticmethod
    def _local_hash(local):
        return digest({k: local.get(k) for k in (
            "content_hash", "name", "description", "category", "tags", "status")})

    def _link(self, skill_id):
        return self.store.get("sync_links", "feishu:" + skill_id)

    def _remote(self, connector, record):
        fields = record.get("fields", {})
        canonical = {k: fields.get(v) for k, v in connector.mapping.items() if v and k != "package"}
        values = {k: text(v) for k, v in canonical.items()}
        attachments = fields.get(connector.mapping["package"], [])
        if not isinstance(attachments, list) or len(attachments) != 1 or not isinstance(attachments[0], dict) or not attachments[0].get("file_token"):
            raise FeishuError("attachment_missing", "目标记录需有且仅有一个完整技能 ZIP 附件；先修复附件再同步。")
        data = connector.download_package(attachments[0]["file_token"], record["record_id"])
        package = inspect_package(data)
        remote_hash = digest({"fields": canonical, "files": package["file_hashes"]})
        visible_body = values.get("body", "")
        body = package["body"] if visible_body.startswith(TRUNCATED_BODY) else visible_body
        if not body.strip():
            raise FeishuError("remote_body", "企业技能正文为空；请修复正文或使用完整正文附件标记。")
        return {"record": record, "data": data, "package": package, "values": values,
            "hash": remote_hash, "body": body}

    def records(self):
        connector = self.connector()
        try:
            result = []
            for record in connector.list_records():
                values = {k: text(record.get("fields", {}).get(v)) for k, v in connector.mapping.items() if k != "package"}
                result.append({**record, "name": values.get("name"), "skill_id": values.get("skill_id"),
                    "body": values.get("body"), "validation": "not_downloaded"})
            return result
        finally:
            connector.close()

    def _row(self, connector, local, record):
        link = self._link(local["id"]) if local else None
        row = {"skill_id": local["id"] if local else None,
            "record_id": record.get("record_id") if record else None,
            "name": local.get("name", "") if local else "", "local_hash": self._local_hash(local) if local else None,
            "local_body": local.get("body", "") if local else "", "remote_hash": None, "remote_body": "", "diff": ""}
        if record is None:
            row["action"] = "remote_missing" if link else "create"
            return row
        try:
            remote = self._remote(connector, record)
        except FeishuError as exc:
            row.update(action="blocked", error=exc.message, error_code=exc.code)
            return row
        row.update(remote_hash=remote["hash"], remote_body=remote["body"])
        row["name"] = row["name"] or remote["values"].get("name") or remote["package"]["manifest"].get("name")
        row["diff"] = "\n".join(difflib.unified_diff(row["local_body"].splitlines(), row["remote_body"].splitlines(),
            fromfile="个人技能", tofile="飞书正文", lineterm=""))[:20000]
        if local is None:
            row["action"] = "remote_only"
        elif link:
            lc = self._local_hash(local) != link.get("local_hash")
            rc = remote["hash"] != link.get("remote_hash")
            row["action"] = "conflict" if lc and rc else "push" if lc else "pull" if rc else "noop"
        else:
            file_hashes = {f["path"]: f["sha256"] for f in local.get("files", [])}
            identical = (local.get("body") == remote["body"]
                and file_hashes == remote["package"]["file_hashes"]
                and local.get("name", "") == remote["values"].get("name", ""))
            row["action"] = "push" if identical else "conflict"
        return row

    def preview(self, skill_ids=None):
        with SYNC_LOCK:
            connector = self.connector()
            try:
                records = connector.list_records()
                by_record = {r["record_id"]: r for r in records}
                by_skill: dict[str, list[dict]] = {}
                for record in records:
                    sid = text(record.get("fields", {}).get(connector.mapping["skill_id"]))
                    if sid:
                        by_skill.setdefault(sid, []).append(record)
                local_items = [self._get_skill(sid) for sid in skill_ids] if skill_ids else self.store.list("skills")
                local_items = [s for s in local_items if s and not s.get("archived") and s.get("status") != "archived"]
                rows, used = [], set()
                for local in local_items:
                    link = self._link(local["id"])
                    matches = by_skill.get(local["id"], [])
                    if len(matches) > 1:
                        rows.append({"skill_id": local["id"], "name": local.get("name"), "action": "blocked",
                            "error": "企业表出现重复 Skill ID，请先人工处理，程序不会猜测覆盖哪条。"})
                        used.update(r["record_id"] for r in matches)
                        continue
                    record = by_record.get(link["record_id"]) if link else (matches[0] if matches else None)
                    if record:
                        used.add(record["record_id"])
                    rows.append(self._row(connector, local, record))
                if not skill_ids:
                    for record in records:
                        sid = text(record.get("fields", {}).get(connector.mapping["skill_id"]))
                        if record["record_id"] not in used and sid and not sid.startswith("__skill_collect_write_test__"):
                            rows.append(self._row(connector, None, record))
                return {"items": rows, "connection_id": "feishu", "read_only": True}
            finally:
                connector.close()

    def _save_baseline(self, local, remote, record_id):
        return self.store.put("sync_links", {"id": "feishu:" + local["id"], "connection_id": "feishu",
            "skill_id": local["id"], "record_id": record_id,
            "local_hash": self._local_hash(local), "remote_hash": remote["hash"]})

    def _fields_for(self, connector, local, token):
        origin = local.get("source") or local.get("origin") or {}
        source = origin.get("url", "") if isinstance(origin, dict) else str(origin)
        body = local.get("body", "")
        values = {"skill_id": local["id"], "name": local.get("name", ""), "description": local.get("description", ""),
            "category": local.get("category", ""), "tags": ", ".join(local.get("tags", [])),
            "status": local.get("status", "draft"), "source": source, "version": str(local.get("version", 1)),
            "content_hash": local.get("content_hash", ""), "updated_at": local.get("updated_at", ""),
            "body": body if len(body) <= 19000 else TRUNCATED_BODY + body[:1000],
            "package": [{"file_token": token}]}
        actual = {f["field_name"]: f for f in connector.fields()}
        for key, required_type in ((k, 17 if k == "package" else 1) for k in connector.mapping):
            name = connector.mapping[key]
            if name and name in actual and actual[name].get("type") != required_type:
                raise FeishuError("field_mapping", f"飞书字段 {name} 的类型不兼容；正文等使用文本，技能包使用附件。")
            if key in ("skill_id", "name", "body", "package") and (not name or name not in actual):
                raise FeishuError("field_mapping", f"缺少必需的飞书字段映射：{key}。")
        return {connector.mapping[k]: value for k, value in values.items()
            if connector.mapping.get(k) and connector.mapping[k] in actual}

    def push(self, skill_id, resolution=None):
        with SYNC_LOCK:
            local = self._get_skill(skill_id)
            if not local:
                raise FeishuError("not_found", "个人技能不存在。")
            if local.get("archived"):
                raise FeishuError("archived", "已归档的个人技能不会同步，请先恢复。")
            preview = self.preview([skill_id])["items"][0]
            if preview["action"] == "blocked":
                raise FeishuError("blocked", preview["error"])
            if preview["action"] in ("conflict", "pull", "remote_missing") and resolution is None:
                raise FeishuError("conflict", "企业内容已变化或远端被移除，请预览差异后选择保留版本或另建副本。")
            if resolution == "remote":
                if not preview.get("record_id"):
                    raise FeishuError("not_found", "企业记录已不存在，不会自动传播删除。")
                return self.pull(preview["record_id"], resolution="remote")
            if resolution == "copy" and preview.get("record_id"):
                return self.pull(preview["record_id"], resolution="copy")
            if resolution not in (None, "local", "copy", "remote"):
                raise FeishuError("config", "未知同步冲突处理方式。")
            if preview["action"] == "noop":
                log = self._log("push", "noop", skill_id=skill_id, record_id=preview["record_id"])
                return {"action": "noop", "record_id": preview["record_id"], "log_id": log["id"]}
            connector = self.connector()
            operation_id = "feishu:" + skill_id
            try:
                # Freeze body, metadata and files from the same local version. Network I/O
                # happens outside the store lock so normal editing remains responsive.
                with getattr(self.store, "lock", nullcontext()):
                    current_local = self._get_skill(skill_id)
                    if not current_local or self._local_hash(current_local) != self._local_hash(local):
                        raise FeishuError("conflict", "个人技能在同步预览后发生变化，请重新预览。")
                    package = self.skills.export_zip(skill_id)
                inspect_package(package)
                token = connector.upload_package(package, local.get("name", "skill") + ".zip")
                fields = self._fields_for(connector, local, token)
                record_id = preview.get("record_id")
                # Durable intent precedes network mutation; no token/credentials are persisted.
                pending = self.store.get("sync_pending", operation_id)
                self.store.put("sync_pending", {"id": operation_id, "skill_id": skill_id,
                    "operation": "update" if record_id else "create", "record_id": record_id,
                    "expected_hash": local["content_hash"], "uncertain": bool(pending and pending.get("uncertain"))})
                if record_id:
                    latest = next((r for r in connector.list_records() if r["record_id"] == record_id), None)
                    if latest is None or self._remote(connector, latest)["hash"] != preview.get("remote_hash"):
                        raise FeishuError("conflict", "企业记录在同步预览后发生变化，请重新预览；未覆盖企业内容。")
                    record = connector.update_record(record_id, fields)
                else:
                    # Reconcile after previous timeout/restart, then recheck immediately before create.
                    matches = [r for r in connector.list_records()
                        if text(r.get("fields", {}).get(connector.mapping["skill_id"])) == skill_id]
                    if len(matches) > 1:
                        raise FeishuError("duplicate", "企业表存在重复 Skill ID，已停止创建。")
                    if matches:
                        record = matches[0]
                        remote = self._remote(connector, record)
                        same = (remote["values"].get("content_hash") == local["content_hash"]
                            and remote["body"] == local.get("body", "")
                            and remote["package"]["file_hashes"] == inspect_package(package)["file_hashes"])
                        if not same:
                            raise FeishuError("conflict", "发现已有同 ID 企业记录且内容不同，请重新预览后处理。")
                    elif pending and pending.get("uncertain"):
                        raise FeishuError("uncertain_write", "上次创建结果仍未知且暂未查到记录；请稍后再次预览，避免生成重复记录。")
                    else:
                        record = connector.create_record(fields)
                remote = self._remote(connector, record)
                self._save_baseline(local, remote, record["record_id"])
                self.store.delete("sync_pending", operation_id)
                log = self._log("push", "passed", skill_id=skill_id, record_id=record["record_id"])
                return {"action": "updated" if record_id else "created", "record_id": record["record_id"], "log_id": log["id"]}
            except FeishuError as exc:
                if exc.code in ("timeout", "network"):
                    intent = self.store.get("sync_pending", operation_id)
                    if intent:
                        self.store.put("sync_pending", {**intent, "uncertain": True})
                self._log("push", "failed", skill_id=skill_id, code=exc.code, error=exc.message)
                raise
            finally:
                connector.close()

    def pull(self, record_id, resolution=None):
        with SYNC_LOCK:
            if resolution not in (None, "remote", "local", "copy"):
                raise FeishuError("config", "未知同步冲突处理方式。")
            connector = self.connector()
            try:
                records = connector.list_records()
                record = next((r for r in records if r["record_id"] == record_id), None)
                if not record:
                    raise FeishuError("not_found", "飞书记录不存在，不会自动删除个人内容。")
                remote = self._remote(connector, record)
                links = [x for x in self.store.list("sync_links") if x.get("record_id") == record_id]
                stable_id = remote["values"].get("skill_id")
                local = self._get_skill(links[0]["skill_id"]) if links else self._get_skill(stable_id)
                if local:
                    row = self._row(connector, local, record)
                    if row["action"] in ("conflict", "push") and resolution is None:
                        raise FeishuError("conflict", "个人内容已修改，请预览后选择保留版本或另建副本。")
                    if resolution == "local":
                        return self.push(local["id"], resolution="local")
                origin = {"platform": "feishu", "connection_id": "feishu", "record_id": record_id,
                    "url": connector.config.get("table_url") or connector.config.get("url") or (
                        "https://feishu.cn/base/" + connector.config["app_token"] + "?table=" + connector.config["table_id"]),
                    "origin_key": "feishu:" + connector.config["app_token"] + ":" + connector.config["table_id"] + ":" + record_id}
                if resolution == "copy":
                    origin["origin_key"] += ":copy:" + str(uuid.uuid4())
                if not local or resolution == "copy":
                    imported = self.skills.import_zip(remote["data"], origin=origin)
                    # Origin association is preserved; enterprise status cannot grant execution validation.
                    local = imported
                update = {"name": remote["values"].get("name") or remote["package"]["manifest"].get("name") or local["name"],
                    "description": remote["values"].get("description", ""), "body": remote["body"],
                    "files": remote["package"]["files"], "category": remote["values"].get("category", ""),
                    "tags": [x.strip() for x in remote["values"].get("tags", "").split(",") if x.strip()],
                    "origin": origin, "status": "draft"}
                local = self.skills.update(local["id"], update)
                self._save_baseline(local, remote, record_id)
                log = self._log("pull", "passed", skill_id=local["id"], record_id=record_id,
                    resolution=resolution or "remote")
                return {"action": "copied" if resolution == "copy" else "imported", "skill": local,
                    "record_id": record_id, "log_id": log["id"]}
            except FeishuError as exc:
                self._log("pull", "failed", record_id=record_id, code=exc.code, error=exc.message)
                raise
            finally:
                connector.close()
