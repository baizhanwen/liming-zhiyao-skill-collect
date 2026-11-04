"""Build the public Windows one-click distribution from a fixed source allowlist.

This reads no user data or credentials and never installs/downloads a runtime.
The small launcher pins a manifest covering the application payload. The final
ZIP SHA-256 also covers that launcher and is the release download checksum.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import tomllib
import zipfile


PROJECT = Path(__file__).resolve().parents[1]
PRODUCT = 'liming-zhiyao-skill-collect'
VERSION = '0.2.4'
LAUNCHER = '一键配置并启动.cmd'
DATA_LAUNCHER = '沿用旧资料并启动.cmd'
LAUNCHERS = (LAUNCHER, DATA_LAUNCHER)
MANIFEST = 'bundle-manifest.json'
FILES = (
    'AGENTS.md', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'docs/用户使用说明.md',
    '.env.example', 'pyproject.toml', 'uv.lock', 'requirements.txt',
    'backend/__init__.py', 'backend/main.py', 'backend/browser_capture.py',
    'backend/media.py', 'backend/models.py', 'backend/project_links.py',
    'backend/security.py', 'backend/skills.py', 'backend/store.py',
    'backend/sync.py', 'backend/task_progress.py', 'backend/tasks.py',
    'backend/connectors/__init__.py', 'backend/connectors/github.py',
    'backend/connectors/feishu.py',
    'frontend/package.json', 'frontend/package-lock.json',
    'frontend/index.html', 'frontend/tsconfig.json', 'frontend/vite.config.ts',
    'frontend/scripts/capture-douyin.mjs',
    'frontend/src/App.tsx', 'frontend/src/ProjectLinks.css',
    'frontend/src/ProjectLinks.tsx', 'frontend/src/TaskProgress.css',
    'frontend/src/TaskProgress.tsx', 'frontend/src/api.ts',
    'frontend/src/main.tsx', 'frontend/src/styles.css', 'frontend/src/vite-env.d.ts',
    'scripts/backup.py', 'scripts/diagnose_douyin.py', 'scripts/doctor.py',
    'scripts/install_browser.py', 'scripts/setup.ps1', 'scripts/start.ps1',
    'scripts/setup.sh', 'scripts/start.sh', 'scripts/windows_bootstrap.ps1',
    'scripts/windows-runtime-lock.json',
    'docs/Windows一键安装.md', 'docs/安装与数据.md',
    'docs/验收报告.md', 'docs/实施进度.md', 'docs/技术决策.md',
    'docs/客户升级与抖音验收.md',
    'docs/抖音原片获取与分析.md', 'docs/MiniMax接入.md',
    'docs/GitHub接入.md', 'docs/飞书接入.md',
)
ASSET = re.compile(r'frontend/dist/assets/[A-Za-z0-9_-]+\.(?:js|css)\Z')
MAX_FILE = 12 * 1024 * 1024
MAX_TOTAL = 48 * 1024 * 1024
MAX_FILES = 160
PRIVATE_PARTS = frozenset({
    'data', 'uploads', 'tasks', 'backups', '.venv', 'node_modules', '.git',
    '.runtime', 'private', '__pycache__', 'artifacts',
})
SECRET_PATTERNS = (
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b'),
    re.compile(rb'\bAKIA[A-Z0-9]{16}\b'),
)


class BundleBuildError(ValueError):
    pass


def allowed_path(value: str) -> bool:
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        return False
    path = PurePosixPath(value)
    parts = value.split('/')
    if path.is_absolute() or str(path) != value or any(p in ('', '.', '..') for p in parts):
        return False
    if any(p.casefold() in PRIVATE_PARTS for p in parts):
        return False
    if any(re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p) for p in parts):
        return False
    if any(p.endswith((' ', '.')) for p in parts):
        return False
    return value in FILES or value in LAUNCHERS or value == 'frontend/dist/index.html' or bool(ASSET.fullmatch(value))


def no_links(path: Path) -> None:
    for candidate in (path.absolute(), *path.absolute().parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BundleBuildError('Links/reparse points are not valid bundle inputs')


def read_file(root: Path, relative: str) -> bytes:
    if not allowed_path(relative):
        raise BundleBuildError('File is outside the public bundle allowlist')
    file = root.joinpath(*PurePosixPath(relative).parts)
    no_links(file)
    try:
        info = file.stat()
    except OSError:
        raise BundleBuildError('A required public bundle file is unavailable') from None
    if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= MAX_FILE:
        raise BundleBuildError('Bundle input file type or size is invalid')
    body = file.read_bytes()
    if len(body) != info.st_size:
        raise BundleBuildError('Bundle input changed during reading')
    if any(pattern.search(body) for pattern in SECRET_PATTERNS):
        raise BundleBuildError('A potential credential was found in a public bundle input')
    return body


def validate_versions(data: dict[str, bytes]) -> None:
    try:
        project = tomllib.loads(data['pyproject.toml'].decode())['project']
        package = json.loads(data['frontend/package.json'])
        lock = json.loads(data['frontend/package-lock.json'])
        uv_project = [item for item in tomllib.loads(data['uv.lock'].decode())['package']
                      if item['name'] == PRODUCT]
        backend_version = re.fullmatch(rb"\s*__version__\s*=\s*['\"](0\.2\.4)['\"]\s*", data['backend/__init__.py'])
        if (project['name'] != PRODUCT or project['version'] != VERSION or not backend_version or
                package['name'] != PRODUCT + '-workbench' or package['version'] != VERSION or
                lock['version'] != VERSION or lock['packages']['']['version'] != VERSION or
                len(uv_project) != 1 or uv_project[0]['version'] != VERSION):
            raise ValueError('version')
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise BundleBuildError('Bundle version metadata must consistently describe ' + VERSION) from None


def build_bundle(source: Path, output: Path) -> dict:
    source, output = Path(source).absolute(), Path(output).absolute()
    no_links(source)
    no_links(output)
    checksum = output.with_name(output.name + '.sha256')
    no_links(checksum)
    if output.exists():
        raise BundleBuildError('Output already exists; choose a new archive name')
    if checksum.exists():
        raise BundleBuildError('Checksum output already exists; choose a new archive name')
    data: dict[str, bytes] = {}

    def add(name: str) -> None:
        if name in data or len(data) >= MAX_FILES:
            raise BundleBuildError('Duplicate or too many public bundle files')
        body = read_file(source, name)
        if sum(map(len, data.values())) + len(body) > MAX_TOTAL:
            raise BundleBuildError('Bundle exceeds its fixed package budget')
        data[name] = body

    for name in FILES:
        add(name)
    validate_versions(data)
    dist = source / 'frontend/dist'
    no_links(dist)
    if not dist.is_dir():
        raise BundleBuildError('Build frontend/dist before packaging')
    for current, dirs, names in os.walk(dist, followlinks=False):
        for name in sorted([*dirs, *names]):
            file = Path(current) / name
            no_links(file)
            relative = file.relative_to(source).as_posix()
            if file.is_dir():
                if relative != 'frontend/dist/assets':
                    raise BundleBuildError('Unexpected directory in compiled frontend')
            else:
                add(relative)
    if 'frontend/dist/index.html' not in data or not any(ASSET.fullmatch(n) and n.endswith('.js') for n in data):
        raise BundleBuildError('Compiled index.html and JavaScript assets are required')
    if len({name.casefold() for name in data}) != len(data):
        raise BundleBuildError('Case-insensitive duplicate paths are not allowed')
    try:
        index = data['frontend/dist/index.html'].decode('utf-8')
    except UnicodeError:
        raise BundleBuildError('Compiled index.html must be UTF-8') from None
    referenced = set()
    for link in re.findall(r'(?:src|href)=["\']([^"\']+)["\']', index):
        if link.startswith('/assets/'):
            asset = 'frontend/dist' + link
            if asset not in data:
                raise BundleBuildError('Compiled index references a missing asset')
            referenced.add(asset)
        else:
            raise BundleBuildError('Compiled frontend must load only bundled /assets/ files')
    if referenced != {name for name in data if ASSET.fullmatch(name)}:
        raise BundleBuildError('Unexpected or unreferenced compiled asset')
    manifest = {
        'schema_version': 1, 'product': PRODUCT, 'version': VERSION,
        'runtimes_bundled': False,
        'files': [{'path': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                  for name, body in sorted(data.items())],
    }
    raw_manifest = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    launchers = {}
    for name in LAUNCHERS:
        launcher = read_file(source, name).decode('utf-8-sig')
        if launcher.count('__BUNDLE_MANIFEST_SHA256__') != 1:
            raise BundleBuildError('Launcher manifest pin placeholder is invalid')
        launchers[name] = launcher.replace('__BUNDLE_MANIFEST_SHA256__', manifest_hash).replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8')
    prefix = 'SkillCollect-v' + VERSION + '-Windows/'
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix='.partial', delete=False) as temporary:
        partial = Path(temporary.name)
    output_created = False
    checksum_created = False
    try:
        with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, body in [*launchers.items(), (MANIFEST, raw_manifest), *sorted(data.items())]:
                entry = zipfile.ZipInfo(prefix + name, date_time=(2026, 11, 2, 0, 0, 0))
                entry.external_attr = 0o100644 << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, body)
        # Exclusive creation also refuses a file/symlink appearing after the
        # initial check; publishing must never replace another user's output.
        with output.open('xb') as destination, partial.open('rb') as source_archive:
            output_created = True
            shutil.copyfileobj(source_archive, destination)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        with checksum.open('x', encoding='ascii', newline='\n') as target:
            checksum_created = True
            target.write(digest + '  ' + output.name + '\n')
    except BaseException:
        if checksum_created:
            checksum.unlink(missing_ok=True)
        if output_created:
            output.unlink(missing_ok=True)
        raise
    finally:
        partial.unlink(missing_ok=True)
    return {
        'product': PRODUCT, 'version': VERSION, 'filename': output.name,
        'sha256': digest, 'size': output.stat().st_size,
        'payload_files': len(data), 'archive_files': len(data) + len(LAUNCHERS) + 1,
        'manifest_sha256': manifest_hash,
        'launcher_sha256': {name: hashlib.sha256(body).hexdigest() for name, body in launchers.items()},
        'runtimes_bundled': False,
        'scope': 'Public source, built frontend and one-click bootstrap; no user data, credentials or installed runtimes',
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=PROJECT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
