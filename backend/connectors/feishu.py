"""Bounded Feishu OpenAPI client. No enterprise credentials are returned or logged."""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_MAPPING = {
    "skill_id": "Skill ID", "name": "名称", "description": "简介",
    "category": "分类", "tags": "标签", "status": "状态", "source": "来源链接",
    "version": "版本", "content_hash": "内容哈希", "updated_at": "更新时间",
    "body": "Skill 正文", "package": "技能包附件",
}
REQUIRED = {"skill_id": 1, "name": 1, "body": 1, "package": 17}
MAX_PACKAGE = 20 * 1024 * 1024


class FeishuError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


def normalize_config(config: dict) -> dict:
    """Only regular Base links are accepted; wiki node resolution is explicit."""
    result = dict(config)
    link = str(config.get("table_url") or config.get("url") or "").strip()
    if link:
        parsed = urlparse(link)
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname.endswith(".feishu.cn") or parsed.hostname.endswith(".larksuite.com")
        ) or parsed.username or parsed.password:
            raise FeishuError("config", "请填写飞书 HTTPS 多维表格链接。")
        match = re.fullmatch(r"/base/([A-Za-z0-9_-]+)/?", parsed.path)
        if not match:
            raise FeishuError("config", "当前支持 /base/ 多维表格链接；知识库链接请先在飞书打开原始多维表格。")
        result["app_token"] = match.group(1)
        result["table_id"] = parse_qs(parsed.query).get("table", [result.get("table_id", "")])[0]
    supplied = result.get("field_mapping", {})
    if not isinstance(supplied, dict):
        raise FeishuError("config", "字段映射必须是 JSON 对象。")
    if any(k not in DEFAULT_MAPPING or not isinstance(v, str) or len(v) > 200 for k, v in supplied.items()):
        raise FeishuError("config", "字段映射只接受已知字段和文本字段名。")
    if result.get("auth_mode", "legacy") not in ("legacy", "oauth3"):
        raise FeishuError("config", "飞书认证模式必须为 legacy 或 oauth3。")
    result["field_mapping"] = {**DEFAULT_MAPPING, **supplied}
    for key in ("app_id", "app_token", "table_id"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", str(result.get(key, ""))):
            raise FeishuError("waiting_config", f"请在设置中填写飞书 {key}。")
    names = [v for v in result["field_mapping"].values() if v]
    if len(names) != len(set(names)):
        raise FeishuError("config", "不同技能字段不能映射到同一个飞书字段。")
    return result


class FeishuConnector:
    def __init__(self, config: dict, secret: str, client: httpx.Client | None = None):
        self.config = normalize_config(config)
        if not secret:
            raise FeishuError("waiting_config", "请在本地设置中填写飞书 App Secret。")
        self.secret = secret
        self.mapping = self.config["field_mapping"]
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owns_client = client is None
        self._token = ""
        self._expires = 0.0
        self._fields: list[dict] | None = None
        self.base = "https://open.feishu.cn/open-apis"
        self.table_path = "/bitable/v1/apps/{}/tables/{}".format(
            self.config["app_token"], self.config["table_id"])

    def close(self):
        if self._owns_client:
            self.client.close()

    def _error(self, status: int, body: dict | None = None) -> FeishuError:
        body = body or {}
        number = body.get("code", status)
        if status == 429 or number in (99991400, 1254290):
            return FeishuError("rate_limit", f"飞书请求过于频繁（代码 {number}），请稍后重试。", True)
        if status >= 500:
            return FeishuError("service", f"飞书服务暂时不可用（HTTP {status}）。", True)
        if status in (401, 403) or number in (99991663, 99991664, 99991668, 99991672):
            return FeishuError("permission", f"飞书凭证或权限不足（代码 {number}），请检查应用与文档权限。")
        return FeishuError("feishu_api", f"飞书调用未成功（HTTP {status}，代码 {number}）；检查字段类型、应用权限和表格授权。")

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        try:
            if self.config.get("auth_mode", "legacy") == "legacy":
                response = self.client.post(self.base + "/auth/v3/tenant_access_token/internal", json={
                    "app_id": self.config["app_id"], "app_secret": self.secret})
            else:
                response = self.client.post("https://accounts.feishu.cn/oauth/v3/token", json={
                    "grant_type": "client_credentials", "client_id": self.config["app_id"],
                    "client_secret": self.secret})
            body = response.json()
        except httpx.TimeoutException:
            raise FeishuError("timeout", "飞书认证请求超时。", True) from None
        except httpx.RequestError:
            raise FeishuError("network", "无法连接飞书认证服务。", True) from None
        except ValueError:
            raise FeishuError("protocol", "飞书认证返回了非 JSON 内容。") from None
        if not isinstance(body, dict):
            raise FeishuError("protocol", "飞书认证返回的 JSON 结构不兼容。")
        token = body.get("access_token") or body.get("tenant_access_token")
        if response.is_error or body.get("code", 0) != 0 or not token:
            raise self._error(response.status_code, body)
        self._token = token
        self._expires = time.monotonic() + max(0, int(body.get("expires_in", body.get("expire", 7200))) - 60)
        return token

    def _request(self, method: str, path: str, *, raw=False, **kwargs) -> Any:
        # Writes are never blindly repeated: SyncService reconciles uncertain writes.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                # Stop oversized bodies while streaming rather than after loading them all.
                with self.client.stream(method, self.base + path,
                    headers={"Authorization": "Bearer " + self._access_token()}, **kwargs) as response:
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_PACKAGE + 1024 * 1024:
                            raise FeishuError("size", "飞书响应超过本地安全上限。")
                    content = bytes(chunks)
                    if raw and not response.is_error:
                        if response.headers.get("content-type", "").startswith("application/json"):
                            try:
                                candidate = json.loads(content)
                            except ValueError:
                                raise FeishuError("protocol", "飞书附件返回了无效 JSON 错误内容。") from None
                            if not isinstance(candidate, dict):
                                raise FeishuError("protocol", "飞书附件响应结构不兼容。")
                            if candidate.get("code", 0):
                                raise self._error(response.status_code, candidate)
                        return content
                    try:
                        body = json.loads(content)
                    except ValueError:
                        raise FeishuError("protocol", "飞书返回了非 JSON 内容；请检查网络。") from None
                    if not isinstance(body, dict):
                        raise FeishuError("protocol", "飞书返回的 JSON 结构不兼容。")
                    if response.is_error or body.get("code", 0) != 0:
                        raise self._error(response.status_code, body)
                    return body.get("data", body)
            except httpx.TimeoutException:
                error = FeishuError("timeout", "飞书请求超时；写入结果将在下次同步前重新核对。", True)
            except httpx.RequestError:
                error = FeishuError("network", "连接飞书时出现网络错误。", True)
            except FeishuError as exc:
                error = exc
            if not error.retryable or attempt == attempts - 1:
                raise error
            time.sleep(0.2 * 2**attempt)

    def _pages(self, path: str) -> list[dict]:
        rows: list[dict] = []
        token = None
        seen: set[str] = set()
        for _ in range(100):
            params = {"page_size": 100}
            if token:
                params["page_token"] = token
            data = self._request("GET", path, params=params)
            if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
                raise FeishuError("protocol", "飞书分页响应结构不兼容。")
            rows.extend(data.get("items") or [])
            if not data.get("has_more"):
                return rows
            token = data.get("page_token")
            if not token or token in seen:
                raise FeishuError("pagination", "飞书分页标记异常，已停止读取以避免循环。")
            seen.add(token)
        raise FeishuError("limit", "目标表超过首版单次读取的 10000 行上限，请缩小目标表。")

    def fields(self) -> list[dict]:
        if self._fields is None:
            self._fields = self._pages(self.table_path + "/fields")
        return self._fields

    def list_records(self) -> list[dict]:
        return self._pages(self.table_path + "/records")

    def create_record(self, fields: dict) -> dict:
        return self._request("POST", self.table_path + "/records", json={"fields": fields})["record"]

    def update_record(self, record_id: str, fields: dict) -> dict:
        self._id(record_id)
        return self._request("PUT", self.table_path + f"/records/{record_id}", json={"fields": fields})["record"]

    def delete_record(self, record_id: str) -> dict:
        self._id(record_id)
        return self._request("DELETE", self.table_path + f"/records/{record_id}")

    @staticmethod
    def _id(value: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
            raise FeishuError("config", "飞书记录或附件标识无效。")

    def upload_package(self, data: bytes, name: str) -> str:
        if len(data) > MAX_PACKAGE:
            raise FeishuError("size", "技能包超过飞书单次上传 20 MiB 上限。")
        result = self._request("POST", "/drive/v1/medias/upload_all", data={
            "file_name": name[:160], "parent_type": "bitable_file",
            "parent_node": self.config["app_token"], "size": str(len(data))},
            files={"file": (name[:160], data, "application/zip")})
        token = result.get("file_token")
        if not token:
            raise FeishuError("protocol", "飞书未返回附件 file_token。")
        return token

    def download_package(self, token: str, record_id: str | None = None) -> bytes:
        self._id(token)
        params = {}
        if record_id:
            fields = {f["field_name"]: f for f in self.fields()}
            field = fields.get(self.mapping["package"])
            if field and field.get("field_id"):
                params["extra"] = json.dumps({"bitablePerm": {
                    "tableId": self.config["table_id"], "attachments": {
                        field["field_id"]: {record_id: [token]}}}}, separators=(",", ":"))
        return self._request("GET", f"/drive/v1/medias/{token}/download", raw=True, params=params)

    def test_connection(self, write_test: bool = False) -> dict:
        self._access_token()
        actual = {f["field_name"]: f for f in self.fields()}
        checks = []
        for key, expected_type in REQUIRED.items():
            name = self.mapping.get(key)
            found = actual.get(name)
            checks.append({"key": key, "field_name": name, "expected_type": expected_type,
                "actual_type": found.get("type") if found else None,
                "ok": bool(found and found.get("type") == expected_type)})
        # Optional mapped fields also must be text; clients may omit them by mapping to "".
        for key, name in self.mapping.items():
            if key not in REQUIRED and name and name in actual:
                checks.append({"key": key, "field_name": name, "expected_type": 1,
                    "actual_type": actual[name].get("type"), "ok": actual[name].get("type") == 1})
        rows = self.list_records()
        result = {"authentication": "passed", "read": "passed", "record_count": len(rows),
            "field_checks": checks, "compatible": all(c["ok"] for c in checks),
            "write": "not_tested", "write_test_record": None}
        if write_test:
            if not result["compatible"]:
                raise FeishuError("field_mapping", "字段映射不兼容，请先修正必需字段类型。")
            marker = "__skill_collect_write_test__" + str(uuid.uuid4())
            record = self.create_record({self.mapping["skill_id"]: marker,
                self.mapping["name"]: "【可删除】skill collect 写权限测试"})
            result["write_test_record"] = record["record_id"]
            try:
                self.delete_record(record["record_id"])
                result["write"] = "passed_and_test_record_deleted"
            except FeishuError as exc:
                result["write"] = "created_cleanup_failed"
                result["cleanup_error"] = exc.message
        return result
