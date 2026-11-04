"""Distribution isolation/integrity contracts using explicitly synthetic input.

These tests do not download runtimes, run a Windows installer, or read user data.
Actual Windows bootstrap execution is covered independently.
"""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from scripts import build_windows_bundle as bundle


ROOT = Path(__file__).resolve().parents[1]


def source_fixture(tmp_path):
    source = tmp_path / 'public-source'
    for name in bundle.FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Explicit synthetic public source fixture.\n', encoding='utf-8')
    (source / 'backend/__init__.py').write_text("__version__ = '0.2.4'\n")
    # Empty Python package initializers are valid public source files.
    (source / 'backend/connectors/__init__.py').write_bytes(b'')
    (source / 'pyproject.toml').write_text('[project]\nname="liming-zhiyao-skill-collect"\nversion="0.2.4"\n')
    (source / 'uv.lock').write_text('version=1\n[[package]]\nname="liming-zhiyao-skill-collect"\nversion="0.2.4"\n')
    package = {'name': bundle.PRODUCT + '-workbench', 'version': '0.2.4'}
    (source / 'frontend/package.json').write_text(json.dumps(package))
    (source / 'frontend/package-lock.json').write_text(json.dumps({**package, 'packages': {'': package}}))
    for name in bundle.LAUNCHERS:
        shutil.copyfile(ROOT / name, source / name)
    dist = source / 'frontend/dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'assets/index-fixture.js').write_text('/* synthetic compiled JavaScript */\n')
    (dist / 'assets/index-fixture.css').write_text('/* synthetic CSS */\n')
    (dist / 'index.html').write_text('<script src="/assets/index-fixture.js"></script><link href="/assets/index-fixture.css">')
    return source


def test_bundle_manifest_and_launchers_cover_only_public_payload(tmp_path):
    source = source_fixture(tmp_path)
    sentinel = b'EXPLICIT_SYNTHETIC_PRIVATE_SENTINEL'
    for name in ('data/app.sqlite3', 'data/.secrets.key', 'data/secrets.enc', '.env',
                 '.runtime/python/python.exe', '.venv/Scripts/python.exe', 'uploads/private.mp4',
                 'tasks/private.txt', 'artifacts/private/probe.json', '.git/config',
                 'frontend/node_modules/private.js', 'backend/unknown-private.py'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sentinel)
    output = tmp_path / 'Windows.zip'
    report = bundle.build_bundle(source, output)
    prefix = 'SkillCollect-v0.2.4-Windows/'
    expected_payload = set(bundle.FILES) | {'frontend/dist/index.html',
        'frontend/dist/assets/index-fixture.js', 'frontend/dist/assets/index-fixture.css'}
    assert {'AGENTS.md', 'docs/用户使用说明.md', 'docs/验收报告.md', 'docs/实施进度.md',
        'docs/GitHub接入.md', 'docs/飞书接入.md', 'docs/MiniMax接入.md',
        'docs/Windows一键安装.md', 'docs/客户升级与抖音验收.md',
        'docs/安装与数据.md', 'docs/技术决策.md', 'docs/抖音原片获取与分析.md'} <= expected_payload
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {prefix + name for name in expected_payload | set(bundle.LAUNCHERS) | {bundle.MANIFEST}}
        assert all(sentinel not in archive.read(name) for name in archive.namelist())
        raw_manifest = archive.read(prefix + bundle.MANIFEST)
        manifest = json.loads(raw_manifest)
        assert manifest['product'] == bundle.PRODUCT and manifest['version'] == '0.2.4'
        assert not manifest['runtimes_bundled']
        assert {entry['path'] for entry in manifest['files']} == expected_payload
        assert hashlib.sha256(raw_manifest).hexdigest() == report['manifest_sha256']
        for entry in manifest['files']:
            body = archive.read(prefix + entry['path'])
            assert len(body) == entry['size']
            assert hashlib.sha256(body).hexdigest() == entry['sha256']
        for name in bundle.LAUNCHERS:
            body = archive.read(prefix + name)
            assert report['manifest_sha256'].encode() in body
            assert b'__BUNDLE_MANIFEST_SHA256__' not in body
            assert b'WindowsPowerShell' in body and b'pwsh.exe' not in body
            assert b'\r\n' in body
            assert report['launcher_sha256'][name] == hashlib.sha256(body).hexdigest()
        assert b'-SelectDataDirectory' in archive.read(prefix + bundle.DATA_LAUNCHER)
        assert b'-SelectDataDirectory' not in archive.read(prefix + bundle.LAUNCHER)
    assert report['payload_files'] == len(expected_payload)
    assert report['archive_files'] == len(expected_payload) + 3
    assert report['size'] == output.stat().st_size
    assert report['sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert output.with_name(output.name + '.sha256').read_text() == report['sha256'] + '  Windows.zip\n'
    with pytest.raises(bundle.BundleBuildError, match='already exists'):
        bundle.build_bundle(source, output)


def test_bundle_is_deterministic_across_output_names(tmp_path):
    source = source_fixture(tmp_path)
    first = tmp_path / 'first.zip'
    second = tmp_path / 'second.zip'
    a = bundle.build_bundle(source, first)
    b = bundle.build_bundle(source, second)
    assert first.read_bytes() == second.read_bytes()
    assert a['sha256'] == b['sha256']


@pytest.mark.parametrize('path', [
    '../backend/main.py', '/backend/main.py', 'backend/../data/app.sqlite3',
    'backend\\main.py', 'C:/backend/main.py', 'backend/main.py:stream',
    'backend/main.py ', 'backend/main.py.', 'frontend/dist/assets/NUL.js',
    'frontend/dist/assets/COM1.css', 'frontend/dist/assets/CON.js',
    'frontend/dist/assets/../main.js', 'frontend/dist/secrets.enc',
    'frontend/dist/assets/private/file.js', 'backend/unknown-private.py',
    'data/app.sqlite3', '.secrets.key', '.env', '.runtime/python/python.exe',
    '.venv/Scripts/python.exe', 'frontend/node_modules/library.js', '.git/config',
])
def test_bundle_allowlist_rejects_private_and_unsafe_paths(path):
    assert not bundle.allowed_path(path)


@pytest.mark.parametrize('change', ['version', 'extra_asset', 'missing_asset',
    'private_dist', 'external_asset', 'data_asset', 'asset_case_mismatch', 'missing_input',
    'bad_launcher', 'credential'])
def test_bundle_rejects_invalid_input_without_partial_output(tmp_path, change):
    source = source_fixture(tmp_path)
    if change == 'version':
        (source / 'frontend/package.json').write_text('{"version":"0.2.3"}')
    elif change == 'extra_asset':
        (source / 'frontend/dist/assets/unknown.js').write_text('/* unreferenced */')
    elif change == 'missing_asset':
        (source / 'frontend/dist/assets/index-fixture.js').unlink()
    elif change == 'private_dist':
        (source / 'frontend/dist/secrets.enc').write_bytes(b'synthetic private')
    elif change == 'external_asset':
        (source / 'frontend/dist/index.html').write_text('<script src="https://fixture.invalid/x.js"></script>')
    elif change == 'data_asset':
        (source / 'frontend/dist/index.html').write_text('<script src="data:text/javascript,fixture"></script>')
    elif change == 'asset_case_mismatch':
        # Renaming makes this contract meaningful on case-insensitive hosts too.
        (source / 'frontend/dist/assets/index-fixture.js').rename(source / 'frontend/dist/assets/INDEX-FIXTURE.js')
    elif change == 'missing_input':
        (source / 'backend/main.py').unlink()
    elif change == 'bad_launcher':
        (source / bundle.LAUNCHER).write_text('fixture launcher without a manifest pin')
    else:
        # Construct the synthetic token so this test's source is not itself a
        # credential-pattern finding in the publication scanner.
        (source / 'backend/models.py').write_bytes(b'ghp_' + b'A' * 36)
    output = tmp_path / 'invalid.zip'
    with pytest.raises(bundle.BundleBuildError):
        bundle.build_bundle(source, output)
    assert not output.exists()
    assert not list(tmp_path.glob('*.partial'))


def test_bundle_refuses_existing_checksum_without_overwriting_it(tmp_path):
    source = source_fixture(tmp_path)
    output = tmp_path / 'occupied.zip'
    checksum = output.with_name(output.name + '.sha256')
    checksum.write_bytes(b'previous checksum must remain unchanged')
    with pytest.raises(bundle.BundleBuildError, match='Checksum output already exists'):
        bundle.build_bundle(source, output)
    assert not output.exists()
    assert checksum.read_bytes() == b'previous checksum must remain unchanged'


def test_bundle_rejects_links_and_size_budget(tmp_path, monkeypatch):
    source = source_fixture(tmp_path)
    file = source / 'backend/models.py'
    outside = tmp_path / 'outside-private'
    outside.write_text('explicit synthetic private target')
    file.unlink()
    try:
        file.symlink_to(outside)
    except OSError:
        pytest.skip('Symlink creation may require Windows developer mode')
    with pytest.raises(bundle.BundleBuildError, match='Links/reparse'):
        bundle.build_bundle(source, tmp_path / 'linked.zip')
    assert outside.read_text() == 'explicit synthetic private target'
    file.unlink()
    file.write_text('explicit public fixture')
    monkeypatch.setattr(bundle, 'MAX_TOTAL', 10)
    with pytest.raises(bundle.BundleBuildError, match='budget'):
        bundle.build_bundle(source, tmp_path / 'over-budget.zip')


def test_bundle_rejects_dead_checksum_symlink(tmp_path):
    source = source_fixture(tmp_path)
    output = tmp_path / 'dead-link.zip'
    outside = tmp_path / 'must-not-be-created'
    try:
        output.with_name(output.name + '.sha256').symlink_to(outside)
    except OSError:
        pytest.skip('Symlink creation may require Windows developer mode')
    with pytest.raises(bundle.BundleBuildError, match='Links/reparse'):
        bundle.build_bundle(source, output)
    assert not outside.exists()
    assert not output.exists()


def test_bundle_cleans_its_partial_outputs_after_a_write_failure(tmp_path, monkeypatch):
    source = source_fixture(tmp_path)
    output = tmp_path / 'interrupted.zip'

    def interrupted_copy(source_file, destination):
        destination.write(source_file.read(32))
        raise OSError('Explicit synthetic storage failure')

    monkeypatch.setattr(bundle.shutil, 'copyfileobj', interrupted_copy)
    with pytest.raises(OSError, match='synthetic storage failure'):
        bundle.build_bundle(source, output)
    assert not output.exists()
    assert not output.with_name(output.name + '.sha256').exists()
    assert not list(tmp_path.glob('*.partial'))


def test_bundle_preserves_an_output_created_after_its_initial_check(tmp_path, monkeypatch):
    source = source_fixture(tmp_path)
    output = tmp_path / 'raced.zip'
    original_open = Path.open
    sentinel = b'Explicit synthetic concurrent publisher output'

    def raced_open(path, mode='r', *args, **kwargs):
        if path == output and mode == 'xb':
            with original_open(path, 'wb') as created_by_other_writer:
                created_by_other_writer.write(sentinel)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', raced_open)
    with pytest.raises(FileExistsError):
        bundle.build_bundle(source, output)
    assert output.read_bytes() == sentinel
    assert not output.with_name(output.name + '.sha256').exists()
    assert not list(tmp_path.glob('*.partial'))


def test_bundle_cleans_partial_zip_when_archive_writing_fails(tmp_path, monkeypatch):
    source = source_fixture(tmp_path)
    output = tmp_path / 'zip-storage-failure.zip'
    original_write = zipfile.ZipFile.writestr

    def interrupted_zip(archive, entry, body, *args, **kwargs):
        original_write(archive, entry, body, *args, **kwargs)
        raise OSError('Explicit synthetic ZIP storage failure')

    monkeypatch.setattr(zipfile.ZipFile, 'writestr', interrupted_zip)
    with pytest.raises(OSError, match='ZIP storage failure'):
        bundle.build_bundle(source, output)
    assert not output.exists()
    assert not output.with_name(output.name + '.sha256').exists()
    assert not list(tmp_path.glob('*.partial'))
