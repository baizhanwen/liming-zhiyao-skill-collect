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
