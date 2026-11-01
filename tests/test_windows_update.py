"""Code-only update packaging, plus actual NoStart execution on Windows runners.

All configuration/data values below are synthetic sentinels; no account is read.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import sysconfig
import zipfile

import pytest

from scripts import build_windows_update as update

ROOT = Path(__file__).resolve().parents[1]


def source_fixture(tmp_path):
    root = tmp_path / 'source'
    for name in update.FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# synthetic public code\n', encoding='utf-8')
    (root / 'backend/__init__.py').write_text("__version__ = '0.2.3'\n")
    (root / 'pyproject.toml').write_text('[project]\nname="liming-zhiyao-skill-collect"\nversion="0.2.3"\n')
    (root / 'uv.lock').write_text('version=1\n[[package]]\nname="liming-zhiyao-skill-collect"\nversion="0.2.3"\n')
    package = {'name': update.PRODUCT + '-workbench', 'version': '0.2.3'}
    (root / 'frontend/package.json').write_text(json.dumps(package))
    (root / 'frontend/package-lock.json').write_text(json.dumps({**package, 'packages': {'': package}}))
    dist = root / 'frontend/dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'assets/index-contract.js').write_text('/* explicit synthetic compiled fixture */\n')
    (dist / 'assets/index-contract.css').write_text('/* explicit synthetic CSS fixture */\n')
    (dist / 'index.html').write_text('<script src="/assets/index-contract.js"></script><link href="/assets/index-contract.css">')
    (root / 'scripts').mkdir()
    for name in ('apply_windows_update.ps1', 'windows_update.cmd'):
        shutil.copyfile(ROOT / 'scripts' / name, root / 'scripts' / name)
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copyfile(ROOT / name, root / name)
    return root


def package_fixture(tmp_path):
    source = source_fixture(tmp_path)
    output = tmp_path / 'update.zip'
    report = update.build_update(source, output)
    folder = tmp_path / '解压补丁 synthetic'
    with zipfile.ZipFile(output) as archive:
        archive.extractall(folder)  # Our explicitly built, validated fixture only.
    return folder / 'skill-collect-update-0.2.3', report


def test_builder_includes_only_exact_public_payload_and_pins_manifest(tmp_path):
    source = source_fixture(tmp_path)
    for name in ('data/app.sqlite3', 'data/secrets.enc', 'data/.secrets.key', 'uploads/private.mp4',
                 'tasks/private.txt', '.env', 'tests/private.py', 'frontend/node_modules/private.js'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('EXPLICIT_SYNTHETIC_PRIVATE_SENTINEL')
    output = tmp_path / 'update.zip'
    report = update.build_update(source, output)
    with zipfile.ZipFile(output) as archive:
        prefix = 'skill-collect-update-0.2.3/'
        names = archive.namelist()
        assert len(names) == 18  # 13 payload files, 3 entry files and 2 license notices.
        for notice in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
            assert archive.read(prefix + notice) == (source / notice).read_bytes()
        assert not any(s in '\n'.join(names) for s in ('node_modules', 'app.sqlite3', 'secrets', 'private', '/tests/'))
        assert all(b'EXPLICIT_SYNTHETIC_PRIVATE_SENTINEL' not in archive.read(name) for name in names)
        raw = archive.read(prefix + 'manifest.json')
        manifest = json.loads(raw)
        assert hashlib.sha256(raw).hexdigest() == report['manifest_sha256']
        script = archive.read(prefix + 'apply_windows_update.ps1')
        assert script.startswith(b'\xef\xbb\xbf') and report['manifest_sha256'].encode() in script
        assert b'__MANIFEST_SHA256__' not in script
        for entry in manifest['files']:
            body = archive.read(prefix + 'payload/' + entry['path'])
            assert entry['size'] == len(body) and entry['sha256'] == hashlib.sha256(body).hexdigest()
        assert {x['path'] for x in manifest['files']} == set(update.FILES) | {
            'frontend/dist/index.html', 'frontend/dist/assets/index-contract.js', 'frontend/dist/assets/index-contract.css'}
    assert hashlib.sha256(output.read_bytes()).hexdigest() == report['sha256']
    with pytest.raises(update.UpdateBuildError, match='already exists'):
        update.build_update(source, output)


@pytest.mark.parametrize('path', ['../data/x', '/backend/models.py', 'backend/../data/x',
    'backend\\models.py', 'C:/data/x', 'frontend/dist/assets/x.js:secret',
    'frontend/dist/assets/NUL.js', 'frontend/dist/assets/COM1.css', 'data/app.sqlite3',
    'backend/main.py', 'frontend/scripts/douyin-login.mjs', 'frontend/dist/assets/../x.js'])
def test_builder_rejects_paths_outside_narrow_allowlist(path):
    assert not update.allowed_path(path)


@pytest.mark.parametrize('change', ['version', 'extra_asset', 'missing_asset', 'private_file', 'external_script'])
def test_builder_rejects_inconsistent_or_unexpected_build_inputs(tmp_path, change):
    source = source_fixture(tmp_path)
    if change == 'version':
        (source / 'backend/__init__.py').write_text("__version__='0.3.0'\n")
    elif change == 'extra_asset':
        (source / 'frontend/dist/assets/unreferenced.js').write_text('synthetic must not be packaged')
    elif change == 'missing_asset':
        (source / 'frontend/dist/assets/index-contract.js').unlink()
    elif change == 'private_file':
        (source / 'frontend/dist/secrets.enc').write_bytes(b'synthetic only')
    else:
        (source / 'frontend/dist/index.html').write_text('<script src="https://contract.invalid/x.js"></script>')
    with pytest.raises(update.UpdateBuildError):
        update.build_update(source, tmp_path / 'bad.zip')
    assert not (tmp_path / 'bad.zip').exists()


def test_builder_rejects_symlink_input_and_package_limits(tmp_path, monkeypatch):
    source = source_fixture(tmp_path)
    file = source / 'backend/models.py'
    outside = tmp_path / 'outside'
    outside.write_text('synthetic external file')
    file.unlink()
    try:
        file.symlink_to(outside)
    except OSError:
        pytest.skip('Windows symlink creation may require developer mode; actual junction tests run separately')
    with pytest.raises(update.UpdateBuildError, match='Links'):
        update.build_update(source, tmp_path / 'links.zip')
    assert outside.read_text() == 'synthetic external file'
    file.unlink(); file.write_text('synthetic public file')
    monkeypatch.setattr(update, 'MAX_TOTAL', 10)
    with pytest.raises(update.UpdateBuildError, match='budget'):
        update.build_update(source, tmp_path / 'large.zip')


WINDOWS = pytest.mark.skipif(os.name != 'nt', reason='actual Windows PowerShell filesystem/NoStart contract')


def target_fixture(tmp_path):
    target = tmp_path / "旧项目 synthetic ' dollar$ &"
    target.mkdir()
    (target / 'backend').mkdir()
    (target / 'frontend/dist/assets').mkdir(parents=True)
    (target / 'backend/__init__.py').write_text("__version__ = '0.2.2'\n")
    (target / 'backend/main.py').write_text('# explicit marker, never imported\n')
    (target / 'pyproject.toml').write_text('[project]\nname="liming-zhiyao-skill-collect"\nversion="0.2.2"\n')
    (target / 'frontend/package.json').write_text(json.dumps({'name': update.PRODUCT + '-workbench', 'version': '0.2.2'}))
    for name in update.FILES:
        path = target / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# synthetic old code\n')
    (target / 'frontend/dist/index.html').write_text('synthetic old index')
    (target / 'frontend/dist/assets/index-old.js').write_text('synthetic old bundle')
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(target / '.venv')], check=True, capture_output=True, timeout=30)
    # Fixture-only dependency lookup; no installation and no account setup.
    site = target / '.venv/Lib/site-packages'
    site.mkdir(parents=True, exist_ok=True)
    (site / 'explicit-fixture.pth').write_text(sysconfig.get_path('purelib') + '\n', encoding='utf-8')
    for name in ('data/app.sqlite3', 'data/.secrets.key', 'data/secrets.enc', 'uploads/private.mp4', 'tasks/private.txt', '.env'):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('EXPLICIT_SYNTHETIC_USER_DATA_' + name)
    return target


def tree_bytes(target):
    return {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob('*')
            if p.is_file() and '.venv' not in p.parts and '.skill-collect-update-backups' not in p.parts}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def invoke(package, target, *, shell='pwsh', port=None):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(shell + ' is unavailable')
    return subprocess.run([executable, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        str(package / 'apply_windows_update.ps1'), '-ProjectPath', str(target), '-NoStart', '-Port', str(port or free_port())],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)


def invoke_launcher(package, target):
    args = [str(package / '升级并启动.cmd'), '-ProjectPath', str(target), '-NoStart', '-Port', str(free_port())]
    # cmd /s /c removes the outer quote pair; inner quoted arguments remain.
    command = '"' + os.environ['COMSPEC'] + '" /d /s /c "' + subprocess.list2cmdline(args) + '"'
    env = dict(os.environ, SKILL_COLLECT_UPDATE_NO_PAUSE='1')
    return subprocess.run(command,
        env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)


@WINDOWS
@pytest.mark.parametrize('entrypoint', ['pwsh', 'cmd'])
def test_actual_windows_nostart_updates_only_code_and_retains_backup_and_data(tmp_path, entrypoint):
    package, _ = package_fixture(tmp_path)
    target = target_fixture(tmp_path)
    before = tree_bytes(target)
    result = invoke(package, target) if entrypoint == 'pwsh' else invoke_launcher(package, target)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((package / 'manifest.json').read_text())
    for entry in manifest['files']:
        assert hashlib.sha256((target / entry['path']).read_bytes()).hexdigest() == entry['sha256']
    for name, body in before.items():
        if name not in {x['path'] for x in manifest['files']}:
            assert (target / name).read_bytes() == body
    backups = list((target / '.skill-collect-update-backups').glob('0.2.3-*/backup.json'))
    assert len(backups) == 1
    assert (backups[0].parent / 'backend/__init__.py').read_bytes() == before['backend/__init__.py']
    assert not (target / '.skill-collect-update-backups/update.lock').exists()
    # Reapplying the same version supports a later source hash without migrations.
    reapplied = invoke(package, target) if entrypoint == 'pwsh' else invoke_launcher(package, target)
    assert reapplied.returncode == 0, reapplied.stdout + reapplied.stderr


@WINDOWS
def test_actual_windows_powershell51_is_explicitly_unsupported_without_any_write(tmp_path):
    package, _ = package_fixture(tmp_path)
    target = target_fixture(tmp_path)
    before = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    result = invoke(package, target, shell='powershell')
    assert result.returncode == 1
    assert 'PowerShell 7' in result.stdout and '5.1' in result.stdout
    assert 'learn.microsoft.com/powershell/' in result.stdout
    assert {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob('*') if p.is_file()} == before
    assert not (target / '.skill-collect-update-backups').exists()


@WINDOWS
def test_actual_windows_pwsh_sta_can_construct_required_folder_picker_without_display():
    script = "Add-Type -AssemblyName System.Windows.Forms; $picker = [Windows.Forms.FolderBrowserDialog]::new(); $picker.Dispose(); Write-Output 'SYNTHETIC_GUI_TYPES_READY'"
    result = subprocess.run([shutil.which('pwsh'), '-NoLogo', '-NoProfile', '-STA', '-Command', script],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'SYNTHETIC_GUI_TYPES_READY' in result.stdout


@WINDOWS
@pytest.mark.parametrize('case', ['manifest_hash', 'payload_hash', 'extra_payload', 'wrong_version', 'wrong_product'])
def test_actual_windows_rejects_invalid_package_or_project_before_any_code_write(tmp_path, case):
    package, _ = package_fixture(tmp_path)
    target = target_fixture(tmp_path)
    if case == 'manifest_hash':
        with (package / 'manifest.json').open('a') as f: f.write(' ')
    elif case == 'payload_hash':
        (package / 'payload/backend/models.py').write_text('synthetic corruption')
    elif case == 'extra_payload':
        (package / 'payload/unauthorized.txt').write_text('synthetic unlisted')
    elif case == 'wrong_version':
        (target / 'backend/__init__.py').write_text("__version__='0.3.0'\n")
    else:
        (target / 'pyproject.toml').write_text('[project]\nname="other-project"\nversion="0.2.2"\n')
    before = tree_bytes(target)
    assert invoke(package, target).returncode != 0
    assert tree_bytes(target) == before


@WINDOWS
@pytest.mark.parametrize('path', ['../data/secrets.enc', 'C:/outside.py',
    'backend/main.py', 'frontend/dist/assets/NUL.js'])
def test_actual_windows_even_a_repinned_manifest_cannot_escape_fixed_update_scope(tmp_path, path):
    package, _ = package_fixture(tmp_path)
    target = target_fixture(tmp_path)
    manifest_path = package / 'manifest.json'
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    manifest['files'][0]['path'] = path
    changed = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    manifest_path.write_bytes(changed)
    script = package / 'apply_windows_update.ps1'
    text = script.read_text(encoding='utf-8-sig').replace(hashlib.sha256(original).hexdigest(), hashlib.sha256(changed).hexdigest())
    script.write_text(text, encoding='utf-8-sig')  # Explicit trusted-fixture manifest pin.
    before = tree_bytes(target)
    assert invoke(package, target).returncode != 0
    assert tree_bytes(target) == before


@WINDOWS
def test_actual_windows_listener_is_not_killed_or_upgraded(tmp_path):
    package, _ = package_fixture(tmp_path)
    target = target_fixture(tmp_path)
    before = tree_bytes(target)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); listener.listen()
        assert invoke(package, target, port=listener.getsockname()[1]).returncode != 0
        assert listener.fileno() >= 0
        assert tree_bytes(target) == before


@WINDOWS
def test_actual_windows_copy_failure_rolls_back_old_code_and_removes_new_assets(tmp_path):
    package, _ = package_fixture(tmp_path)
    target = target_fixture(tmp_path)
    before = tree_bytes(target)
    script = package / 'apply_windows_update.ps1'
    text = script.read_text(encoding='utf-8-sig')
    needle = '        Write-CodeFile $Source $Destination'
    assert text.count(needle) == 1
    # After the last file was replaced: prove current-file rollback as well as
    # restoration of earlier files and removal of assets absent in the old tree.
    marker = 'SYNTHETIC_LATE_COPY_FAILURE_REACHED'
    text = text.replace(needle, needle + "\n        if ($Entry.path -ceq 'uv.lock') { Write-Host '" + marker + "'; throw 'synthetic-copy-failure' }")
    script.write_text(text, encoding='utf-8-sig')
    result = invoke(package, target)
    assert result.returncode != 0
    assert marker in result.stdout, 'Late injected failure was not reached: ' + result.stdout + result.stderr
    assert tree_bytes(target) == before
    assert (target / 'uv.lock').read_bytes() == before['uv.lock']
    assert not (target / 'frontend/dist/assets/index-contract.js').exists()
    assert not (target / 'frontend/dist/assets/index-contract.css').exists()
    assert list((target / '.skill-collect-update-backups').glob('0.2.3-*/backup.json'))


@WINDOWS
@pytest.mark.parametrize('location', ['target', 'payload'])
def test_actual_windows_junction_is_rejected_without_touching_external_files(tmp_path, location):
    package, _ = package_fixture(tmp_path)
    target = target_fixture(tmp_path)
    outside = tmp_path / 'outside-backend'
    destination = target / 'backend' if location == 'target' else package / 'payload/backend'
    shutil.copytree(destination, outside)
    shutil.rmtree(destination)
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(destination), str(outside)], capture_output=True, timeout=10)
    assert result.returncode == 0
    external = {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()}
    assert invoke(package, target).returncode != 0
    assert {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()} == external
