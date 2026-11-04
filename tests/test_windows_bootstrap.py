"""Actual Windows bootstrap contracts with explicit local synthetic user data.

These tests do not download runtimes, visit Douyin, call models or read account
data. The separate Windows installation job records real runtime installation.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = pytest.mark.skipif(os.name != 'nt', reason='actual Windows PowerShell 5.1 bootstrap contract')
PRODUCT = 'liming-zhiyao-skill-collect'
SECRET = 'EXPLICIT_SYNTHETIC_BOOTSTRAP_PRIVATE_SENTINEL'
PROTECTED = (
    'data/app.sqlite3', 'data/.secrets.key', 'data/secrets.enc',
    'uploads/private.mp4', 'tasks/private.txt', '.env',
)


def project_fixture(tmp_path):
    project = tmp_path / "旧项目 synthetic ' dollar$ &"
    project.mkdir()
    for relative in (
        'scripts/windows_bootstrap.ps1', 'scripts/windows-runtime-lock.json',
        'scripts/doctor.py', 'scripts/install_browser.py',
        'pyproject.toml', 'requirements.txt', 'backend/__init__.py',
        'backend/main.py', 'frontend/package.json', 'frontend/package-lock.json',
        'frontend/scripts/capture-douyin.mjs',
    ):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    # Deliberately not a usable database, credentials or media file. A read-only
    # environment check must not open these as business inputs.
    for relative in PROTECTED:
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SECRET + ':' + relative, encoding='utf-8')
    return project


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def protected_bytes(root):
    return {relative: (root / relative).read_bytes() for relative in PROTECTED}


def persistent_path_hashes():
    """Return hashes only; never put a user's PATH contents in test reports."""
    import winreg
    values = []
    for hive, key in ((winreg.HKEY_CURRENT_USER, 'Environment'),
                      (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, kind = winreg.QueryValueEx(handle, 'Path')
            values.append(hashlib.sha256((str(kind) + ':' + value).encode('utf-8')).hexdigest())
        except FileNotFoundError:
            values.append(None)
    return values


def invoke(project, *arguments, shell='powershell'):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(shell + ' is unavailable')
    return subprocess.run(
        [executable, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(project / 'scripts/windows_bootstrap.ps1'), '-ProjectPath', str(project),
         '-Json', *arguments],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=50,
    )


def report_from(result):
    # The Json switch is a machine-readable interface. A parse failure here is
    # a contract failure, not permission to find a convenient JSON substring.
    report = json.loads(result.stdout)
    assert report['schema_version'] == 1
    assert report['product'] == PRODUCT
    assert type(report['ok']) is bool
    assert isinstance(report['checks'], list)
    assert SECRET not in result.stdout + result.stderr
    return report


def invoke_helper(tmp_path, commands, **values):
    """Dot-source the real helpers; paths are environment data, never PS code."""
    shell = shutil.which('powershell')
    if not shell:
        pytest.skip('Windows PowerShell 5.1 is unavailable')
    wrapper = tmp_path / 'explicit-helper-contract.ps1'
    wrapper.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        ". $env:SC_CONTRACT_SCRIPT\n" + commands + '\n', encoding='utf-8-sig')
    return subprocess.run(
        [shell, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(wrapper)],
        env=dict(os.environ, SC_CONTRACT_SCRIPT=str(ROOT / 'scripts/windows_bootstrap.ps1'),
                 **{key: str(value) for key, value in values.items()}),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
    )


@WINDOWS
@pytest.mark.parametrize('shell', ['powershell', 'pwsh'])
def test_actual_windows_check_only_is_zero_write_and_does_not_read_user_inputs(tmp_path, shell):
    project = project_fixture(tmp_path)
    before = tree_bytes(project)
    paths = persistent_path_hashes()
    result = invoke(project, '-CheckOnly', '-ForcePrivateRuntimes', shell=shell)
    report = report_from(result)
    assert report['mode'] == 'check'
    assert result.returncode != 0 and report['ok'] is False
    checks = {item['name']: item for item in report['checks']}
    for name in ('python', 'node', 'ffmpeg', 'ffprobe', 'chromium', 'python_dependencies', 'frontend'):
        assert name in checks
        assert checks[name]['ok'] is False
    assert tree_bytes(project) == before
    assert persistent_path_hashes() == paths
    assert not (project / '.runtime').exists()


@WINDOWS
def test_actual_windows_offline_missing_runtime_is_visible_and_retriable_without_data_changes(tmp_path):
    project = project_fixture(tmp_path)
    before = protected_bytes(project)
    paths = persistent_path_hashes()
    # Two independent attempts prove the failed attempt did not leave an
    # exclusive bootstrap handle behind or turn private files into config.
    for _ in range(2):
        result = invoke(project, '-Offline', '-ForcePrivateRuntimes', '-NoStart')
        report = report_from(result)
        assert report['mode'] == 'configure'
        assert result.returncode != 0 and report['ok'] is False
        assert report.get('error', {}).get('code')
        assert protected_bytes(project) == before
        assert persistent_path_hashes() == paths


@WINDOWS
def test_actual_windows_bootstrap_lock_contention_never_removes_other_installers_lock(tmp_path):
    project = project_fixture(tmp_path)
    before = protected_bytes(project)
    runtime = project / '.runtime'
    runtime.mkdir()
    lock = runtime / 'bootstrap.lock'
    lock.write_bytes(b'EXPLICIT_SYNTHETIC_ACTIVE_INSTALLER')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    handle = create(str(lock), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    try:
        result = invoke(project, '-Offline', '-ForcePrivateRuntimes', '-NoStart')
        report = report_from(result)
        assert result.returncode != 0 and report['ok'] is False
        assert report.get('error', {}).get('code')
        assert protected_bytes(project) == before
        assert lock.exists()
    finally:
        assert close(handle)
    assert lock.read_bytes() == b'EXPLICIT_SYNTHETIC_ACTIVE_INSTALLER'


@WINDOWS
def test_actual_windows_invalid_project_is_rejected_before_any_write(tmp_path):
    project = project_fixture(tmp_path)
    (project / 'pyproject.toml').write_text('[project]\nname="EXPLICIT_SYNTHETIC_OTHER_APP"\nversion="1"\n')
    before = tree_bytes(project)
    result = invoke(project, '-Offline', '-ForcePrivateRuntimes', '-NoStart')
    report = report_from(result)
    assert result.returncode != 0 and report['ok'] is False
    assert tree_bytes(project) == before


@WINDOWS
def test_actual_windows_project_junction_is_rejected_without_external_writes(tmp_path):
    project = project_fixture(tmp_path)
    outside = tmp_path / 'outside-project'
    project.rename(outside)
    result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(project), str(outside)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    before = tree_bytes(outside)
    result = invoke(project, '-Offline', '-ForcePrivateRuntimes', '-NoStart')
    report = report_from(result)
    assert result.returncode != 0 and report['ok'] is False
    assert tree_bytes(outside) == before


@WINDOWS
@pytest.mark.parametrize('case', ['traversal', 'absolute', 'reserved', 'stream',
                                  'backslash', 'trailing_dot', 'trailing_space',
                                  'case_duplicate', 'symlink', 'wrong_prefix'])
def test_actual_windows_archive_rejects_bad_entries_before_extracting_even_good_files(tmp_path, case):
    archive = tmp_path / 'explicit-synthetic-runtime.zip'
    destination = tmp_path / 'extracted'
    cases = {
        'traversal': 'runtime/../outside.txt', 'absolute': 'C:/outside.txt',
        'reserved': 'runtime/CON.txt', 'stream': 'runtime/python.exe:stream',
        'backslash': 'runtime\\outside.txt', 'trailing_dot': 'runtime/trailing.',
        'trailing_space': 'runtime/trailing ', 'case_duplicate': 'runtime/PYTHON.EXE',
        'symlink': 'runtime/symlink', 'wrong_prefix': 'other/runtime.exe',
    }
    with zipfile.ZipFile(archive, 'w') as target:
        target.writestr('runtime/python.exe', b'EXPLICIT_SYNTHETIC_NOT_AN_EXECUTABLE')
        entry = zipfile.ZipInfo(cases[case])
        # ZipInfo.__init__ normalizes os.sep on Windows. Preserve the explicit
        # malformed ZIP name so this really exercises backslash rejection.
        entry.filename = cases[case]
        entry.orig_filename = cases[case]
        if case == 'symlink':
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
        target.writestr(entry, b'EXPLICIT_SYNTHETIC_INVALID_ENTRY')
    result = invoke_helper(tmp_path, """
try {
    SC-ExpandZip $env:SC_CONTRACT_ARCHIVE $env:SC_CONTRACT_DESTINATION 'runtime/'
    exit 77
} catch {
    [Console]::Out.WriteLine($script:SCErrorCode)
    exit 0
}
""", SC_CONTRACT_ARCHIVE=archive, SC_CONTRACT_DESTINATION=destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(code in result.stdout for code in ('archive_path', 'archive_prefix', 'unsafe_path'))
    assert not destination.exists()
    assert not (tmp_path / 'outside.txt').exists()


@WINDOWS
def test_actual_windows_safe_archive_prefix_extracts_exact_bytes_and_hashes_without_module_dependency(tmp_path):
    archive = tmp_path / 'explicit-synthetic-runtime.zip'
    destination = tmp_path / 'extracted'
    body = b'EXPLICIT_SYNTHETIC_BINARY\x00\xff\n'
    with zipfile.ZipFile(archive, 'w') as target:
        target.writestr('runtime/', b'')
        target.writestr('runtime/tools/', b'')
        target.writestr('runtime/tools/python.exe', body)
    result = invoke_helper(tmp_path, """
SC-ExpandZip $env:SC_CONTRACT_ARCHIVE $env:SC_CONTRACT_DESTINATION 'runtime/'
[Console]::Out.WriteLine((SC-HashFile (Join-Path $env:SC_CONTRACT_DESTINATION 'tools/python.exe')))
""", SC_CONTRACT_ARCHIVE=archive, SC_CONTRACT_DESTINATION=destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (destination / 'tools/python.exe').read_bytes() == body
    assert result.stdout.strip() == hashlib.sha256(body).hexdigest()


@WINDOWS
def test_actual_windows_archive_never_overwrites_a_previous_file(tmp_path):
    archive = tmp_path / 'explicit-synthetic-runtime.zip'
    destination = tmp_path / 'extracted'
    destination.mkdir()
    previous = destination / 'python.exe'
    previous.write_bytes(b'EXPLICIT_SYNTHETIC_OLD_RUNTIME')
    with zipfile.ZipFile(archive, 'w') as target:
        target.writestr('runtime/python.exe', b'EXPLICIT_SYNTHETIC_NEW_RUNTIME')
    result = invoke_helper(tmp_path, """
try {
    SC-ExpandZip $env:SC_CONTRACT_ARCHIVE $env:SC_CONTRACT_DESTINATION 'runtime/'
    exit 77
} catch { exit 0 }
""", SC_CONTRACT_ARCHIVE=archive, SC_CONTRACT_DESTINATION=destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert previous.read_bytes() == b'EXPLICIT_SYNTHETIC_OLD_RUNTIME'


@WINDOWS
def test_actual_windows_source_cmd_needs_no_preinstalled_pwsh_and_preserves_paths(tmp_path):
    project = project_fixture(tmp_path)
    launcher = project / 'one-click.cmd'
    shutil.copyfile(ROOT / '一键配置并启动.cmd', launcher)
    before = tree_bytes(project)
    args = [str(launcher), '-CheckOnly', '-ForcePrivateRuntimes', '-Json']
    command = '"' + os.environ['COMSPEC'] + '" /d /s /c "' + subprocess.list2cmdline(args) + '"'
    result = subprocess.run(command, env=dict(os.environ, SKILL_COLLECT_BOOTSTRAP_NO_PAUSE='1'),
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=50)
    report = report_from(result)
    assert result.returncode != 0 and report['ok'] is False
    assert report['mode'] == 'check'
    assert tree_bytes(project) == before


def bundle_manifest_fixture(project):
    dist = project / 'frontend/dist'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text('Explicit synthetic compiled workbench; never opened.\n')
    files = []
    for relative, body in sorted(tree_bytes(project).items()):
        if relative not in PROTECTED:
            files.append({'path': relative, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
    return {'schema_version': 1, 'product': PRODUCT, 'version': '0.2.5',
            'runtimes_bundled': False, 'files': files}


@WINDOWS
@pytest.mark.parametrize('case', ['missing_pin', 'wrong_pin', 'changed_payload', 'unsafe_path',
                                  'private_path', 'duplicate_path'])
def test_actual_windows_bundle_verification_rejects_tampering_without_writing_environment_or_data(tmp_path, case):
    project = project_fixture(tmp_path)
    manifest = bundle_manifest_fixture(project)
    if case == 'unsafe_path':
        manifest['files'].append({'path': '../outside', 'size': 0, 'sha256': '0' * 64})
    elif case == 'private_path':
        manifest['files'].append({'path': 'data/secrets.enc', 'size': 0, 'sha256': '0' * 64})
    elif case == 'duplicate_path':
        manifest['files'].append(dict(manifest['files'][0]))
    raw = json.dumps(manifest).encode()
    (project / 'bundle-manifest.json').write_bytes(raw)
    pin = hashlib.sha256(raw).hexdigest()
    if case == 'changed_payload':
        (project / 'backend/main.py').write_text('# Explicit synthetic payload corruption\n')
    if case == 'wrong_pin':
        pin = '0' * 64
    arguments = [] if case == 'missing_pin' else ['-BundleManifestSha256', pin]
    before = tree_bytes(project)
    result = invoke(project, '-CheckOnly', '-ForcePrivateRuntimes', *arguments)
    report = report_from(result)
    assert result.returncode != 0 and report['ok'] is False
    assert report['error']['code'] in ('bundle_pin', 'bundle_hash', 'bundle_payload', 'bundle_private', 'unsafe_path')
    assert tree_bytes(project) == before


@WINDOWS
def test_actual_windows_packaged_cmd_passes_pinned_manifest_to_system_powershell(tmp_path):
    project = project_fixture(tmp_path)
    manifest = bundle_manifest_fixture(project)
    raw = json.dumps(manifest).encode()
    (project / 'bundle-manifest.json').write_bytes(raw)
    pin = hashlib.sha256(raw).hexdigest()
    launcher = project / 'one-click.cmd'
    template = (ROOT / '一键配置并启动.cmd').read_text(encoding='utf-8-sig')
    assert template.count('__BUNDLE_MANIFEST_SHA256__') == 1
    launcher.write_text(template.replace('__BUNDLE_MANIFEST_SHA256__', pin), encoding='utf-8')
    before = tree_bytes(project)
    command = '"' + os.environ['COMSPEC'] + '" /d /s /c "' + subprocess.list2cmdline(
        [str(launcher), '-CheckOnly', '-ForcePrivateRuntimes', '-Json']) + '"'
    result = subprocess.run(command, env=dict(os.environ, SKILL_COLLECT_BOOTSTRAP_NO_PAUSE='1'),
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=50)
    report = report_from(result)
    assert result.returncode != 0 and report['ok'] is False
    assert report['stage'] == 'checked' and report['error']['code'] == 'environment_missing'
    assert tree_bytes(project) == before
