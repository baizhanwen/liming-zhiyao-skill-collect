"""Build the narrow 0.2.2 -> 0.2.3 Windows code-only update. No account data is read."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import tomllib
import zipfile

PRODUCT = 'liming-zhiyao-skill-collect'
VERSION = '0.2.3'
FILES = (
    'backend/__init__.py', 'backend/browser_capture.py', 'backend/media.py',
    'backend/models.py', 'backend/task_progress.py',
    'frontend/scripts/capture-douyin.mjs', 'pyproject.toml', 'uv.lock',
    'frontend/package.json', 'frontend/package-lock.json',
)
MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 128
ASSET = re.compile(r'frontend/dist/assets/[A-Za-z0-9_-]+\.(?:js|css)\Z')
PROJECT = Path(__file__).resolve().parents[1]


class UpdateBuildError(ValueError):
    pass


def allowed_path(value: str) -> bool:
    if not isinstance(value, str) or not value.isascii() or '\\' in value or ':' in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in ('', '.', '..') for p in value.split('/')):
        return False
    if any(re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p) for p in value.split('/')):
        return False
    return value in FILES or value == 'frontend/dist/index.html' or bool(ASSET.fullmatch(value))


def no_links(path: Path) -> None:
    """Check every existing ancestor, including Windows junction/reparse points."""
    current = path.absolute()
    for candidate in (current, *current.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise UpdateBuildError('Links/reparse points are not valid update inputs')


def read_file(root: Path, relative: str) -> bytes:
    if not allowed_path(relative):
        raise UpdateBuildError('File is outside the fixed update allowlist')
    file = root.joinpath(*PurePosixPath(relative).parts)
    no_links(file)
    try:
        info = file.stat()
    except OSError:
        raise UpdateBuildError('A required update file is unavailable') from None
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE:
        raise UpdateBuildError('Update file type or size is invalid')
    data = file.read_bytes()
    if len(data) != info.st_size:
        raise UpdateBuildError('Update input changed during reading')
    return data


def build_update(source: Path, output: Path) -> dict:
    source, output = Path(source).absolute(), Path(output).absolute()
    no_links(source)
    no_links(output)
    if output.exists():
        raise UpdateBuildError('Output already exists; choose a new archive name')
    data = {}
    def add(name):
        if len(data) >= MAX_FILES:
            raise UpdateBuildError('Too many update files')
        body = read_file(source, name)
        if sum(map(len, data.values())) + len(body) > MAX_TOTAL:
            raise UpdateBuildError('Update exceeds its fixed package budget')
        data[name] = body
    for name in FILES:
        add(name)
    try:
        project = tomllib.loads(data['pyproject.toml'].decode())['project']
        package = json.loads(data['frontend/package.json'])
        lock = json.loads(data['frontend/package-lock.json'])
        uv_project = [p for p in tomllib.loads(data['uv.lock'].decode())['package'] if p['name'] == PRODUCT]
        version = re.fullmatch(rb"\s*__version__\s*=\s*['\"](0\.2\.3)['\"]\s*", data['backend/__init__.py'])
        if (project['name'] != PRODUCT or project['version'] != VERSION or not version or
                package['name'] != PRODUCT + '-workbench' or package['version'] != VERSION or
                lock['version'] != VERSION or lock['packages']['']['version'] != VERSION or
                len(uv_project) != 1 or uv_project[0]['version'] != VERSION):
            raise ValueError('version')
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise UpdateBuildError('The five version metadata files must consistently describe 0.2.3') from None
    dist = source / 'frontend/dist'
    no_links(dist)
    if not dist.is_dir():
        raise UpdateBuildError('Build frontend/dist before packaging')
    for current, dirs, names in os.walk(dist, followlinks=False):
        for name in [*dirs, *names]:
            file = Path(current) / name
            no_links(file)
            relative = file.relative_to(source).as_posix()
            if file.is_dir():
                if relative != 'frontend/dist/assets':
                    raise UpdateBuildError('Unexpected directory in compiled frontend')
            else:
                add(relative)
    if 'frontend/dist/index.html' not in data or not any(ASSET.fullmatch(name) and name.endswith('.js') for name in data):
        raise UpdateBuildError('Compiled index.html and JavaScript assets are required')
    if len(data) > MAX_FILES or sum(map(len, data.values())) > MAX_TOTAL:
        raise UpdateBuildError('Update exceeds its fixed package budget')
    if len({name.casefold() for name in data}) != len(data):
        raise UpdateBuildError('Case-insensitive duplicate paths are not allowed')
    # Do not allow a stale/missing bundled script to make a healthy-looking index.
    index = data['frontend/dist/index.html'].decode('utf-8')
    referenced = set()
    for link in re.findall(r'(?:src|href)=["\']([^"\']+)["\']', index):
        if link.startswith('/assets/'):
            if 'frontend/dist' + link not in data:
                raise UpdateBuildError('Compiled index references a missing asset')
            referenced.add('frontend/dist' + link)
        elif re.match(r'(?:https?:)?//', link):
            raise UpdateBuildError('Compiled frontend must not load external scripts/assets')
    if referenced != {name for name in data if ASSET.fullmatch(name)}:
        raise UpdateBuildError('Unexpected or unreferenced compiled asset')
    manifest = {'schema_version': 1, 'product': PRODUCT, 'version': VERSION,
        'supported_versions': ['0.2.2', '0.2.3'],
        'files': [{'path': path, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                  for path, body in sorted(data.items())]}
    raw_manifest = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    template = source / 'scripts/apply_windows_update.ps1'
    command = source / 'scripts/windows_update.cmd'
    no_links(template); no_links(command)
    ps = template.read_text(encoding='utf-8-sig')
    if ps.count('__MANIFEST_SHA256__') != 1:
        raise UpdateBuildError('Updater manifest pin placeholder is invalid')
    # Windows PowerShell 5.1 needs a BOM for Chinese messages.
    ps_bytes = ps.replace('__MANIFEST_SHA256__', manifest_hash).encode('utf-8-sig')
    cmd_bytes = command.read_text(encoding='utf-8-sig').replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8')
    notices = {}
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        file = source / name
        no_links(file)
        if not file.is_file() or not 0 < file.stat().st_size <= 65536:
            raise UpdateBuildError('Required redistribution notice is unavailable')
        notices[name] = file.read_bytes()
    prefix = 'skill-collect-update-' + VERSION + '/'
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix='.partial', delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, body in [
                ('manifest.json', raw_manifest), ('apply_windows_update.ps1', ps_bytes),
                ('升级并启动.cmd', cmd_bytes), *sorted(notices.items()),
                *[('payload/' + path, body) for path, body in sorted(data.items())],
            ]:
                entry = zipfile.ZipInfo(prefix + name, date_time=(2026, 11, 1, 0, 0, 0))
                entry.external_attr = 0o100644 << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, body)
        no_links(output)
        if output.exists():
            raise UpdateBuildError('Output appeared during packaging')
        # link with exclusive creation, rather than replacing an existing file.
        with output.open('xb') as destination, temporary.open('rb') as origin:
            while chunk := origin.read(1024 * 1024):
                destination.write(chunk)
        return {'version': VERSION, 'archive': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                'manifest_sha256': manifest_hash, 'payload_files': len(data), 'payload_bytes': sum(map(len, data.values()))}
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=PROJECT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build_update(args.source, args.output)
    except (OSError, UpdateBuildError) as exc:
        parser.exit(1, 'Update package not built: ' + str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
