"""Privacy-limited GitHub progress contracts; all HTTP/Git readers are explicit mocks."""
import base64
import hashlib
import json

import httpx
import pytest

import backend.connectors.github as github
from backend.connectors.github import GitHubConnector, GithubError
from backend.security import AppError

SHA = 'a' * 40
URL = 'https://github.com/private_contract_owner/private_contract_repo'
RAW = {'README.md': b'Private contract source title', 'LICENSE': b'Private contract license',
       'entry.py': b'print("private source code, never executed")', 'binary.md': b'\x00not text'}


def blob_sha(raw):
    return hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()


def connector(*, files=None, fail=None, token='', truncated=False):
    files = RAW if files is None else files
    calls = []

    def reply(request):
        calls.append((request.method, request.url.path, str(request.url.query)))
        if fail:
            return fail(request)
        if request.url.path.endswith('/commits/main'):
            return httpx.Response(200, json={'sha': SHA})
        if '/git/trees/' in request.url.path:
            return httpx.Response(200, json={'tree': [
                {'path': path, 'sha': blob_sha(raw), 'size': len(raw), 'type': 'blob', 'mode': '100644'}
                for path, raw in files.items()], 'truncated': truncated})
        if '/git/blobs/' in request.url.path:
            raw = next(raw for raw in files.values() if blob_sha(raw) == request.url.path.rsplit('/', 1)[1])
            return httpx.Response(200, json={'encoding': 'base64', 'content': base64.b64encode(raw).decode()})
        return httpx.Response(200, json={'default_branch': 'main', 'description': 'Private contract description'})

    return GitHubConnector(token, client=httpx.Client(transport=httpx.MockTransport(reply))), calls


def test_real_returned_candidate_counts_do_not_treat_none_as_read_or_change_source():
    baseline, baseline_calls = connector()
    expected = baseline.snapshot(URL, 'deep')
    observed, calls = connector()
    events = []
    observed.set_progress_callback(events.append)
    result = observed.snapshot(URL, 'deep')
    assert result == expected and calls == baseline_calls
    assert [e['phase'] for e in events[:4]] == ['github_metadata', 'github_metadata', 'github_tree', 'github_tree']
    assert [(e['completed'], e['total']) for e in events[:4]] == [(0, 1), (1, 1), (0, 1), (1, 1)]
    files = [e for e in events if e['phase'] == 'github_files']
    assert [e['completed'] for e in files] == list(range(5))
    assert [e['files_read'] for e in files] == [0, 1, 2, 3, 3]
    assert all(e['total'] == e['files_total'] == 4 for e in files)
    assert files[-1]['files_read'] == result['metadata']['files_read'] == len(result['files']) == 3
    encoded = json.dumps(events)
    assert all(secret not in encoded for secret in (URL, 'README', 'LICENSE', 'entry.py', 'Private', 'contract_owner'))
    assert all(set(e) <= {'phase', 'completed', 'total', 'files_read', 'files_total'} for e in events)


def test_checked_and_retained_counts_remain_distinct_at_context_budget():
    c, _ = connector(files={'README.md': b'a' * 50000, 'LICENSE': b'b' * 20000, 'entry.py': b'code'})
    events = []
    c.set_progress_callback(events.append)
    result = c.snapshot(URL, 'quick')
    final = events[-1]
    assert final == {'phase': 'github_files', 'completed': 3, 'total': 3, 'files_read': 2, 'files_total': 3}
    assert len(result['files']) == result['metadata']['files_read'] == 2


def test_empty_or_nontext_candidates_never_claim_files_were_read():
    for files, expected_checks in (({}, 0), ({'README.md': b'\x00binary'}, 1)):
        c, _ = connector(files=files)
        events = []
        c.set_progress_callback(events.append)
        with pytest.raises(GithubError) as error:
            c.snapshot(URL)
        assert error.value.code == 'no_content'
        assert events[-1] == {'phase': 'github_files', 'completed': expected_checks,
                              'total': expected_checks, 'files_read': 0, 'files_total': expected_checks}


def test_file_check_is_emitted_after_reader_returns_and_not_after_failure(monkeypatch):
    c, calls = connector()
    events = []
    c.set_progress_callback(events.append)
    original = c._text_file
    checked = 0

    def read(target, item):
        nonlocal checked
        assert events[-1]['phase'] == 'github_files' and events[-1]['completed'] == checked
        if checked == 1:
            raise GithubError('network', 'Explicit synthetic file failure')
        result = original(target, item)
        checked += 1
        return result

    monkeypatch.setattr(c, '_text_file', read)
    # Configured credentials prevent the existing API-error fallback, preserving
    # the exact failure/HTTP behavior being checked here.
    c.token = 'synthetic-configured-token'
    with pytest.raises(GithubError) as error:
        c.snapshot(URL)
    assert error.value.code == 'network'
    assert events[-1]['completed'] == 1 and events[-1]['files_read'] == 1
    assert sum('/git/blobs/' in path for _, path, _ in calls) == 1


@pytest.mark.parametrize('exception', [RuntimeError('Private observer failure'),
                                     AppError('configuration', 'Private observer failure')])
def test_observer_failure_never_changes_source_or_repeats_requests(exception):
    baseline, expected_calls = connector()
    expected = baseline.snapshot(URL)
    c, calls = connector()

    def broken(event):
        raise exception

    c.set_progress_callback(broken)
    assert c.snapshot(URL) == expected
    assert calls == expected_calls


@pytest.mark.parametrize('phase,completed,expected_requests', [
    ('github_metadata', 0, 0), ('github_metadata', 1, 2), ('github_tree', 0, 2), ('github_files', 1, 4)])
def test_explicit_cancellation_propagates_and_stops_later_work(phase, completed, expected_requests):
    c, calls = connector()
    cancelled = AppError('cancelled', 'Synthetic user cancellation')

    def observe(event):
        if event['phase'] == phase and event['completed'] == completed:
            raise cancelled

    c.set_progress_callback(observe)
    with pytest.raises(AppError) as error:
        c.snapshot(URL)
    assert error.value is cancelled and len(calls) == expected_requests


def test_progress_metrics_are_whitelisted_and_copied_not_shared():
    c, _ = connector()
    events = []
    c.set_progress_callback(events.append)
    c._emit_progress('github_files', completed=2, total=4, files_read=1, files_total=4,
                     url=URL, path='Private source path', title='Private title', message='Private headers')
    c._emit_progress('github_files', completed=-1, total=1.5, files_read=True, files_total='Private')
    c._emit_progress('Private URL as phase', completed=1)
    assert events == [{'phase': 'github_files', 'completed': 2, 'total': 4, 'files_read': 1, 'files_total': 4},
                      {'phase': 'github_files'}]
    captured = []

    def mutate(event):
        captured.append(dict(event))
        event['completed'] = event['files_read'] = 999

    c.set_progress_callback(mutate)
    result = c.snapshot(URL)
    assert [e['completed'] for e in captured if e['phase'] == 'github_files'] == list(range(5))
    assert result['metadata']['files_read'] == 3


def test_disabling_observer_and_old_snapshot_arguments_remain_supported():
    c, calls = connector()
    events = []
    c.set_progress_callback(events.append)
    c.set_progress_callback(None)
    assert c.snapshot(URL, 'deep')['version'] == SHA and not events
    assert len(calls) == 7


def test_unsafe_input_is_rejected_before_progress_or_http():
    c, calls = connector()
    events = []
    c.set_progress_callback(events.append)
    with pytest.raises(GithubError):
        c.snapshot('https://localhost/private')
    assert not calls and not events


@pytest.mark.parametrize('status,headers,token', [(401, {}, ''), (403, {}, ''), (404, {}, ''),
    (403, {'x-ratelimit-remaining': '0'}, 'synthetic-configured-token')])
def test_progress_cannot_enable_anonymous_fallback_for_auth_or_permission_failures(monkeypatch, status, headers, token):
    c, calls = connector(token=token, fail=lambda request: httpx.Response(status, headers=headers))
    events = []
    c.set_progress_callback(events.append)
    monkeypatch.setattr(c, '_snapshot_public_git', lambda *args: pytest.fail('No fallback permitted'))
    with pytest.raises(GithubError):
        c.snapshot(URL)
    assert events == [{'phase': 'github_metadata', 'completed': 0, 'total': 1}] and len(calls) == 1


def test_fallback_announces_transport_change_before_blocking_without_fake_ticks(tmp_path, monkeypatch):
    c, _ = connector(fail=lambda request: httpx.Response(429))
    events = []
    c.set_progress_callback(events.append)

    class PublicGitFixture:
        MAX_DISK = 128 * 1024 * 1024
        MAX_SECONDS = 75

        def __init__(self, workspace, repository):
            assert events[-1] == {'phase': 'github_fallback', 'completed': 0, 'total': 1}
            self.repo = workspace / 'repository.git'
            self.repo.mkdir()

        def resolve(self, parsed):
            self.before_fetch = list(events)
            return 'main', '', 'main'

        def fetch(self, revision):
            assert events == self.before_fetch  # No synthetic wait/heartbeat updates.
            return SHA

        def tree(self, sha, limit):
            assert events[-1] == {'phase': 'github_tree', 'completed': 0, 'total': 1}
            return ([{'path': path, 'sha': blob_sha(raw), 'size': len(raw), 'type': 'blob', 'mode': '100644'}
                     for path, raw in RAW.items()], False)

        def blob(self, item, limit):
            return RAW[item['path']]

    monkeypatch.setattr(github, '_PublicGit', PublicGitFixture)
    result = c.snapshot(URL)
    assert result['metadata']['transport'] == 'public_git'
    assert result['metadata']['fallback_reason'] == 'rate_limit' and result['version'] == SHA
    assert [e for e in events if e['phase'] == 'github_fallback'] == [
        {'phase': 'github_fallback', 'completed': 0, 'total': 1},
        {'phase': 'github_fallback', 'completed': 1, 'total': 1}]
    assert events[-1]['completed'] == 4 and events[-1]['files_read'] == 3


def test_cancellation_before_public_git_does_not_start_the_fallback(monkeypatch):
    c, calls = connector(fail=lambda request: httpx.Response(429))

    def observe(event):
        if event['phase'] == 'github_fallback':
            raise AppError('cancelled', 'Synthetic cancellation before public Git')

    c.set_progress_callback(observe)
    monkeypatch.setattr(github, '_PublicGit', lambda *args: pytest.fail('Cancelled before spawning public Git'))
    with pytest.raises(AppError) as error:
        c.snapshot(URL)
    assert error.value.code == 'cancelled' and len(calls) == 1
