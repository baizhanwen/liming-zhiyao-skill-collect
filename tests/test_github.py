"""GitHub API contract tests use explicit MockTransport; no live credentials."""
import base64
import hashlib
import io
import json
import zipfile

import httpx
import pytest

from backend.connectors.github import GitHubConnector, GithubError

SHA = 'a' * 40
OTHER = 'b' * 40
FILES = {
    'README.md': b'# Contract repository\nProject self-description.',
    'LICENSE': b'MIT License - contract fixture',
    'package.json': b'{"name":"contract","scripts":{"start":"do-not-execute"}}',
    'skills/first/SKILL.md': b'---\nname: first\ndescription: First contract skill\n---\n# Instructions\n',
    'skills/first/scripts/run.py': b'print("never executed by import")\n',
    'skills/first/templates/input.txt': b'source template\n',
    'skills/second/SKILL.md': b'---\nname: second\ndescription: Second contract skill\n---\n# Second\n',
}


def blob_sha(raw):
    return hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()


def connector(*, token='', extra_tree=None, truncated=False, fail=None):
    requests = []
    tree = [{'path': path, 'type': 'blob', 'mode': '100644', 'sha': blob_sha(raw), 'size': len(raw)}
            for path, raw in FILES.items()]
    tree += extra_tree or []
    repo = {'default_branch': 'main', 'license': {'spdx_id': 'MIT'}, 'stargazers_count': 2,
            'description': 'Contract fixture', 'updated_at': '2026-10-26T00:00:00Z'}

    def respond(request):
        requests.append(request)
        p = request.url.path
        if fail:
            return fail(request)
        if p == '/repos/owner/repo':
            return httpx.Response(200, json=repo)
        if p.startswith('/repos/owner/repo/commits/'):
            ref = p.split('/commits/', 1)[1]
            if ref in ('main', SHA, 'feature/work'):
                return httpx.Response(200, json={'sha': SHA})
            return httpx.Response(404, json={})
        if p == f'/repos/owner/repo/git/trees/{SHA}':
            return httpx.Response(200, json={'tree': tree, 'truncated': truncated})
        if p.startswith('/repos/owner/repo/git/blobs/'):
            sha = p.rsplit('/', 1)[1]
            raw = next((r for r in FILES.values() if blob_sha(r) == sha), b'corrupt')
            return httpx.Response(200, json={'encoding': 'base64', 'content': base64.b64encode(raw).decode()})
        if p == '/search/repositories':
            return httpx.Response(200, json={'total_count': 1, 'items': [{'full_name': 'owner/repo',
                'html_url': 'https://github.com/owner/repo', **repo}]})
        if p == '/search/code':
            return httpx.Response(200, json={'items': [{'name': 'SKILL.md', 'path': 'skills/first/SKILL.md',
                'html_url': f'https://github.com/owner/repo/blob/{SHA}/skills/first/SKILL.md',
                'repository': {'full_name': 'owner/repo'}}]})
        return httpx.Response(404, json={})

    return GitHubConnector(token, client=httpx.Client(transport=httpx.MockTransport(respond))), requests


@pytest.mark.parametrize('url', ['http://github.com/owner/repo', 'https://github.com.evil.test/owner/repo',
    'https://localhost/owner/repo', 'file:///etc/passwd', 'https://user:pass@github.com/owner/repo',
    'https://github.com/owner/repo/tree/main/../bad', 'https://github.com/owner/repo/issues'])
def test_reject_unsafe_urls(url):
    c, calls = connector()
    with pytest.raises(GithubError, match='GitHub|路径|链接'):
        c.normalize(url)
    assert not calls


def test_slash_ref_normalized_to_commit():
    c, _ = connector()
    n = c.normalize('https://github.com/owner/repo/tree/feature/work/skills/first')
    assert n['ref'] == 'feature/work'
    assert n['path'] == 'skills/first'
    assert n['version'] == SHA


def test_snapshot_fixed_evidence_and_real_files():
    c, calls = connector()
    result = c.snapshot('https://github.com/owner/repo')
    assert result['version'] == SHA
    assert result['metadata']['execution_verified'] is False
    assert any(f['path'] == 'README.md' for f in result['files'])
    assert any(e['kind'] == 'code_observation' for e in result['evidence'])
    assert all('/blob/' + SHA + '/' in e['url'] for e in result['evidence'])
    assert all(r.method == 'GET' and r.url.host == 'api.github.com' for r in calls)


def test_target_file_scope():
    c, _ = connector()
    result = c.snapshot('https://github.com/owner/repo/blob/main/package.json')
    assert [f['path'] for f in result['files']] == ['package.json']


def test_discover_multiple_skills_and_related_files():
    c, _ = connector()
    result = c.discover('https://github.com/owner/repo')
    assert result['total_found'] == 2
    assert {s['name'] for s in result['skills']} == {'first', 'second'}
    first = result['skills'][0]
    assert any(f['path'].endswith('scripts/run.py') for f in first['files'])
    assert first['license'] == 'MIT'
    assert first['verification_status'] == 'not_executed'


def test_search_no_token_is_real_repository_search_only():
    c, calls = connector()
    result = c.search('transcript')
    assert result['items'][0]['repository'] == 'owner/repo'
    assert result['code_search_available'] is False
    assert result['notes']
    assert not any(r.url.path == '/search/code' for r in calls)


def test_authenticated_code_search():
    c, calls = connector(token='contract-only')
    result = c.search('transcript')
    assert result['code_search_available']
    assert result['items'][1]['path'] == 'skills/first/SKILL.md'
    assert all(r.headers['Authorization'] == 'Bearer contract-only' for r in calls)


def test_package_preserves_all_files_and_license():
    c, _ = connector()
    raw = c.skill_package('https://github.com/owner/repo', 'skills/first/SKILL.md')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert z.read('first/scripts/run.py') == FILES['skills/first/scripts/run.py']
        assert z.read('first/LICENSE') == FILES['LICENSE']
        manifest = json.loads(z.read('first/manifest.json'))
        assert manifest['source']['version'] == SHA
        assert manifest['verification_status'] == 'not_executed'
        for f in manifest['files']:
            assert hashlib.sha256(z.read('first/' + f['path'])).hexdigest() == f['sha256']
        assert not any('second' in n for n in z.namelist())


@pytest.mark.parametrize('mode,type_', [('120000', 'blob'), ('160000', 'commit')])
def test_package_rejects_symlink_and_submodule(mode, type_):
    c, _ = connector(extra_tree=[{'path': 'skills/first/unsafe', 'mode': mode, 'type': type_, 'sha': OTHER, 'size': 10}])
    with pytest.raises(GithubError, match='符号链接|子模块'):
        c.skill_package('https://github.com/owner/repo', 'skills/first/SKILL.md')


def test_truncated_tree_cannot_pretend_complete_package():
    c, _ = connector(truncated=True)
    assert c.discover('https://github.com/owner/repo')['truncated']
    with pytest.raises(GithubError, match='截断'):
        c.skill_package('https://github.com/owner/repo', 'skills/first/SKILL.md')


@pytest.mark.parametrize('status,code,retryable', [(401, 'authentication', False), (404, 'not_found', False),
    (429, 'rate_limit', True), (503, 'upstream', True)])
def test_errors_are_structured_and_do_not_echo_secret(status, code, retryable):
    c, _ = connector(fail=lambda r: httpx.Response(status, json={'message': 'never echo secret contract-only'}))
    with pytest.raises(GithubError) as error:
        c.search('x')
    assert error.value.code == code
    assert error.value.retryable == retryable
    assert 'contract-only' not in str(error.value)


def test_blob_integrity_rejected():
    c, _ = connector(extra_tree=[{'path': 'skills/first/corrupt.txt', 'mode': '100644', 'type': 'blob', 'sha': OTHER, 'size': 7}])
    with pytest.raises(GithubError, match='哈希'):
        c.skill_package('https://github.com/owner/repo', 'skills/first/SKILL.md')


def test_connector_package_import_export_roundtrip_and_deduplication(tmp_path):
    from backend.skills import SkillService, unpack
    from backend.store import Store
    c, _ = connector()
    raw = c.skill_package('https://github.com/owner/repo', 'skills/first/SKILL.md')
    service = SkillService(Store(tmp_path / 'data'))
    first = service.import_zip(raw)
    repeated = service.import_zip(raw)
    assert first['id'] == repeated['id']
    assert first['version'] == repeated['version'] == 1
    assert len(service.store.list('skills')) == 1
    assert first['status'] == 'draft'
    original_files, _ = unpack(raw)
    exported_files, _ = unpack(service.export_zip(first['id']))
    assert exported_files == original_files
    assert 'LICENSE' in exported_files
    assert first['source']['repository'] == 'owner/repo'
    assert first['source']['version'] == SHA


def test_snapshot_rate_limit_uses_explicit_anonymous_fallback(monkeypatch):
    c, _ = connector(fail=lambda r: httpx.Response(403, headers={'x-ratelimit-remaining': '0'}, json={}))
    calls = []
    monkeypatch.setattr(c, '_snapshot_public_git', lambda url, depth, reason: calls.append((url, depth, reason)) or {'transport': 'fixture-public-git'})
    assert c.snapshot('https://github.com/owner/repo', 'deep')['transport'] == 'fixture-public-git'
    assert calls == [('https://github.com/owner/repo', 'deep', 'rate_limit')]


@pytest.mark.parametrize('status,headers,token', [(401, {}, ''), (403, {}, ''), (404, {}, ''),
    (403, {'x-ratelimit-remaining': '0'}, 'configured-token')])
def test_snapshot_never_bypasses_auth_permission_or_configured_token(monkeypatch, status, headers, token):
    c, _ = connector(token=token, fail=lambda r: httpx.Response(status, headers=headers, json={}))
    monkeypatch.setattr(c, '_snapshot_public_git', lambda *args: pytest.fail('anonymous fallback must not run'))
    with pytest.raises(GithubError):
        c.snapshot('https://github.com/owner/repo')


def test_public_git_environment_blocks_inherited_credentials_and_config(tmp_path, monkeypatch):
    from backend.connectors.github import _PublicGit
    monkeypatch.setenv('GIT_CONFIG_PARAMETERS', "'credential.helper=malicious-helper'")
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'http.extraHeader')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'Authorization: fixture-private')
    monkeypatch.setenv('GH_TOKEN', 'fixture-gh-token')
    monkeypatch.setenv('GIT_ASKPASS', '/malicious-helper')
    reader = _PublicGit(tmp_path, 'owner/repo')
    assert reader.url == 'https://github.com/owner/repo.git'
    assert not any(key in reader.env for key in ('GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GH_TOKEN'))
    assert reader.env['GIT_TERMINAL_PROMPT'] == '0'
    assert reader.env['GIT_CONFIG_NOSYSTEM'] == '1'
    assert reader.env['GIT_NO_LAZY_FETCH'] == '1'
    assert reader.env['GIT_ASKPASS'] == ''
    for config in ('credential.helper=', 'http.followRedirects=false', 'protocol.file.allow=never',
                   'protocol.ext.allow=never', 'submodule.recurse=false', 'filter.lfs.process=',
                   'remote.https://github.com/owner/repo.git.promisor=false'):
        assert config in reader.command


def test_public_git_limits_output_disk_and_process_time(tmp_path):
    import sys
    import time
    from backend.connectors.github import _PublicGit
    reader = _PublicGit(tmp_path, 'owner/repo')
    reader.command = [sys.executable, '-c', 'import sys; sys.stdout.write("x" * 1000)']
    with pytest.raises(GithubError, match='输出超过'):
        reader.run([], limit=10)
    reader.MAX_DISK = 10
    (tmp_path / 'too-big').write_bytes(b'x' * 11)
    with pytest.raises(GithubError, match='安全上限'):
        reader._check_disk()
    (tmp_path / 'too-big').unlink()
    reader.MAX_DISK = 128 * 1024 * 1024
    reader.command = [sys.executable, '-c', 'import time; time.sleep(5)']
    reader.deadline = time.monotonic() + 0.05
    with pytest.raises(GithubError, match='75 秒'):
        reader.run([])


@pytest.fixture
def public_git_objects(tmp_path):
    """Real local Git objects; production transport still only allows public HTTPS."""
    import os
    from pathlib import Path
    import shutil
    import subprocess
    git = shutil.which('git')
    if not git:
        pytest.skip('Git runtime unavailable for local object-reader contract')
    root = tmp_path / 'fixture.git'
    empty = tmp_path / 'fixture-empty'
    empty.mkdir()
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_AUTHOR_NAME': 'Contract', 'GIT_AUTHOR_EMAIL': 'contract@example.invalid',
           'GIT_COMMITTER_NAME': 'Contract', 'GIT_COMMITTER_EMAIL': 'contract@example.invalid'}
    def run(args, input=None):
        return subprocess.run([git, '-c', 'core.hooksPath=' + str(empty), '-c', 'commit.gpgsign=false',
                               '--git-dir=' + str(root), *args], input=input, capture_output=True, env=env, check=True).stdout
    run(['init', '--quiet', '--bare', '--template=' + str(empty), str(root)])
    data = {'README.md': b'# Read-only Git fixture\n', 'LICENSE': b'MIT fixture',
            'package.json': b'{"name":"fixture"}', 'entry.py': b'print("never executed")\n',
            'large.md': b'x' * (512 * 1024 + 1)}
    lines = []
    for name, raw in data.items():
        oid = run(['hash-object', '-w', '--stdin'], raw).decode().strip()
        lines.append(f'100644 blob {oid}\t{name}\n')
    oid = run(['hash-object', '-w', '--stdin'], b'../outside').decode().strip()
    lines.append(f'120000 blob {oid}\tREADME-link.md\n')
    tree = run(['mktree'], ''.join(lines).encode()).decode().strip()
    sha = run(['commit-tree', tree], b'Explicit local Git contract\n').decode().strip()
    run(['update-ref', 'refs/heads/main', sha])
    run(['update-ref', 'refs/heads/feature/work', sha])
    return root, sha, data


def test_public_git_actual_object_reader_fixed_commit_slash_ref_blob_scope(tmp_path, monkeypatch, public_git_objects):
    import shutil
    from backend.connectors.github import _PublicGit
    fixture, sha, files = public_git_objects
    original_run = _PublicGit.run
    commands = []
    def fixture_transport(reader, args, **kwargs):
        commands.append(args)
        if args[0] == 'ls-remote':
            return (f'ref: refs/heads/main\tHEAD\n{sha}\tHEAD\n{sha}\trefs/heads/feature/work\n').encode()
        if 'fetch' in args:
            assert reader.url == 'https://github.com/owner/repo.git'
            assert '--filter=blob:limit=524288' in args
            shutil.copytree(fixture, reader.repo, dirs_exist_ok=True)
            (reader.repo / 'FETCH_HEAD').write_text(sha + '\n')
            return b''
        return original_run(reader, args, **kwargs)
    monkeypatch.setattr(_PublicGit, 'run', fixture_transport)
    c, _ = connector(fail=lambda r: httpx.Response(403, headers={'x-ratelimit-remaining': '0'}, json={}))
    result = c.snapshot('https://github.com/owner/repo/tree/feature/work', 'deep')
    assert result['version'] == sha
    assert result['metadata']['transport'] == 'public_git'
    assert result['metadata']['fallback_reason'] == 'rate_limit'
    assert result['metadata']['execution_verified'] is False
    actual = {f['path']: f['content'] for f in result['files']}
    assert actual['README.md'] == files['README.md'].decode()
    assert actual['entry.py'] == files['entry.py'].decode()
    assert 'large.md' not in actual and 'README-link.md' not in actual
    assert all('/blob/' + sha + '/' in e['url'] for e in result['evidence'])
    single = c.snapshot('https://github.com/owner/repo/blob/feature/work/package.json')
    assert [f['path'] for f in single['files']] == ['package.json']
    fixed = c.snapshot('https://github.com/owner/repo/blob/' + sha + '/README.md')
    assert fixed['files'][0]['content'] == files['README.md'].decode()
    assert not any('checkout' in a or 'submodule' in a or 'clone' in a for a in commands)


def test_public_git_missing_blob_does_not_lazy_fetch(tmp_path, monkeypatch):
    from backend.connectors.github import _PublicGit
    reader = _PublicGit(tmp_path, 'owner/repo')
    calls = []
    monkeypatch.setattr(reader, 'read', lambda args, **kwargs: calls.append((args, kwargs)) or None)
    assert reader.blob({'sha': 'a' * 40}, 512 * 1024) is None
    assert calls == [(['cat-file', '-s', 'a' * 40], {'limit': 128, 'missing': True})]


def test_public_git_refuses_existing_netrc_without_reading_it(tmp_path, monkeypatch):
    from backend.connectors.github import _PublicGit
    monkeypatch.setattr(_PublicGit, '_has_netrc', staticmethod(lambda: True))
    with pytest.raises(GithubError, match='netrc'):
        _PublicGit(tmp_path, 'owner/repo')
