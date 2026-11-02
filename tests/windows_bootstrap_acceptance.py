"""Windows CI acceptance after a real private-runtime production installation.

All data is explicitly synthetic. Only loopback HTTP is exercised here; this is
not Douyin retrieval, model analysis, or Feishu business acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def persistent_paths():
    import winreg
    values = []
    for hive, key in ((winreg.HKEY_CURRENT_USER, 'Environment'),
                      (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value = winreg.QueryValueEx(handle, 'Path')[0]
        except FileNotFoundError:
            value = ''
        values.append(digest(value.encode('utf-8')))
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--path-baseline', type=Path, required=True)
    args = parser.parse_args()
    require(os.name == 'nt', 'Windows execution is required.')
    project = args.project.resolve()
    os.chdir(project)
    sys.path.insert(0, str(project))
    from backend import __version__
    from backend.security import Vault
    from backend.store import Store

    shell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    script = str(project / 'scripts/windows_bootstrap.ps1')
    environment = json.loads((project / '.runtime/environment.json').read_text(encoding='utf-8-sig'))
    lock = json.loads((project / 'scripts/windows-runtime-lock.json').read_text())
    download_checks = {}
    for name, entry in lock['runtimes'].items():
        archive = project / '.runtime/downloads' / (entry['sha256'] + '.zip')
        require(archive.is_file(), 'A pinned runtime archive was not actually downloaded.')
        require(digest(archive.read_bytes()) == entry['sha256'], 'A downloaded runtime hash does not match its lock.')
        executable = Path(environment[name])
        require(executable.is_file() and executable.is_relative_to(project / '.runtime'), 'A private runtime executable is unavailable.')
        download_checks[name] = {'version': entry['version'], 'sha256_verified': True, 'private_executable_exists': True}

    baseline = json.loads(args.path_baseline.read_text(encoding='utf-8-sig'))
    require(persistent_paths() == baseline, 'Cold installation modified a persistent PATH.')
    data = project / 'data'
    store = Store(data)
    vault = Vault(data)
    key = 'EXPLICIT_SYNTHETIC_LOCAL_KEY_NO_EXTERNAL_CALLS'
    vault.set('model_text', key)
    store.put('model_configs', {'id': 'text', 'base_url': 'https://example.invalid/v1', 'model': 'explicit-synthetic-no-call-model'})
    store.put('knowledge', {'id': 'bootstrap-preservation', 'title': 'Explicit synthetic preservation record', 'body': 'No external source or model.'})
    for relative in ('uploads/synthetic.txt', 'tasks/synthetic/source.txt'):
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('EXPLICIT_SYNTHETIC_PRESERVED_DATA', encoding='utf-8')
    (project / '.env').write_text('EXPLICIT_SYNTHETIC_NOT_AUTO_LOADED=1\n', encoding='utf-8')
    before_rows = digest(json.dumps(store.list('model_configs') + store.list('knowledge'), sort_keys=True).encode())
    before_files = {str(path.relative_to(project)): path.read_bytes() for path in (
        data / '.secrets.key', data / 'secrets.enc', data / 'uploads/synthetic.txt',
        data / 'tasks/synthetic/source.txt', project / '.env')}
    original_managed = {name: (project / '.runtime' / name / '.skill-collect-managed.json').read_bytes()
                        for name in lock['runtimes']}
    env = dict(os.environ, PYTHONUTF8='1', PYTHONNOUSERSITE='1',
               PLAYWRIGHT_BROWSERS_PATH=environment['browser_cache'], SKILL_COLLECT_DATA=str(data))
    dirs = [str(Path(environment[name]).parent) for name in ('python', 'node', 'ffmpeg', 'ffprobe')]
    env['PATH'] = os.pathsep.join(dict.fromkeys(dirs)) + os.pathsep + env['PATH']
    for _ in range(2):
        result = subprocess.run([shell, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script,
                                 '-ForcePrivateRuntimes', '-Offline', '-NoStart', '-Json'],
                                env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        require(result.returncode == 0, 'Offline reconfiguration failed; inspect fixed bootstrap stage locally.')
        report = json.loads(result.stdout)
        require(report['ok'] is True and report['mode'] == 'configure', 'Offline reconfiguration did not report readiness.')
        require(all(check['ok'] is True for check in report['checks']), 'An environment check did not pass after installation.')
    require(all((project / relative).read_bytes() == body for relative, body in before_files.items()), 'Reconfiguration changed a protected synthetic file.')
    require(all((project / '.runtime' / name / '.skill-collect-managed.json').read_bytes() == body
                for name, body in original_managed.items()), 'Offline replay replaced a runtime unnecessarily.')
    require(digest(json.dumps(store.list('model_configs') + store.list('knowledge'), sort_keys=True).encode()) == before_rows,
            'Reconfiguration changed stored synthetic records.')
    require(Vault(data).get('model_text') == key, 'An encrypted model key was not retained.')

    doctor = subprocess.run([sys.executable, 'scripts/doctor.py', '--json'], env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    require(doctor.returncode == 0 and json.loads(doctor.stdout)['ok'] is True, 'Read-only doctor did not pass after installation.')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    env['SKILL_COLLECT_PORT'] = str(port)
    # The real customer launcher starts the actual application. This process
    # owns the entire tree it terminates; no unrelated PID is ever stopped.
    process = subprocess.Popen([shell, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script,
                                '-ForcePrivateRuntimes', '-Offline', '-NoBrowser'], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    status = None
    try:
        deadline = time.monotonic() + 60
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            require(process.poll() is None, 'The actual bootstrap exited before its local application was ready.')
            try:
                with opener.open(f'http://127.0.0.1:{port}/api/status', timeout=2) as response:
                    status = json.load(response)
                break
            except (OSError, urllib.error.URLError, ValueError):
                time.sleep(.5)
        require(status is not None, 'The application did not start through the real bootstrap.')
        require(status.get('version') == __version__ and status.get('mode') == 'local_single_user', 'The wrong local application answered.')
        require(status['models']['text']['key_configured'] is True, 'Stored synthetic model configuration was not visible to the app.')
        require(status['counts']['knowledge'] == 1, 'Stored synthetic knowledge was not visible to the app.')
        with opener.open(f'http://127.0.0.1:{port}/', timeout=3) as response:
            require(response.status == 200 and b'/assets/' in response.read(), 'The compiled workbench was not served.')
        # A second click reuses the same service and never creates a duplicate.
        second = subprocess.run([shell, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script,
                                 '-Offline', '-NoStart', '-Json'], env=env, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=30)
        require(second.returncode == 0 and json.loads(second.stdout)['stage'] == 'already_running', 'A second click did not reuse the owned local service.')
        require(process.poll() is None, 'A second click terminated the first service.')
    finally:
        if process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=15)
        process.wait(timeout=15)

    require(persistent_paths() == baseline, 'Bootstrap changed persistent system/user PATH.')
    require(all((project / relative).read_bytes() == body for relative, body in before_files.items()), 'Startup changed a protected synthetic file.')
    require(digest(json.dumps(store.list('model_configs') + store.list('knowledge'), sort_keys=True).encode()) == before_rows,
            'Startup changed stored synthetic records.')
    public = {
        'schema_version': 1, 'application_version': __version__, 'platform': 'Windows x64',
        'bootstrap_shell': 'Windows PowerShell 5.1', 'runtimes': download_checks,
        'production_dependencies_installed': True, 'offline_reconfiguration_runs': 2,
        'encrypted_key_bytes_preserved': True, 'stored_model_and_knowledge_preserved': True,
        'synthetic_media_and_env_preserved': True, 'persistent_path_unchanged': True,
        'actual_loopback_http_startup': True, 'compiled_workbench_served': True,
        'second_click_reused_owned_service': True,
        'external_model_calls': 0, 'douyin_source_calls': 0, 'feishu_calls': 0,
        'scope': 'Real dependency installation and local runtime startup with explicit synthetic data; not customer Win11 or Douyin business acceptance.',
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(public, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(public))


if __name__ == '__main__':
    main()
