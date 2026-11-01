"""Session and initial-navigation contracts, with no external network or account."""
import io
import json
import subprocess
import shutil

import httpx
import pytest

import backend.browser_capture as capture
from backend.security import AppError

SHORT = 'https://v.douyin.com/synthetic-share/'
VIDEO = '1234567890123456789'
PAGE = 'https://www.douyin.com/video/' + VIDEO
STATE = {'cookies': [{'name': 'sessionid', 'value': 'synthetic-auth-session', 'domain': '.douyin.com', 'path': '/',
    'expires': -1, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}], 'origins': []}


def test_state_is_copied_and_restricted_to_official_platform():
    state = capture._browser_state(STATE)
    assert state == STATE and state is not STATE and state['cookies'] is not STATE['cookies']


@pytest.mark.parametrize('state', ['synthetic/profile/path', {'cookies': [], 'origins': [{'origin': 'https://contract.invalid'}]},
    {'cookies': [{'domain': '.contract.invalid', 'value': 'synthetic-secret'}]}, {'cookies': 'synthetic-secret'},
    {'cookies': [], 'origins': [], 'extra': 'synthetic-' + 'x' * 270000}])
def test_invalid_or_foreign_state_never_leaks_value(state):
    with pytest.raises(AppError) as error: capture._browser_state(state)
    assert error.value.code == 'douyin_session_invalid' and 'synthetic' not in error.value.message


@pytest.mark.parametrize('chain,result_id,accepted', [
    ([SHORT, PAGE], VIDEO, True), ([SHORT, 'https://www.iesdouyin.com/share/video/' + VIDEO + '/'], VIDEO, True),
    ([SHORT, PAGE], '9999999999999999999', False), ([SHORT, PAGE, PAGE.replace(VIDEO, '9999999999999999999')], VIDEO, False),
    ([SHORT, 'https://www.douyin.com/?modal_id=' + VIDEO], VIDEO, False),
    ([SHORT, 'https://contract.invalid/video/' + VIDEO], VIDEO, False),
    ([SHORT, PAGE.replace('https:', 'http:')], VIDEO, False), ([PAGE], VIDEO, False),
    ([SHORT] * 6 + [PAGE], VIDEO, False),
])
def test_resolution_only_accepts_initial_official_http_chain_and_unique_numeric_path(chain, result_id, accepted):
    message = {'redirect_chain': chain, 'video_id': result_id, 'resolved_url': chain[-1]}
    if not accepted:
        with pytest.raises(AppError): capture._resolved_redirect(message, SHORT)
    else:
        result = capture._resolved_redirect(message, SHORT)
        assert result['video_id'] == VIDEO and result['method'] == 'official_browser_redirect'


class MemoryPipe(io.StringIO):
    def close(self): pass


class Driver:
    pid = 999999999
    def __init__(self, message):
        self.stdin = MemoryPipe(); self.stdout = MemoryPipe(json.dumps(message) + '\n'); self.returncode = None
    def poll(self): return self.returncode
    def wait(self, timeout=None): self.returncode = 0; return 0


def test_resolver_reuses_state_but_returns_no_cookie_or_media_even_on_final_403(tmp_path, monkeypatch):
    driver = Driver({'type': 'result', 'ok': True, 'redirect_chain': [SHORT, PAGE], 'resolved_url': PAGE,
        'video_id': VIDEO, 'http_status': 403})
    monkeypatch.setattr(capture.subprocess, 'Popen', lambda *a, **kw: driver)
    monkeypatch.setattr(capture, '_kill', lambda process: None)
    def no_http(request): pytest.fail('resolve-only must never download media or call Python HTTP')
    result = capture.resolve_douyin_url(SHORT, tmp_path, storage_state=STATE,
        transport_factory=lambda: httpx.MockTransport(no_http))
    start = json.loads(driver.stdin.getvalue().splitlines()[0])
    assert start['resolve_only'] and start['storage_state'] == STATE
    assert result == {'video_id': VIDEO, 'resolved_url': PAGE, 'method': 'official_browser_redirect',
        'redirect_count': 1, 'session_used': True, 'http_status': 403}
    assert 'synthetic-auth' not in json.dumps(result) and not list(tmp_path.glob('browser-*'))


def test_capture_seeds_session_redaction_before_any_cookie_header_is_seen(tmp_path, monkeypatch):
    seen = []
    original = capture._Broker
    class Spy(original):
        def close(self):
            seen.append(self.public_title('source synthetic-auth-session'))
            super().close()
    driver = Driver({'type': 'result', 'ok': False, 'code': 'douyin_browser_material'})
    monkeypatch.setattr(capture, '_Broker', Spy)
    monkeypatch.setattr(capture.subprocess, 'Popen', lambda *a, **kw: driver)
    monkeypatch.setattr(capture, '_kill', lambda process: None)
    with pytest.raises(AppError) as error:
        capture.capture_douyin(PAGE, tmp_path, video_id=VIDEO, storage_state=STATE,
            transport_factory=lambda: httpx.MockTransport(lambda request: pytest.fail('no external call')))
    assert seen == ['source [REDACTED]']
    assert 'synthetic-auth' not in json.dumps(error.value.capture_diagnostic)


def test_actual_node_redirect_validator_rejects_recommendations_and_cross_platform():
    script = """import fs from 'node:fs'; import vm from 'node:vm';
const source=fs.readFileSync('frontend/scripts/capture-douyin.mjs','utf8');
const platform=source.match(/function platformPage\\(url\\) \\{[\\s\\S]*?\\n\\}/)[0];
const redirect=source.match(/function officialRedirect\\(chain, initial\\) \\{[\\s\\S]*?\\n\\}/)[0];
const context=vm.createContext({URL}); vm.runInContext(platform+'\\n'+redirect,context);
const initial='https://v.douyin.com/synthetic/';
console.log(JSON.stringify([
 context.officialRedirect([initial,'https://www.douyin.com/video/123'],initial)?.video_id,
 context.officialRedirect([initial,'https://contract.invalid/video/123'],initial),
 context.officialRedirect([initial,'https://www.douyin.com/?modal_id=123'],initial),
 context.officialRedirect([initial,'https://www.douyin.com/video/123','https://www.douyin.com/video/456'],initial)
]));"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script],
        cwd=capture.PROJECT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout) == ['123', None, None, None]
