"""Explicit local login/session contracts. No real browser, account or network."""
import copy
import base64
import io
import json
import os
import subprocess
import struct
import threading
import time
import zlib
from datetime import datetime, timezone

import pytest

from backend.douyin_session import (DouyinSessionService, MAX_STATE_BYTES, METADATA_ID,
    STATE_KEY, sanitize_storage_state)
import backend.douyin_session as login
from backend.security import AppError, Vault
from backend.store import Store


SECRET = 'SYNTHETIC_LOCAL_AUTH_SECRET'
PRIVATE = 'SYNTHETIC_ACCOUNT_RESPONSE_SHOULD_NEVER_ESCAPE'


def cookie(name='sessionid', domain='.douyin.com', expiry=None, value=SECRET):
    return {'name': name, 'value': value, 'domain': domain, 'path': '/',
        'expires': time.time() + 7200 if expiry is None else expiry,
        'httpOnly': True, 'secure': False, 'sameSite': 'Lax'}


def state():
    return {'cookies': [cookie(), cookie('ttwid', expiry=time.time() + 5)],
        'origins': [{'origin': 'https://www.douyin.com', 'localStorage': [
            {'name': 'passport_csrf_token', 'value': SECRET},
            {'name': 'userInfo', 'value': PRIVATE}]}], 'untrusted_extra': PRIVATE}


def success(storage=None):
    return {'type': 'result', 'ok': True, 'authenticated': True,
        'verification_method': 'official_profile_response',
        'storage_state': state() if storage is None else storage, 'account': PRIVATE}


class MemoryStore:
    def __init__(self):
        self.values, self.writes = {}, []

    def get(self, kind, id):
        return copy.deepcopy(self.values.get((kind, id)))

    def put(self, kind, value):
        self.writes.append(copy.deepcopy(value))
        self.values[(kind, value['id'])] = copy.deepcopy(value)
        return copy.deepcopy(value)


class MemoryVault:
    def __init__(self):
        self.values, self.keys = {}, []

    def get(self, key):
        self.keys.append(key)
        return self.values.get(key, '')

    def set(self, key, value):
        self.keys.append(key)
        if value:
            self.values[key] = value
        else:
            self.values.pop(key, None)


class FakeRunner:
    def __init__(self, result=None):
        self.result = success() if result is None else result
        self.started, self.release = threading.Event(), threading.Event()
        self.commands, self.closed, self.options = [], False, None

    def run(self, options, callback, cancelled):
        self.thread = threading.current_thread()
        self.options, self.callback, self.cancelled = copy.deepcopy(options), callback, cancelled
        self.started.set()
        assert self.release.wait(3), 'Test did not release its explicit fake runner'
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)

    def send(self, command):
        self.commands.append(command)

    def close(self):
        # Deliberately allow a late result after cancellation to test isolation.
        self.closed = True

    def finish(self):
        self.release.set()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


@pytest.fixture
def service_factory():
    services, all_runners = [], []

    def make(*runners, store=None, vault=None):
        store, vault = store or MemoryStore(), vault or MemoryVault()
        pending = list(runners)
        all_runners.extend(runners)
        service = DouyinSessionService(store, vault, runner_factory=lambda: pending.pop(0))
        services.append(service)
        return service, store, vault

    yield make
    for service in services:
        service.close()
    for runner in all_runners:
        runner.release.set()
        if runner.started.is_set():
            runner.thread.join(timeout=2)


def start(service, runner):
    report = service.start_login()
    assert report['status'] == 'opening' and runner.started.wait(1)


def private_free(value):
    serialized = json.dumps(value)
    assert SECRET not in serialized and PRIVATE not in serialized
    assert 'cookies' not in serialized and 'localStorage' not in serialized


def test_status_reads_only_metadata_and_never_creates_activity(service_factory):
    service, store, vault = service_factory()
    assert service.status() == {'status': 'signed_out', 'session_saved': False,
        'verified_at': None, 'expires_at': None, 'browser_open': False,
        'qr_available': False, 'qr_updated_at': None,
        'verification_available': False, 'verification_frame_id': None,
        'verification_width': None, 'verification_height': None,
        'verification_error': None, 'error': None}
    for _ in range(5):
        service.status()
    assert store.writes == [] and vault.keys == [STATE_KEY]


def test_verified_login_is_encrypted_only_and_state_snapshot_is_independent(service_factory):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner)
    assert runner.options == {'mode': 'login', 'timeout_ms': 600000}
    runner.callback({'type': 'progress', 'phase': 'waiting_scan', 'account': PRIVATE})
    assert service.status()['browser_open'] is True
    runner.finish()
    report = service.status()
    assert report['status'] == 'logged_in' and report['session_saved'] is True
    assert report['browser_open'] is False and report['verified_at']
    # Short-lived visitor expiry must not expire the authenticated session.
    assert datetime.fromisoformat(report['expires_at']).timestamp() > time.time() + 7000
    private_free(report); private_free(store.writes)
    restored = service.storage_state()
    assert restored['cookies'][0]['secure'] is False
    assert PRIVATE not in json.dumps(restored)
    restored['cookies'][0]['value'] = 'MUTATED_COPY'
    assert service.storage_state()['cookies'][0]['value'] == SECRET
    assert set(vault.values) == {STATE_KEY}


@pytest.mark.parametrize('method', [None, 'cookie_present', 'public_profile', 'synthetic'])
def test_cookie_or_untrusted_authentication_claim_cannot_confirm_login(service_factory, method):
    result = success(); result['verification_method'] = method
    runner = FakeRunner(result)
    service, store, vault = service_factory(runner)
    start(service, runner); runner.finish()
    assert service.status()['status'] == 'failed'
    assert service.storage_state() is None and not vault.values
    private_free(service.status()); private_free(store.writes)


def test_authentication_claim_without_auth_cookie_is_rejected(service_factory):
    runner = FakeRunner(success({'cookies': [cookie('ttwid')], 'origins': []}))
    service, _, vault = service_factory(runner)
    start(service, runner); runner.finish()
    assert service.status()['status'] == 'failed' and not vault.values


@pytest.mark.parametrize('operation,expected', [('cancel_login', 'cancelled'), ('clear', 'signed_out')])
def test_cancel_or_clear_cannot_be_undone_by_late_login(service_factory, operation, expected):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner)
    getattr(service, operation)()
    runner.callback({'type': 'progress', 'phase': 'waiting_scan', 'body': PRIVATE})
    runner.finish()
    assert service.status()['status'] == expected
    assert service.storage_state() is None and not vault.values
    private_free(service.status()); private_free(store.writes)


def test_relogin_ignores_previous_runner_result_and_does_not_import_old_context(service_factory):
    old = FakeRunner(success({'cookies': [cookie(value='SYNTHETIC_OLD')], 'origins': []}))
    new = FakeRunner()
    service, _, vault = service_factory(old, new)
    start(service, old); start(service, new)
    new.finish(); old.finish()
    assert service.status()['status'] == 'logged_in'
    assert service.storage_state()['cookies'][0]['value'] == SECRET
    assert 'storage_state' not in new.options
    assert 'SYNTHETIC_OLD' not in vault.values[STATE_KEY]


def test_check_in_open_window_sends_fixed_command_without_new_runner(service_factory):
    runner = FakeRunner()
    service, store, _ = service_factory(runner)
    start(service, runner)
    runner.callback({'phase': 'waiting_scan'})
    writes = len(store.writes)
    assert service.check_login()['status'] == 'waiting_scan'
    service.check_login()
    assert runner.commands == [{'type': 'check'}]
    assert len(store.writes) == writes
    # An authoritative runner receipt, even for the same phase, unlocks checks.
    runner.callback({'type': 'progress', 'phase': 'waiting_scan'})
    service.check_login()
    assert runner.commands == [{'type': 'check'}, {'type': 'check'}]
    assert service.status()['status'] == 'waiting_scan'
    runner.finish()


def test_saved_check_uses_state_and_failed_authentication_blocks_reuse(service_factory):
    initial, check = FakeRunner(), FakeRunner({'ok': False, 'authenticated': False,
        'error': {'code': 'douyin_session_not_authenticated', 'message': PRIVATE}, 'body': PRIVATE})
    service, store, vault = service_factory(initial, check)
    start(service, initial); initial.finish()
    assert service.check_login()['status'] == 'opening' and check.started.wait(1)
    assert check.options['mode'] == 'check' and check.options['timeout_ms'] == 45000
    assert check.options['storage_state']['cookies'][0]['value'] == SECRET
    check.finish()
    assert service.status()['status'] == 'expired' and service.storage_state() is None
    assert service.status()['session_saved'] is True
    private_free(service.status()); private_free(store.writes)
    # A restart must not silently revive the officially rejected session.
    restarted = DouyinSessionService(store, vault, runner_factory=lambda: None)
    assert restarted.status()['status'] == 'expired' and restarted.storage_state() is None
    restarted.close()


def test_cancel_relogin_keeps_previously_verified_session(service_factory):
    initial, relogin = FakeRunner(), FakeRunner()
    service, _, _ = service_factory(initial, relogin)
    start(service, initial); initial.finish()
    start(service, relogin)
    service.cancel_login(); relogin.finish()
    assert service.status()['status'] == 'cancelled'
    assert service.status()['session_saved'] is True
    assert service.storage_state()['cookies'][0]['value'] == SECRET


def test_verification_and_unknown_errors_are_fixed_and_do_not_leak(service_factory):
    verify = FakeRunner({'ok': False, 'error': {'code': 'douyin_session_verification_required',
        'message': PRIVATE}, 'account_id': PRIVATE})
    unknown = FakeRunner(RuntimeError(SECRET + PRIVATE))
    service, store, _ = service_factory(verify, unknown)
    start(service, verify)
    verify.callback({'phase': 'verification_required', 'cookie': SECRET})
    assert service.status()['status'] == 'verification_required'
    verify.finish()
    assert service.status()['error']['code'] == 'douyin_session_verification_required'
    start(service, unknown); unknown.finish()
    assert service.status()['error']['code'] == 'douyin_session_protocol'
    private_free(service.status()); private_free(store.writes)


def test_close_prevents_late_save_without_deleting_saved_credentials(service_factory):
    runner = FakeRunner()
    service, _, vault = service_factory(runner)
    start(service, runner)
    service.close(); runner.finish()
    assert not vault.values and service.status()['browser_open'] is False


def test_unknown_expiry_is_null_and_never_uses_visitor_deadline(service_factory):
    runner = FakeRunner(success({'cookies': [cookie(expiry=-1), cookie('ttwid', expiry=time.time() + 1)], 'origins': []}))
    service, _, _ = service_factory(runner)
    start(service, runner); runner.finish()
    assert service.status()['expires_at'] is None and service.storage_state()


def test_expired_auth_cookie_is_read_only_derived_status(service_factory, monkeypatch):
    runner = FakeRunner()
    service, store, _ = service_factory(runner)
    start(service, runner); runner.finish()
    writes = len(store.writes)
    monkeypatch.setattr('backend.douyin_session.time.time', lambda: 253402300000)
    assert service.status()['status'] == 'expired' and service.storage_state() is None
    assert len(store.writes) == writes


def test_filter_drops_foreign_cookies_and_non_https_or_profile_storage():
    original = state()
    original['cookies'].extend([cookie(domain='.evil.invalid'), cookie(domain='.douyin.com.evil.invalid'),
        cookie(domain='user@douyin.com')])
    original['origins'].extend([
        {'origin': 'http://www.douyin.com', 'localStorage': [{'name': 'ttwid', 'value': SECRET}]},
        {'origin': 'https://www.douyin.com:8443', 'localStorage': [{'name': 'ttwid', 'value': SECRET}]},
        {'origin': 'https://evil.invalid', 'localStorage': [{'name': 'ttwid', 'value': SECRET}]}])
    filtered = sanitize_storage_state(original)
    assert len(filtered['cookies']) == 2 and len(filtered['origins']) == 1
    assert filtered['origins'][0]['localStorage'] == [{'name': 'passport_csrf_token', 'value': SECRET}]
    assert PRIVATE not in json.dumps(filtered) and 'evil.invalid' not in json.dumps(filtered)


@pytest.mark.parametrize('bad', [
    {'cookies': 'wrong'}, {'cookies': [cookie()] * 129},
    {'cookies': [cookie(value='x' * 8193)]},
    {'cookies': [cookie(name='bad\nname')]},
    {'cookies': [cookie(), cookie()]},
    {'cookies': [cookie(expiry=float('nan'))]},
    {'cookies': [cookie()], 'unexpected': 'x' * MAX_STATE_BYTES},
    {'cookies': [cookie()], 'origins': [{'origin': 'https://douyin.com', 'localStorage': [
        {'name': 'ttwid', 'value': 'x' * 16385}]}]},
])
def test_storage_state_has_bounded_structure_and_values(bad):
    with pytest.raises(AppError) as error:
        sanitize_storage_state(bad)
    assert error.value.code == 'douyin_session_protocol'
    assert SECRET not in error.value.message


def test_real_local_vault_keeps_session_out_of_database_and_plain_files(tmp_path, service_factory):
    store, vault = Store(tmp_path), Vault(tmp_path)
    runner = FakeRunner()
    service, _, _ = service_factory(runner, store=store, vault=vault)
    start(service, runner); runner.finish()
    assert service.status()['status'] == 'logged_in'
    assert json.loads(vault.get(STATE_KEY))['cookies'][0]['value'] == SECRET
    for path in tmp_path.iterdir():
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes() and PRIVATE.encode() not in path.read_bytes()
    private_free(store.get('connections', METADATA_ID))
    service.clear()
    assert vault.get(STATE_KEY) == ''


def test_cancel_before_runner_factory_finishes_is_cancelled_and_cannot_save(service_factory):
    entered, unblock = threading.Event(), threading.Event()
    runner = FakeRunner()

    def factory():
        entered.set(); assert unblock.wait(2)
        return runner

    service, _, vault = service_factory()
    service.runner_factory = factory
    service.start_login(); assert entered.wait(1)
    thread = service.active['thread']
    assert service.cancel_login()['status'] == 'cancelled'
    unblock.set(); thread.join(timeout=1)
    assert not thread.is_alive() and runner.closed and not vault.values


def test_initial_metadata_failure_does_not_start_a_runner(service_factory):
    class BrokenStore(MemoryStore):
        def put(self, *args):
            raise RuntimeError(SECRET + PRIVATE)

    runner = FakeRunner()
    service, _, vault = service_factory(runner, store=BrokenStore())
    assert service.start_login()['error']['code'] == 'douyin_session_storage'
    assert service.status()['status'] == 'failed' and not runner.started.is_set()
    assert not service.status()['browser_open'] and not vault.values
    private_free(service.status())


def test_vault_failure_never_claims_saved_login(service_factory):
    class BrokenVault(MemoryVault):
        def set(self, *args):
            raise RuntimeError(SECRET + PRIVATE)

    runner = FakeRunner()
    service, store, _ = service_factory(runner, vault=BrokenVault())
    start(service, runner); runner.finish()
    assert service.status()['status'] == 'failed' and service.storage_state() is None
    assert service.status()['error']['code'] == 'douyin_session_storage'
    private_free(service.status()); private_free(store.writes)


def test_changed_vault_state_without_matching_metadata_requires_recheck(service_factory):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner); runner.finish()
    replacement = sanitize_storage_state({'cookies': [cookie(value='SYNTHETIC_DIFFERENT_SESSION')], 'origins': []})
    # Simulates interruption between the encrypted write and metadata commit.
    vault.values[STATE_KEY] = json.dumps(replacement)
    restarted = DouyinSessionService(store, vault, runner_factory=lambda: None)
    assert restarted.status()['status'] == 'signed_out' and restarted.storage_state() is None
    assert restarted.status()['session_saved'] is True
    restarted.close()


class FakeProxy:
    def __init__(self, address, budget):
        self.server_address = ('127.0.0.1', 12345)
        self.stop = threading.Event()
        self.tunnels_stopped = self.closed = False

    def serve_forever(self):
        self.stop.wait(2)

    def stop_tunnels(self):
        self.tunnels_stopped = True

    def shutdown(self):
        self.stop.set()

    def server_close(self):
        self.closed = True


@pytest.mark.parametrize('case', ['success', 'malformed', 'qr_limit', 'total_limit',
    'qr_expired_total', 'verification_limit', 'verification_total'])
def test_node_runner_has_no_profile_file_or_secret_environment_and_cleans_up(tmp_path, monkeypatch, case):
    script = tmp_path / 'frontend/scripts/douyin-login.mjs'
    script.parent.mkdir(parents=True)
    script.write_text('// Explicit fake runner; never executed', encoding='utf-8')
    monkeypatch.setattr(login, 'PROJECT', tmp_path)
    monkeypatch.setattr(login.shutil, 'which', lambda name: 'SYNTHETIC_NODE')
    monkeypatch.setenv('MODEL_API_KEY', SECRET)
    monkeypatch.setenv('NODE_OPTIONS', '--require=' + PRIVATE)
    if case == 'verification_total':
        monkeypatch.setattr(login, 'MAX_VERIFICATION_TOTAL', 1024)
    proxies, captured = [], {}

    def proxy(address, budget):
        value = FakeProxy(address, budget); proxies.append(value)
        return value

    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            if case == 'success':
                output = '\n'.join(json.dumps(value) for value in [
                    {'type': 'qr_clear', 'unknown': PRIVATE},
                    {'type': 'qr_expired', 'unknown': PRIVATE}, verification_message(),
                    {'type': 'verification_error', 'code': 'input_focus', 'message': PRIVATE},
                    {'type': 'verification_clear', 'unknown': PRIVATE}, success()]) + '\n'
            elif case == 'qr_limit':
                output = (json.dumps(qr_message()) + '\n') * 21 + json.dumps(success()) + '\n'
            elif case == 'total_limit':
                output = (json.dumps({'type': 'progress', 'phase': 'waiting_scan',
                    'unknown': 'x' * 300000}) + '\n') * 8 + json.dumps(success()) + '\n'
            elif case == 'qr_expired_total':
                output = (json.dumps({'type': 'qr_expired', 'unknown': 'x' * 300000}) + '\n') * 8 + json.dumps(success()) + '\n'
            elif case == 'verification_limit':
                output = (json.dumps(verification_message()) + '\n') * 49 + json.dumps(success()) + '\n'
            elif case == 'verification_total':
                output = (json.dumps({**verification_message(), 'unknown': 'x' * 600}) + '\n') * 3 + json.dumps(success()) + '\n'
            else:
                output = 'MALFORMED ' + PRIVATE + '\n'
            self.stdout = io.StringIO(output)
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            captured['ipc'] = self.stdin.getvalue()
            self.returncode = 0

    def popen(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return Process()

    monkeypatch.setattr(login, '_PinnedProxy', proxy)
    monkeypatch.setattr(login.subprocess, 'Popen', popen)
    monkeypatch.setattr(login, '_kill', lambda process: None)
    runner = login._NodeRunner()
    options = {'mode': 'check', 'timeout_ms': 45000, 'storage_state': sanitize_storage_state(state())}
    if case == 'success':
        messages = []
        assert runner.run(options, messages.append, lambda: False)['authenticated'] is True
        assert messages == [{'type': 'qr_clear'}, {'type': 'qr_expired'},
            {key: value for key, value in verification_message().items() if key != 'untrusted'},
            {'type': 'verification_error', 'code': 'input_focus'}, {'type': 'verification_clear'}]
        assert PRIVATE not in json.dumps(messages)
    else:
        with pytest.raises(AppError) as error:
            runner.run(options, lambda message: None, lambda: False)
        assert error.value.code == 'douyin_session_protocol'
        private_free({'message': error.value.message})
    assert captured['args'] == ['SYNTHETIC_NODE', str(script)]
    assert captured['kwargs']['stderr'] is subprocess.DEVNULL
    assert 'MODEL_API_KEY' not in captured['kwargs']['env']
    assert 'NODE_OPTIONS' not in captured['kwargs']['env']
    assert captured['kwargs']['start_new_session'] is (os.name != 'nt')
    first = json.loads(captured['ipc'].splitlines()[0])
    assert first['type'] == 'start' and first['guard_proxy'] == 'http://127.0.0.1:12345'
    assert first['storage_state']['cookies'][0]['value'] == SECRET
    assert proxies[0].tunnels_stopped and proxies[0].closed
    assert set(tmp_path.rglob('*')) == {script.parent.parent, script.parent, script}


def test_node_runner_cancel_does_not_wait_for_a_blocked_stdin_writer(monkeypatch):
    runner = login._NodeRunner()
    runner.process = type('OwnedProcess', (), {'poll': lambda self: None})()
    killed = []
    monkeypatch.setattr(login, '_kill', lambda process: killed.append(process))
    runner.write_lock.acquire()
    try:
        closing = threading.Thread(target=runner.close, daemon=True)
        closing.start(); closing.join(timeout=.5)
        assert not closing.is_alive() and killed == [runner.process]
        assert runner.stopping.is_set()
    finally:
        runner.write_lock.release()


def qr_png(width=1):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, 1, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(b'\0\xff\xff\xff')) + chunk(b'IEND', b''))


def qr_message(data=None):
    return {'type': 'qr', 'image_base64': base64.b64encode(qr_png() if data is None else data).decode(),
        'cookie': SECRET, 'account': PRIVATE}


def test_qr_is_current_login_memory_only_and_creates_no_persisted_heartbeat(service_factory):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner)
    writes, values = len(store.writes), copy.deepcopy(vault.values)
    runner.callback(qr_message())
    assert service.qr_image() == qr_png()
    report = service.status()
    assert report['qr_available'] is True and report['qr_updated_at']
    assert len(store.writes) == writes and vault.values == values
    assert all('qr_available' not in item and 'qr_updated_at' not in item for item in store.writes)
    private_free(report); private_free(store.writes)
    assert 'image_base64' not in json.dumps(report)
    runner.finish()
    assert service.qr_image() is None and service.status()['qr_available'] is False
    assert service.status()['qr_updated_at'] is None


@pytest.mark.parametrize('image', [
    None, [], 'not base64 ' + PRIVATE, 'data:image/png;base64,abcd',
    base64.b64encode(b'not PNG ' + PRIVATE.encode()).decode(),
    base64.b64encode(b'\x89PNG\r\n\x1a\nnot a complete PNG').decode(),
    base64.b64encode(qr_png() + b'x' * (64 * 1024)).decode(),
    base64.b64encode(qr_png(width=100000)).decode(),
    base64.b64encode(qr_png()).decode() + '\n',
], ids=['none', 'wrong-type', 'not-base64', 'data-uri', 'not-png',
        'truncated-png', 'oversized-png', 'oversized-dimensions', 'trailing-newline'])
def test_invalid_or_excessive_qr_data_is_ignored_without_echo(service_factory, image):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner)
    runner.callback({'type': 'qr', 'image_base64': image, 'cookie': SECRET})
    assert service.qr_image() is None and service.status()['qr_available'] is False
    assert not vault.values
    private_free(service.status()); private_free(store.writes)
    runner.finish()


@pytest.mark.parametrize('action', ['cancel_login', 'clear', 'start_login', 'close'])
def test_qr_is_removed_by_cancel_clear_relogin_or_close(service_factory, action):
    old, new = FakeRunner(), FakeRunner()
    service, _, _ = service_factory(old, new)
    start(service, old); old.callback(qr_message())
    assert service.qr_image() is not None
    getattr(service, action)()
    assert service.qr_image() is None and service.status()['qr_updated_at'] is None
    old.callback(qr_message())
    assert service.qr_image() is None
    if action == 'start_login':
        assert new.started.wait(1)
        new.callback(qr_message())
        assert service.qr_image() is not None
        new.finish()
    old.finish()
    assert service.qr_image() is None


def test_saved_check_never_exposes_a_qr_and_timeout_removes_login_qr(service_factory):
    initial = FakeRunner()
    checking = FakeRunner({'ok': False, 'error': {'code': 'douyin_session_timeout'}})
    login_timeout = FakeRunner(AppError('douyin_session_timeout', PRIVATE))
    service, _, _ = service_factory(initial, checking, login_timeout)
    start(service, initial); initial.finish()
    service.check_login(); assert checking.started.wait(1)
    checking.callback(qr_message())
    assert service.qr_image() is None
    checking.finish()
    start(service, login_timeout); login_timeout.callback(qr_message())
    assert service.qr_image() is not None
    login_timeout.finish()
    assert service.qr_image() is None and service.status()['qr_available'] is False


def test_qr_update_count_has_an_independent_service_limit(service_factory):
    runner = FakeRunner()
    service, _, _ = service_factory(runner)
    start(service, runner)
    for width in range(1, 21):
        runner.callback(qr_message(qr_png(width)))
    assert service.qr_image() is not None
    runner.callback(qr_message(qr_png(21)))
    assert service.qr_image() is None
    runner.finish()


def test_identical_qr_does_not_change_image_version_count_or_storage(service_factory):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner)
    runner.callback(qr_message())
    before, writes, encrypted = service.status(), len(store.writes), copy.deepcopy(vault.values)
    for _ in range(50):
        runner.callback(qr_message())
    assert service.status() == before and service.qr_image() == qr_png()
    assert service.active['qr_count'] == 1
    assert len(store.writes) == writes and vault.values == encrypted
    service.check_login()
    runner.callback(qr_message())
    assert service.status()['qr_updated_at'] == before['qr_updated_at']
    assert service.active['qr_count'] == 1 and runner.commands == [{'type': 'check'}]
    runner.finish()


def test_qr_expiration_clears_image_and_requires_user_action_without_refreshing(service_factory):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner)
    runner.callback({'type': 'progress', 'phase': 'waiting_scan'})
    runner.callback(qr_message())
    writes, encrypted = len(store.writes), copy.deepcopy(vault.values)
    runner.callback({'type': 'qr_expired', 'cookie': SECRET, 'message': PRIVATE})
    expected = {'code': 'douyin_session_qr_expired',
        'message': '官网二维码已失效，请点击重新获取二维码'}
    assert service.status()['status'] == 'waiting_scan'
    assert service.status()['error'] == expected and service.status()['browser_open'] is True
    assert service.qr_image() is None and service.status()['qr_updated_at'] is None
    assert runner.commands == [] and len(store.writes) == writes and vault.values == encrypted
    assert service.check_login()['status'] == 'waiting_scan'
    runner.callback({'type': 'progress', 'phase': 'checking'})
    assert service.status()['status'] == 'waiting_scan'
    runner.callback({'type': 'progress', 'phase': 'waiting_scan'})
    assert service.status()['error'] == expected and service.qr_image() is None
    assert runner.commands == [{'type': 'check'}]
    runner.callback(qr_message(qr_png(2)))
    assert service.qr_image() == qr_png(2) and service.status()['error'] is None
    # New valid image events clear only the QR-expired hint, not other errors.
    with service.lock:
        service._error = login._error('douyin_session_runtime')
    runner.callback(qr_message(qr_png(3)))
    assert service.status()['error']['code'] == 'douyin_session_runtime'
    private_free(service.status()); private_free(store.writes)
    runner.finish()


def test_qr_expiration_cannot_override_mfa_or_a_new_login_generation(service_factory):
    old, new = FakeRunner(), FakeRunner()
    service, store, _ = service_factory(old, new)
    start(service, old)
    show_verification(old)
    before, image, writes = service.status(), service.verification_image(), len(store.writes)
    old.callback({'type': 'qr_expired', 'message': PRIVATE})
    assert service.status() == before and service.verification_image() == image
    assert len(store.writes) == writes and old.commands == []
    start(service, new)
    new.callback({'type': 'progress', 'phase': 'waiting_scan'})
    new.callback(qr_message())
    before = service.status()
    old.callback({'type': 'qr_expired'})
    assert service.status() == before and service.qr_image() == qr_png()
    new.finish(); old.finish()


def test_headless_check_ignores_qr_expiration(service_factory):
    initial, checking = FakeRunner(), FakeRunner()
    service, _, _ = service_factory(initial, checking)
    start(service, initial); initial.finish()
    service.check_login(); assert checking.started.wait(1)
    checking.callback({'type': 'progress', 'phase': 'checking'})
    before = service.status()
    checking.callback({'type': 'qr_expired'})
    assert service.status() == before and checking.commands == []
    checking.finish()


def test_qr_clear_is_memory_only_and_late_clear_cannot_erase_new_login_image(service_factory):
    old, new = FakeRunner(), FakeRunner()
    service, store, vault = service_factory(old, new)
    start(service, old); old.callback(qr_message())
    writes, encrypted = len(store.writes), copy.deepcopy(vault.values)
    old.callback({'type': 'qr_clear', 'cookie': SECRET, 'account': PRIVATE})
    assert service.qr_image() is None and service.status()['qr_updated_at'] is None
    assert len(store.writes) == writes and vault.values == encrypted
    old.callback(qr_message())
    start(service, new); new.callback(qr_message())
    before = service.status()['qr_updated_at']
    old.callback({'type': 'qr_clear'})
    assert service.qr_image() == qr_png() and service.status()['qr_updated_at'] == before
    private_free(service.status()); private_free(store.writes)
    new.finish(); old.finish()


def test_active_check_preserves_live_qr_until_official_confirmation(service_factory):
    runner = FakeRunner()
    service, _, _ = service_factory(runner)
    start(service, runner)
    runner.callback({'type': 'progress', 'phase': 'waiting_scan'})
    runner.callback(qr_message())
    before = service.status()['qr_updated_at']
    assert service.check_login()['status'] == 'waiting_scan'
    assert service.qr_image() == qr_png() and service.status()['qr_updated_at'] == before
    runner.callback({'type': 'progress', 'phase': 'checking'})
    runner.callback(qr_message())
    assert service.qr_image() == qr_png()
    assert runner.commands == [{'type': 'check'}]
    runner.callback({'type': 'progress', 'phase': 'waiting_confirmation', 'account': PRIVATE})
    report = service.status()
    assert report['status'] == 'waiting_confirmation' and report['browser_open'] is True
    assert report['qr_available'] is False and report['qr_updated_at'] is None
    # Only a genuine new official waiting_scan phase can re-enable a QR.
    runner.callback(qr_message())
    assert service.qr_image() is None
    private_free(report)
    runner.finish()
    assert service.status()['status'] == 'logged_in'


def test_check_does_not_replace_phone_confirmation_or_revive_late_phase(service_factory):
    runner = FakeRunner()
    service, store, _ = service_factory(runner)
    start(service, runner)
    runner.callback({'type': 'progress', 'phase': 'waiting_confirmation'})
    assert service.check_login()['status'] == 'waiting_confirmation'
    service.check_login()
    assert runner.commands == [{'type': 'check'}]
    assert service.status()['browser_open'] is True and service.qr_image() is None
    service.cancel_login()
    runner.callback({'type': 'progress', 'phase': 'waiting_confirmation'})
    assert service.status()['status'] == 'cancelled' and service.status()['browser_open'] is False
    private_free(service.status()); private_free(store.writes)
    runner.finish()


def verification_message(identity='1', width=12, height=8):
    # Explicit synthetic JPEG-header fixture; no real verification content.
    sof = b'\x08' + height.to_bytes(2, 'big') + width.to_bytes(2, 'big') + b'\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00'
    image = b'\xff\xd8\xff\xc0' + (len(sof) + 2).to_bytes(2, 'big') + sof + b'\xff\xd9'
    return {'type': 'verification_frame', 'image_base64': base64.b64encode(image).decode(),
        'width': width, 'height': height, 'frame_id': identity, 'untrusted': PRIVATE}


def show_verification(runner, identity='1'):
    runner.callback({'type': 'progress', 'phase': 'verification_required'})
    message = verification_message(identity)
    runner.callback(message)
    return base64.b64decode(message['image_base64'])


def test_verification_image_is_memory_only_with_public_id_and_real_header_dimensions(service_factory):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner)
    runner.callback(verification_message())
    assert service.verification_image() is None
    runner.callback({'type': 'progress', 'phase': 'verification_required'})
    writes, saved = len(store.writes), copy.deepcopy(vault.values)
    runner.callback(verification_message())
    report = service.status()
    assert service.verification_image() == base64.b64decode(verification_message()['image_base64'])
    assert report['verification_available'] is True and report['verification_frame_id'] != '1'
    assert report['verification_width'] == 12 and report['verification_height'] == 8
    assert len(store.writes) == writes and vault.values == saved
    assert all(not any(key.startswith('verification_') for key in item) for item in store.writes)
    private_free(report); private_free(store.writes)
    runner.finish()
    assert service.verification_image() is None and service.status()['verification_frame_id'] is None


@pytest.mark.parametrize('payload', [
    {'kind': 'click', 'x': 0, 'y': 7}, {'kind': 'text', 'text': SECRET},
    {'kind': 'key', 'key': 'Tab'}, {'kind': 'refresh'},
])
def test_manual_verification_action_is_whitelisted_consumed_and_never_echoed(service_factory, payload):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner); show_verification(runner, '23')
    identity = service.status()['verification_frame_id']
    saved, writes = copy.deepcopy(vault.values), len(store.writes)
    report = service.verification_action({'frame_id': identity, **payload})
    assert runner.commands == [{'type': 'verification_action', 'frame_id': '23', **payload}]
    assert report['verification_available'] is False and report['verification_frame_id'] is None
    assert service.verification_image() is None and vault.values == saved and len(store.writes) == writes
    private_free(report); private_free(store.writes)
    with pytest.raises(AppError) as error:
        service.verification_action({'frame_id': identity, **payload})
    assert error.value.code == 'douyin_verification_stale' and len(runner.commands) == 1
    # An old node frame cannot revive a consumed public ID.
    runner.callback(verification_message('23'))
    assert service.status()['verification_available'] is False
    runner.callback(verification_message('24'))
    assert service.status()['verification_available'] is True
    assert service.status()['verification_frame_id'] != identity
    runner.finish()


@pytest.mark.parametrize('payload', [
    {'kind': 'click', 'x': True, 'y': 0}, {'kind': 'click', 'x': 0.5, 'y': 0},
    {'kind': 'click', 'x': -1, 'y': 0}, {'kind': 'click', 'x': 12, 'y': 0},
    {'kind': 'click', 'x': 0, 'y': 8}, {'kind': 'click', 'x': 0},
    {'kind': 'text', 'text': ''}, {'kind': 'text', 'text': 'x' * 129},
    {'kind': 'text', 'text': '123\n456'}, {'kind': 'text', 'text': '123\u202e456'},
    {'kind': 'key', 'key': 'Control+L'}, {'kind': 'key', 'key': 'F12'},
    {'kind': 'refresh', 'url': PRIVATE}, {'kind': 'execute', 'text': PRIVATE},
])
def test_invalid_manual_action_does_not_consume_the_current_frame_or_echo_values(service_factory, payload):
    runner = FakeRunner()
    service, _, _ = service_factory(runner)
    start(service, runner); show_verification(runner)
    identity = service.status()['verification_frame_id']
    with pytest.raises(AppError) as error:
        service.verification_action({'frame_id': identity, **payload})
    assert error.value.code == 'douyin_verification_action'
    assert service.status()['verification_frame_id'] == identity and runner.commands == []
    private_free({'message': error.value.message})
    runner.finish()


@pytest.mark.parametrize('overrides', [
    {'image_base64': PRIVATE}, {'image_base64': base64.b64encode(b'not jpeg').decode()},
    {'width': True}, {'height': 0}, {'width': 2049}, {'height': 9},
    {'frame_id': PRIVATE}, {'frame_id': '1\n'}, {'frame_id': True},
    {'image_base64': base64.b64encode(b'\xff\xd8' + b'x' * 262144 + b'\xff\xd9').decode()},
])
def test_verification_frames_reject_bad_id_size_encoding_and_forged_dimensions(service_factory, overrides):
    runner = FakeRunner()
    service, store, _ = service_factory(runner)
    start(service, runner)
    runner.callback({'type': 'progress', 'phase': 'verification_required'})
    runner.callback({**verification_message(), **overrides})
    assert service.verification_image() is None and service.status()['verification_available'] is False
    private_free(service.status()); private_free(store.writes)
    runner.finish()


@pytest.mark.parametrize('action', ['cancel_login', 'clear', 'start_login', 'close'])
def test_verification_frame_and_late_events_are_isolated_by_login_generation(service_factory, action):
    old, new = FakeRunner(), FakeRunner()
    service, _, _ = service_factory(old, new)
    start(service, old); show_verification(old)
    identity = service.status()['verification_frame_id']
    getattr(service, action)()
    assert service.verification_image() is None
    if action == 'start_login':
        assert new.started.wait(1)
        show_verification(new)  # Node integer ID restarts at 1 in a new generation.
        assert service.status()['verification_frame_id'] != identity
        old.callback({'type': 'verification_clear'})
        old.callback({'type': 'verification_error', 'code': 'budget', 'message': PRIVATE})
        assert service.status()['verification_available'] is True
        assert service.status()['verification_error'] is None
        with pytest.raises(AppError):
            service.verification_action({'frame_id': identity, 'kind': 'refresh'})
        new.finish()
    old.callback(verification_message('2'))
    old.finish()
    assert service.verification_image() is None


def test_verification_clear_errors_and_active_check_preserve_official_challenge(service_factory):
    runner = FakeRunner()
    service, store, _ = service_factory(runner)
    start(service, runner); show_verification(runner)
    image = service.verification_image()
    assert service.check_login()['status'] == 'verification_required'
    assert service.verification_image() == image and runner.commands == [{'type': 'check'}]
    for code in ('input_focus', 'action_failed', 'stale_frame', 'budget'):
        runner.callback({'type': 'verification_error', 'code': code, 'message': SECRET + PRIVATE})
        assert service.status()['verification_error']['code'] == code
        private_free(service.status())
    before = copy.deepcopy(service.status()['verification_error'])
    runner.callback({'type': 'verification_error', 'code': PRIVATE, 'message': SECRET})
    assert service.status()['verification_error'] == before
    runner.callback(verification_message('2'))
    assert service.status()['verification_error'] is None
    runner.callback({'type': 'verification_clear'})
    assert service.verification_image() is None
    runner.callback(verification_message('3'))
    runner.callback({'type': 'progress', 'phase': 'waiting_scan'})
    assert service.verification_image() is not None and service.status()['verification_error'] is None
    assert service.status()['status'] == 'verification_required'
    private_free(store.writes)
    runner.finish()


def test_verification_frame_and_action_budgets_are_independent_and_bounded(service_factory, monkeypatch):
    runner = FakeRunner()
    service, _, _ = service_factory(runner)
    start(service, runner)
    runner.callback({'type': 'progress', 'phase': 'verification_required'})
    for identity in range(50):
        runner.callback(verification_message(str(identity)))
    assert service.verification_image() is None
    assert service.status()['verification_error']['code'] == 'budget'
    assert len(service.active['verification_seen']) == 48
    runner.finish()

    # Isolate the user-action ceiling from the stricter production frame cap.
    monkeypatch.setattr(login, 'MAX_VERIFICATION_FRAMES', 200)
    next_runner = FakeRunner()
    next_service, _, _ = service_factory(next_runner)
    start(next_service, next_runner)
    next_runner.callback({'type': 'progress', 'phase': 'verification_required'})
    for identity in range(128):
        next_runner.callback(verification_message(str(identity)))
        next_service.verification_action({'frame_id': next_service.status()['verification_frame_id'], 'kind': 'refresh'})
    next_runner.callback(verification_message('129'))
    with pytest.raises(AppError) as error:
        next_service.verification_action({'frame_id': next_service.status()['verification_frame_id'], 'kind': 'refresh'})
    assert error.value.code == 'douyin_verification_budget' and len(next_runner.commands) == 128
    private_free(next_service.status())
    next_runner.finish()


def test_verification_send_is_outside_service_lock_and_send_failure_is_safe(service_factory):
    runner = FakeRunner()
    service, _, _ = service_factory(runner)
    start(service, runner); show_verification(runner)

    def send(command):
        read = threading.Event()
        observer = threading.Thread(target=lambda: (service.status(), read.set()), daemon=True)
        observer.start(); assert read.wait(.5), 'service lock was held during IPC'
        raise RuntimeError(SECRET + PRIVATE)

    runner.send = send
    result = service.verification_action({'frame_id': service.status()['verification_frame_id'], 'kind': 'text', 'text': SECRET})
    assert result['verification_available'] is False and result['verification_error']['code'] == 'action_failed'
    private_free(result)
    runner.finish()


def test_saved_check_allows_manual_official_verification_without_mutating_credentials(service_factory):
    initial, checking = FakeRunner(), FakeRunner()
    service, store, vault = service_factory(initial, checking)
    start(service, initial); initial.finish()
    saved = copy.deepcopy(vault.values)
    service.check_login(); assert checking.started.wait(1)
    show_verification(checking, '99')
    assert service.status()['verification_available'] is True
    report = service.verification_action({'frame_id': service.status()['verification_frame_id'], 'kind': 'key', 'key': 'Enter'})
    assert checking.commands == [{'type': 'verification_action', 'frame_id': '99', 'kind': 'key', 'key': 'Enter'}]
    assert report['status'] == 'verification_required' and report['verification_available'] is False
    assert vault.values == saved
    private_free(report); private_free(store.writes)
    checking.finish()


def test_image_reads_match_current_version_atomically_without_retaining_old_images(service_factory):
    runner = FakeRunner()
    service, _, _ = service_factory(runner)
    start(service, runner)
    runner.callback(qr_message())
    stamp = service.status()['qr_updated_at']
    assert service.qr_image() == service.qr_image(updated_at=stamp) == qr_png()
    assert service.qr_image(updated_at='stale-version') is None
    expected = show_verification(runner, '1')
    first = service.status()['verification_frame_id']
    assert service.verification_image() == service.verification_image(frame_id=first) == expected
    assert service.verification_image(frame_id='stale-version') is None
    runner.callback(verification_message('2'))
    current = service.status()['verification_frame_id']
    assert current != first and service.verification_image(frame_id=first) is None
    assert service.verification_image(frame_id=current) == expected
    service.clear()
    assert service.qr_image(updated_at=stamp) is None
    assert service.verification_image(frame_id=current) is None
    runner.finish()


def test_verification_latch_rejects_stale_scan_check_and_qr_until_final_result(service_factory):
    runner = FakeRunner()
    service, store, _ = service_factory(runner)
    start(service, runner); runner.callback(qr_message())
    assert service.status()['qr_available'] is True
    expected = show_verification(runner)
    identity = service.status()['verification_frame_id']
    writes = len(store.writes)
    for phase in ('opening', 'checking', 'waiting_scan', 'waiting_confirmation', 'waiting_browser'):
        runner.callback({'type': 'progress', 'phase': phase})
        runner.callback(qr_message())
        assert service.status()['status'] == 'verification_required'
        assert service.status()['qr_available'] is False and service.qr_image() is None
        assert service.status()['verification_frame_id'] == identity
        assert service.verification_image() == expected
    assert len(store.writes) == writes
    assert service.check_login()['status'] == 'verification_required'
    runner.callback({'type': 'verification_clear'})
    assert service.verification_image() is None
    runner.callback({'type': 'progress', 'phase': 'checking'})
    assert service.status()['status'] == 'verification_required'
    runner.callback(verification_message('2'))
    assert service.status()['verification_available'] is True
    runner.finish()
    assert service.status()['status'] == 'logged_in'
    assert service.status()['verification_available'] is False
    assert service.status()['qr_available'] is False
    assert all('verification_started' not in item for item in store.writes)


def test_verification_latch_does_not_transfer_to_a_new_login(service_factory):
    old, new = FakeRunner(), FakeRunner()
    service, _, _ = service_factory(old, new)
    start(service, old); show_verification(old)
    start(service, new)
    new.callback({'type': 'progress', 'phase': 'waiting_scan'})
    new.callback(qr_message())
    assert service.status()['status'] == 'waiting_scan' and service.status()['qr_available'] is True
    old.callback({'type': 'progress', 'phase': 'verification_required'})
    assert service.status()['status'] == 'waiting_scan'
    new.finish(); old.finish()


def test_waiting_browser_uses_private_user_view_without_claiming_mfa(service_factory):
    runner = FakeRunner()
    service, store, vault = service_factory(runner)
    start(service, runner)
    runner.callback(qr_message())
    runner.callback({'type': 'progress', 'phase': 'waiting_browser', 'account': PRIVATE})
    assert service.status()['status'] == 'waiting_browser'
    assert service.status()['qr_available'] is False and service.status()['browser_open'] is True
    assert service.active['verification_started'] is False
    writes, encrypted = len(store.writes), copy.deepcopy(vault.values)
    message = verification_message('1')
    runner.callback(message)
    identity = service.status()['verification_frame_id']
    assert service.status()['verification_available'] is True
    assert service.verification_image(frame_id=identity) == base64.b64decode(message['image_base64'])
    assert service.verification_image(frame_id='stale-image') is None
    assert service.check_login()['status'] == 'waiting_browser'
    assert runner.commands == [{'type': 'check'}]
    result = service.verification_action({'frame_id': identity, 'kind': 'text', 'text': SECRET})
    assert result['status'] == 'waiting_browser' and result['verification_available'] is False
    assert runner.commands[-1] == {'type': 'verification_action', 'frame_id': '1', 'kind': 'text', 'text': SECRET}
    with pytest.raises(AppError) as stale:
        service.verification_action({'frame_id': identity, 'kind': 'refresh'})
    assert stale.value.status == 409
    assert len(store.writes) == writes and vault.values == encrypted
    private_free(service.status()); private_free(store.writes)
    assert all(not any(key.startswith('verification_') for key in item) for item in store.writes)
    runner.finish()
    assert service.status()['status'] == 'logged_in' and service.verification_image() is None


def test_waiting_browser_phase_changes_consume_old_frames_and_mfa_cannot_downgrade(service_factory):
    runner = FakeRunner()
    service, _, _ = service_factory(runner)
    start(service, runner)
    runner.callback({'type': 'progress', 'phase': 'waiting_browser'})
    runner.callback(verification_message('1'))
    first = service.status()['verification_frame_id']
    runner.callback({'type': 'progress', 'phase': 'waiting_scan'})
    assert service.active['verification_started'] is False
    assert service.verification_image(frame_id=first) is None
    with pytest.raises(AppError) as stale:
        service.verification_action({'frame_id': first, 'kind': 'click', 'x': 1, 'y': 1})
    assert stale.value.status == 409
    runner.callback(qr_message())
    assert service.status()['qr_available'] is True
    runner.callback({'type': 'progress', 'phase': 'waiting_browser'})
    runner.callback(verification_message('2'))
    second = service.status()['verification_frame_id']
    runner.callback({'type': 'progress', 'phase': 'verification_required'})
    assert service.verification_image(frame_id=second) is None
    runner.callback(verification_message('3'))
    mfa = service.status()
    runner.callback({'type': 'progress', 'phase': 'waiting_browser'})
    runner.callback(qr_message())
    assert service.status() == mfa and service.active['verification_started'] is True
    assert service.status()['status'] == 'verification_required' and service.status()['qr_available'] is False
    runner.finish()


def test_waiting_browser_late_frames_cannot_affect_another_login_generation(service_factory):
    old, new = FakeRunner(), FakeRunner()
    service, _, _ = service_factory(old, new)
    start(service, old)
    old.callback({'type': 'progress', 'phase': 'waiting_browser'})
    old.callback(verification_message('1'))
    prior = service.status()['verification_frame_id']
    start(service, new)
    new.callback({'type': 'progress', 'phase': 'waiting_browser'})
    new.callback(verification_message('1'))
    current = service.status()
    assert current['verification_frame_id'] != prior
    old.callback({'type': 'progress', 'phase': 'waiting_browser'})
    old.callback({'type': 'verification_clear'})
    old.callback(verification_message('2'))
    assert service.status() == current
    with pytest.raises(AppError) as stale:
        service.verification_action({'frame_id': prior, 'kind': 'refresh'})
    assert stale.value.status == 409 and new.commands == []
    new.finish(); old.finish()


def test_unknown_browser_prompt_timeout_does_not_mark_saved_session_expired(service_factory):
    initial = FakeRunner()
    prompt = FakeRunner({'ok': False, 'error': {'code': 'douyin_session_timeout'}})
    service, _, vault = service_factory(initial, prompt)
    start(service, initial); initial.finish()
    encrypted = copy.deepcopy(vault.values)
    start(service, prompt)
    prompt.callback({'type': 'progress', 'phase': 'waiting_browser'})
    assert service.active['verification_started'] is False
    prompt.finish()
    assert service.status()['status'] == 'failed'
    assert service.status()['error']['code'] == 'douyin_session_timeout'
    assert service.storage_state() is not None and vault.values == encrypted
