"""Distribution/data tests use only temporary local fixtures, no credentials or network."""
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile
import socket

import pytest

from backend.store import Store
from scripts.doctor import port_available


def test_port_check_detects_listener_without_stopping_it():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen()
        assert not port_available(listener.getsockname()[1])
        assert listener.fileno()>=0


@pytest.mark.skipif(os.name=='nt',reason='POSIX address reuse semantics')
def test_port_check_allows_immediate_restart_after_time_wait():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
        with socket.create_connection(('127.0.0.1',port)) as client:
            accepted,_=listener.accept();accepted.close();client.recv(1)
    assert port_available(port)
from scripts.backup import BackupError, make_backup, restore_backup
from scripts.doctor import diagnose
from scripts.doctor import browser_runtime_check
from scripts.install_browser import install_browser


def fixture_store(tmp_path):
    store = Store(tmp_path / 'original')
    (store.root / 'uploads').mkdir()
    (store.root / 'uploads' / 'captions.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nContract\n')
    (store.root / 'tasks' / 'task-1').mkdir(parents=True)
    (store.root / 'tasks' / 'task-1' / 'frame.jpg').write_bytes(b'contract frame')
    (store.root / 'logs').mkdir()
    (store.root / 'logs' / 'server.log').write_text('private log excluded')
    (store.root / '.secrets.key').write_text('contract secret excluded')
    (store.root / 'secrets.enc').write_text('contract encrypted credentials excluded')
    store.put('knowledge', {'id': 'card', 'title': '可恢复的知识', 'body': '真实数据库行为测试', 'tags': []})
    store.put('tasks', {'id': 'task-1', 'status': 'running', 'stage': 'analysis', 'uploads': [
        {'path': str(store.root / 'uploads' / 'captions.srt')}], 'media': {
        'frames': [{'path': str(store.root / 'tasks' / 'task-1' / 'frame.jpg')}]}})
    store.put('model_configs', {'id': 'text', 'model': 'fixture', 'base_url': 'http://localhost:9999/v1',
                               'last_test': {'available': True}})
    return store


def test_backup_restore_integrity_and_no_credentials(tmp_path):
    store = fixture_store(tmp_path)
    raw = make_backup(store)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert 'secrets.enc' not in z.namelist()
        assert '.secrets.key' not in z.namelist()
        assert not any('logs/' in n for n in z.namelist())
        manifest = json.loads(z.read('backup-manifest.json'))
        assert manifest['credentials_included'] is False
    target = tmp_path / 'restored'
    result = restore_backup(raw, target)
    assert result['restored'] is True
    assert (target / 'uploads' / 'captions.srt').read_bytes() == (store.root / 'uploads' / 'captions.srt').read_bytes()
    restored = Store(target)
    assert restored.find('knowledge', '可恢复')[0]['id'] == 'card'
    task = restored.get('tasks', 'task-1')
    assert task['status'] == 'waiting_config'
    assert task['uploads'][0]['path'] == str(target / 'uploads' / 'captions.srt')
    assert task['media']['frames'][0]['path'] == str(target / 'tasks' / 'task-1' / 'frame.jpg')
    assert 'last_test' not in restored.get('model_configs', 'text')
    assert not (target / 'secrets.enc').exists()
    assert not (target / '.secrets.key').exists()


def test_restore_never_overwrites_existing_user_data(tmp_path):
    store = fixture_store(tmp_path)
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'preserve.txt').write_text('preserve')
    with pytest.raises(BackupError, match='不会覆盖'):
        restore_backup(make_backup(store), target)
    assert (target / 'preserve.txt').read_text() == 'preserve'


def test_restore_allows_existing_empty_directory(tmp_path):
    store = fixture_store(tmp_path)
    target = tmp_path / 'empty'
    target.mkdir()
    assert restore_backup(make_backup(store), target)['restored']


def test_tampered_archive_is_rejected_before_target_creation(tmp_path):
    store = fixture_store(tmp_path)
    raw = make_backup(store)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(out, 'w') as target:
        for name in original.namelist():
            target.writestr(name, b'changed' if name.endswith('captions.srt') else original.read(name))
    destination = tmp_path / 'tampered'
    with pytest.raises(BackupError, match='哈希|长度'):
        restore_backup(out.getvalue(), destination)
    assert not destination.exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'uploads/../../escape', 'uploads\\evil', 'secrets.enc'])
def test_restore_rejects_unsafe_or_non_allowlist_paths(tmp_path, name):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr(name, b'bad')
    with pytest.raises(BackupError):
        restore_backup(out.getvalue(), tmp_path / 'unsafe')
    assert not (tmp_path / 'escape').exists()


@pytest.mark.skipif(os.name == 'nt', reason='symlink creation may require Windows developer mode')
def test_backup_rejects_symlink_media(tmp_path):
    store = fixture_store(tmp_path)
    (store.root / 'uploads' / 'link').symlink_to(tmp_path / 'other')
    with pytest.raises(BackupError, match='符号链接'):
        make_backup(store)


def test_sqlite_backup_captures_wal_committed_data(tmp_path):
    store = fixture_store(tmp_path)
    for i in range(5):
        store.put('skills', {'id': str(i), 'name': 'version' + str(i), 'body': 'content'})
    target = tmp_path / 'wal'
    restore_backup(make_backup(store), target)
    restored = Store(target)
    assert len(restored.list('skills')) == 5
    with sqlite3.connect(restored.path) as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_doctor_does_not_read_credentials(tmp_path):
    store = fixture_store(tmp_path)
    r = diagnose(store.root)
    serialized = json.dumps(r)
    assert 'contract secret' not in serialized
    assert any(c['name'] == '资料库' and c['ok'] for c in r['checks'])
    assert (store.root / 'secrets.enc').read_text() == 'contract encrypted credentials excluded'


def test_backup_cli_round_trip(tmp_path):
    store = fixture_store(tmp_path)
    archive = tmp_path / 'private.zip'
    p = subprocess.run([sys.executable, 'scripts/backup.py', 'backup', '--data', str(store.root), '--output', str(archive)], capture_output=True, text=True, check=True)
    assert json.loads(p.stdout)['credentials_included'] is False
    restored = tmp_path / 'from-cli'
    p = subprocess.run([sys.executable, 'scripts/backup.py', 'restore', str(archive), '--target', str(restored)], capture_output=True, text=True, check=True)
    assert json.loads(p.stdout)['restored'] is True
    assert Store(restored).get('knowledge', 'card')['title'] == '可恢复的知识'


def test_browser_installer_uses_locked_cli_official_hosts_and_preserves_global_env(tmp_path, monkeypatch):
    import scripts.install_browser as module
    project = tmp_path / 'project with spaces'
    cli = project / 'frontend/node_modules/@playwright/test/cli.js'
    cli.parent.mkdir(parents=True)
    cli.write_text('// official package CLI contract fixture')
    runtime = project / 'frontend/scripts/capture-douyin.mjs'
    runtime.parent.mkdir(parents=True)
    runtime.write_text('// readonly runtime check fixture')
    calls = []
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/contract/node')
    monkeypatch.setenv('PLAYWRIGHT_DOWNLOAD_HOST', 'https://contract-mirror.invalid')
    monkeypatch.setenv('PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST', 'https://contract-chromium.invalid')
    monkeypatch.setenv('PLAYWRIGHT_FFMPEG_DOWNLOAD_HOST', 'https://contract-ffmpeg.invalid')
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', '/contract/user-cache')

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({'ok': True, 'browser_installed': True}))
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert install_browser(project) == 0
    args, kwargs = calls[0]
    assert args == ['/contract/node', str(cli), 'install', 'chromium']
    assert not any(name in kwargs['env'] for name in ('PLAYWRIGHT_DOWNLOAD_HOST', 'PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST', 'PLAYWRIGHT_FFMPEG_DOWNLOAD_HOST'))
    assert kwargs['env']['PLAYWRIGHT_BROWSERS_PATH'] == '/contract/user-cache'
    assert os.environ['PLAYWRIGHT_DOWNLOAD_HOST'] == 'https://contract-mirror.invalid'
    assert kwargs['cwd'] == project and kwargs['timeout'] == 900
    assert calls[1][0] == ['/contract/node', str(runtime), '--check-runtime']


@pytest.mark.parametrize('outcome', ['failed', 'timeout', 'missing'])
def test_browser_install_failure_is_visible_without_installing_os_packages(tmp_path, monkeypatch, capsys, outcome):
    import scripts.install_browser as module
    cli = tmp_path / 'frontend/node_modules/@playwright/test/cli.js'
    if outcome != 'missing':
        cli.parent.mkdir(parents=True)
        cli.write_text('// contract CLI')
        runtime = tmp_path / 'frontend/scripts/capture-douyin.mjs'
        runtime.parent.mkdir(parents=True)
        runtime.write_text('// readonly runtime check fixture')
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/contract/node')
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(args, 900)
        return subprocess.CompletedProcess(args, 1)
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert install_browser(tmp_path) == 1
    if outcome == 'missing':
        assert calls == []
    else:
        assert calls[0][-2:] == ['install', 'chromium']
    assert 'setup' in capsys.readouterr().err


def test_installer_success_without_actual_browser_file_is_not_reported_as_ready(tmp_path, monkeypatch):
    import scripts.install_browser as module
    for relative in ('frontend/node_modules/@playwright/test/cli.js', 'frontend/scripts/capture-douyin.mjs'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('// contract fixture')
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/contract/node')
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout=json.dumps({'ok': False, 'browser_installed': False})))
    assert install_browser(tmp_path) == 1


@pytest.mark.parametrize('returncode,report,ok', [
    (0, {'ok': True, 'browser_installed': True, 'playwright_version': '1.64.1'}, True),
    (0, {'ok': True, 'browser_installed': False}, False),
    (1, {'ok': False, 'browser_installed': False, 'error': {'message': 'SYNTHETIC_SECRET'}}, False),
    (0, [], False),
])
def test_doctor_browser_runtime_check_is_readonly_and_does_not_echo_errors(tmp_path, monkeypatch, returncode, report, ok):
    import scripts.doctor as module
    script = tmp_path / 'frontend/scripts/capture-douyin.mjs'
    script.parent.mkdir(parents=True)
    script.write_text('// check-runtime contract fixture')
    monkeypatch.setattr(module, 'PROJECT', tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout=json.dumps(report), stderr='SYNTHETIC_SECRET')
    monkeypatch.setattr(module.subprocess, 'run', run)
    result = browser_runtime_check('/contract/node')
    assert result['ok'] is ok and result['required'] is True
    assert calls == [['/contract/node', str(script), '--check-runtime']]
    assert 'SYNTHETIC_SECRET' not in json.dumps(result)


def test_doctor_missing_browser_source_does_not_launch_or_download(tmp_path, monkeypatch):
    import scripts.doctor as module
    monkeypatch.setattr(module, 'PROJECT', tmp_path)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: pytest.fail('No launch/download in doctor'))
    assert browser_runtime_check('/contract/node')['ok'] is False
