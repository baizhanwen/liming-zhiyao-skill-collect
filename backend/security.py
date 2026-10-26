import ipaddress
import json
import os
import re
import socket
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet


class AppError(Exception):
    def __init__(self, code, message, retryable=False, status=400):
        super().__init__(message)
        self.code, self.message, self.retryable, self.status = code, message, retryable, status


class Vault:
    """Separate local encryption key; secrets are omitted from backups and exports."""
    def __init__(self, root):
        self.root = Path(root)
        self.key_path = self.root / '.secrets.key'
        self.path = self.root / 'secrets.enc'
        self.lock = threading.RLock()
        if not self.key_path.exists():
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        if os.name != 'nt':
            self.key_path.chmod(0o600)
        self.cipher = Fernet(self.key_path.read_bytes())

    def _load(self):
        return json.loads(self.cipher.decrypt(self.path.read_bytes())) if self.path.exists() else {}

    def get(self, key):
        with self.lock:
            return self._load().get(key, '')

    def set(self, key, value):
        with self.lock:
            secrets = self._load()
            if value:
                secrets[key] = value
            else:
                secrets.pop(key, None)
            temp = self.path.with_suffix('.tmp')
            temp.write_bytes(self.cipher.encrypt(json.dumps(secrets).encode()))
            if os.name != 'nt':
                temp.chmod(0o600)
            temp.replace(self.path)

    def redact(self, message, limit=1500):
        message = str(message)
        with self.lock:
            for secret in self._load().values():
                if secret:
                    message = message.replace(secret, '[REDACTED]')
        value=re.sub(r'(?i)(Bearer\s+)[\w.\-]+', r'\1[REDACTED]', message)
        return value if limit is None else value[:limit]


def public_url(url):
    p = urlsplit(url)
    try:
        port=p.port
    except ValueError:
        raise AppError('unsafe_url','链接端口不合法')
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or port not in (None,80,443):
        raise AppError('unsafe_url', '只允许无凭证的公共 HTTP(S) 链接')
    try:
        addresses = socket.getaddrinfo(p.hostname, port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise AppError('dns', '无法解析来源地址', True)
    ips = list(dict.fromkeys(a[4][0] for a in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise AppError('unsafe_url','来源链接不能访问本机、内网或保留地址')
    return ips


class PublicTransport(httpx.BaseTransport):
    """Resolve, validate and pin each connection; redirect targets go through the same check."""
    def __init__(self):
        self.transport = httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        ips = public_url(str(request.url))
        hostname = request.url.host
        headers=request.headers.copy();headers['host']=request.url.netloc.decode()
        extensions={**request.extensions,'sni_hostname':hostname}
        pinned=httpx.Request(request.method,request.url.copy_with(host=ips[0]),headers=headers,stream=request.stream,extensions=extensions)
        return self.transport.handle_request(pinned)

    def close(self):
        self.transport.close()


def fetch_public(url, max_bytes=2_000_000):
    with httpx.Client(transport=PublicTransport(), timeout=25, follow_redirects=True, max_redirects=5, trust_env=False) as client:
        with client.stream('GET', url, headers={'User-Agent':'SkillCollect/0.1 (local research workbench)'}) as r:
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise AppError('too_large','来源下载超过大小限制')
                chunks.append(chunk)
            return b''.join(chunks), str(r.url), dict(r.headers)
