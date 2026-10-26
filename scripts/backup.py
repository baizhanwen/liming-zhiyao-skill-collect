"""Private backup/restore, with SQLite snapshots and a strict file allowlist.

CLI: python scripts/backup.py backup --output backups/private.zip
     python scripts/backup.py restore backups/private.zip --target /empty/data
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

MAX_TOTAL = 512 * 1024 * 1024
MAX_SINGLE = 128 * 1024 * 1024
MAX_FILES = 5000
ALLOWED_DIRS = {'tasks', 'uploads'}


class BackupError(ValueError):
    pass


def _allowed(name):
    p = PurePosixPath(name)
    return name == 'app.sqlite3' or (p.parts and p.parts[0] in ALLOWED_DIRS and len(p.parts) > 1)


def _safe(name):
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or '\x00' in name
            or str(p) != name or not _allowed(name)):
        raise BackupError('备份包含不合法路径或不允许恢复的文件')
    return p


def make_backup(store) -> bytes:
    """Only DB + tasks + uploads; encryption secrets/logs/locks never enter the archive."""
    root = Path(store.root).resolve()
    payloads = []
    with store.lock, tempfile.TemporaryDirectory(prefix='skill-collect-snapshot-') as temporary:
        snapshot = Path(temporary) / 'app.sqlite3'
        store.backup_to(snapshot)
        payloads.append(('app.sqlite3', snapshot.read_bytes()))
        total = len(payloads[0][1])
        for directory in sorted(ALLOWED_DIRS):
            base = root / directory
            if base.is_symlink():
                raise BackupError('资料目录不能是符号链接')
            if not base.exists():
                continue
            for path in sorted(base.rglob('*')):
                if path.is_symlink():
                    raise BackupError('备份不支持符号链接，请先移除链接后重试')
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                _safe(relative)
                size = path.stat().st_size
                if size > MAX_SINGLE or total + size > MAX_TOTAL or len(payloads) >= MAX_FILES:
                    raise BackupError('备份超过上限：单文件 128 MiB、总量 512 MiB、5000 个文件')
                raw = path.read_bytes()
                if len(raw) > MAX_SINGLE:
                    raise BackupError('备份期间文件大小超过上限，请停止相关任务再试')
                payloads.append((relative, raw))
                total += len(raw)
        if total > MAX_TOTAL or any(len(raw) > MAX_SINGLE for _, raw in payloads):
            raise BackupError('备份超过大小上限')
        manifest = {'format_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'original_data_root': str(root), 'credentials_included': False,
                    'notice': '包含个人资料、媒体和企业技能，请妥善保管，禁止发布到 GitHub。恢复后重新配置凭证。',
                    'files': [{'path': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                              for name, raw in payloads]}
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        if len(manifest_bytes) > 1024 * 1024:
            raise BackupError('备份文件清单超过 1 MiB，请缩小资料范围')
        out = io.BytesIO()
        # Stored avoids decompression bombs in our own archives and media rarely compresses well.
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as archive:
            for name, raw in payloads:
                info = zipfile.ZipInfo(name)
                info.external_attr = 0o100600 << 16
                archive.writestr(info, raw)
            archive.writestr('backup-manifest.json', manifest_bytes)
        return out.getvalue()


def _remap_value(value, old_root, target, key=''):
    if isinstance(value, dict):
        return {k: _remap_value(v, old_root, target, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap_value(v, old_root, target, key) for v in value]
    if key not in ('path', 'media_path') or not isinstance(value, str):
        return value
    # Normalize both separator styles so Windows backups can be restored on macOS/Linux.
    normalized, old = value.replace('\\', '/'), old_root.replace('\\', '/').rstrip('/')
    if normalized.startswith(old + '/'):
        rel = normalized[len(old) + 1:]
        _safe(rel)
        return str(target.joinpath(*PurePosixPath(rel).parts))
    if normalized.startswith('/') or re.match(r'^[A-Za-z]:/', normalized):
        raise BackupError('数据库引用了原资料目录之外的本地文件，恢复已停止')
    return value


def _restore_database(path, original_root, final_target):
    # A sqlite Connection context commits/rolls back but does not close its handle.
    # The handle must close before Windows can move the restored staging directory.
    with closing(sqlite3.connect(path)) as db, db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise BackupError('备份数据库完整性检查失败')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'entities', 'migrations'}.issubset(tables):
            raise BackupError('备份不是受支持的应用数据库')
        for kind, id_, raw in db.execute('SELECT kind,id,data FROM entities').fetchall():
            value = _remap_value(json.loads(raw), original_root, final_target)
            if kind == 'tasks' and value.get('status') in ('queued', 'running'):
                value['status'] = 'waiting_config'
                value['error'] = {'code': 'configuration', 'message': '备份已恢复，请检查模型与外部连接配置后手动继续',
                                  'retryable': False, 'stage': value.get('stage', 'recovery')}
                value.setdefault('stages', []).append({'stage': 'restore', 'status': 'waiting_config',
                    'message': '备份已恢复，等待检查设置后手动继续', 'at': datetime.now(timezone.utc).isoformat()})
            if kind in ('model_configs', 'connections', 'settings'):
                value.pop('last_test', None)
                value.pop('last_probe', None)
            db.execute('UPDATE entities SET data=? WHERE kind=? AND id=?',
                       (json.dumps(value, ensure_ascii=False), kind, id_))
        db.commit()
        db.execute('PRAGMA journal_mode=DELETE')


def restore_backup(data: bytes, target) -> dict:
    """Validate entirely, restore atomically to an empty directory, never merge/overwrite."""
    destination = Path(target).absolute()
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise BackupError('恢复目标必须是不存在或为空的资料目录；不会覆盖现有数据')
    if len(data) > MAX_TOTAL + 4 * 1024 * 1024:
        raise BackupError('备份文件超过 512 MiB 上限')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.skill-collect-restore-', dir=destination.parent))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES + 1:
                raise BackupError('备份文件数超过上限')
            names, total = set(), 0
            for info in entries:
                name = info.filename
                if name in names or info.is_dir() or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                    raise BackupError('备份包含重名、目录、加密或符号链接条目')
                names.add(name)
                if name != 'backup-manifest.json':
                    _safe(name)
                if info.file_size > MAX_SINGLE or (info.compress_size == 0 and info.file_size > 0) or info.file_size > max(info.compress_size, 1) * 1000:
                    raise BackupError('备份文件大小或压缩比例超过限制')
                total += info.file_size
                if total > MAX_TOTAL + 1024 * 1024:
                    raise BackupError('备份解压总量超过上限')
            manifest_raw = archive.read('backup-manifest.json')
            if len(manifest_raw) > 1024 * 1024:
                raise BackupError('备份清单超过大小上限')
            manifest = json.loads(manifest_raw)
            if manifest.get('format_version') != 1 or manifest.get('credentials_included') is not False:
                raise BackupError('不支持该备份格式或其包含凭证')
            original = manifest.get('original_data_root')
            if not isinstance(original, str) or not original:
                raise BackupError('备份缺少原资料目录信息')
            files = manifest.get('files', [])
            if not isinstance(files, list) or len(files) > MAX_FILES:
                raise BackupError('备份清单不合法')
            expected = {f['path'] for f in files}
            if expected | {'backup-manifest.json'} != names or len(expected) != len(files) or 'app.sqlite3' not in expected:
                raise BackupError('备份文件与清单不一致')
            for item in files:
                name = item['path']
                relative = _safe(name)
                raw = archive.read(name)
                if len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                    raise BackupError('备份文件哈希或长度校验失败')
                path = staging.joinpath(*relative.parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                if os.name != 'nt':
                    path.chmod(0o600)
        _restore_database(staging / 'app.sqlite3', original, destination)
        if destination.exists():
            if any(destination.iterdir()):
                raise BackupError('恢复目标在验证期间发生变化，恢复已停止')
            destination.rmdir()
        staging.replace(destination)
        if os.name != 'nt':
            destination.chmod(0o700)
        return {'restored': True, 'target': str(destination), 'files': len(files),
                'credentials_restored': False, 'notice': '请重新配置凭证并分别测试模型、GitHub 和飞书连接；待处理任务需手动继续。'}
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError('备份格式、清单或数据库无效，恢复已停止') from None
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description='私有资料备份与安全恢复；备份不含凭证，不可公开。')
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('backup')
    b.add_argument('--data', default=os.environ.get('SKILL_COLLECT_DATA', str(PROJECT / 'data')))
    b.add_argument('--output', required=True)
    r = sub.add_parser('restore')
    r.add_argument('archive')
    r.add_argument('--target', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'backup':
            from backend.store import Store
            if not (Path(args.data) / 'app.sqlite3').is_file():
                raise BackupError('资料库尚未初始化，请先完成安装')
            output = Path(args.output)
            if output.exists():
                raise BackupError('输出文件已存在，请选择新文件名；不会覆盖旧备份')
            output.parent.mkdir(parents=True, exist_ok=True)
            raw = make_backup(Store(args.data))
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            print(json.dumps({'saved': str(output.absolute()), 'bytes': len(raw), 'credentials_included': False,
                              'notice': '备份包含私人资料和媒体，请妥善保管，不要发布到 GitHub。'}, ensure_ascii=False))
        else:
            archive = Path(args.archive)
            if archive.stat().st_size > MAX_TOTAL + 4 * 1024 * 1024:
                raise BackupError('备份文件超过大小上限')
            print(json.dumps(restore_backup(archive.read_bytes(), args.target), ensure_ascii=False))
    except (BackupError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
