"""Anonymous share-resolution contracts. All HTTP/process/browser peers are fake."""
import io
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

import backend.browser_capture as capture
import backend.media as media
from backend.security import AppError


SHORT = 'https://v.douyin.com/explicit-share/'
VIDEO = '1234567890123456789'
PAGE = 'https://www.douyin.com/video/' + VIDEO
PRIVATE = 'EXPLICIT_PRIVATE_SENTINEL'


@pytest.mark.parametrize('chain,identity,accepted', [
    ([SHORT, PAGE], VIDEO, True),
    ([SHORT, 'https://www.iesdouyin.com/share/video/' + VIDEO + '/'], VIDEO, True),
    ([SHORT, PAGE], '999', False),
    ([SHORT, PAGE, PAGE.replace(VIDEO, '999')], VIDEO, False),
    ([SHORT, 'https://www.douyin.com/?modal_id=' + VIDEO], VIDEO, False),
    ([SHORT, 'https://outside.invalid/video/' + VIDEO], VIDEO, False),
    ([SHORT, PAGE.replace('https:', 'http:')], VIDEO, False),
    ([SHORT, PAGE.replace('www.', 'user:private@www.')], VIDEO, False),
    ([SHORT, PAGE.replace('.com/', '.com:8443/')], VIDEO, False),
    ([PAGE], VIDEO, False),
    ([SHORT] * 6 + [PAGE], VIDEO, False),
    ([SHORT, PAGE.replace(VIDEO, '１２３')], '１２３', False),
    ([SHORT, PAGE.replace(VIDEO, '1' * 33)], '1' * 33, False),
], ids=['official', 'official-share', 'wrong-id', 'conflicting-id', 'modal-only', 'foreign',
    'http', 'userinfo', 'port', 'different-start', 'too-many-hops', 'unicode-id', 'long-id'])
def test_only_initial_official_redirect_can_establish_identity(chain, identity, accepted):
    message = {'redirect_chain': chain, 'resolved_url': chain[-1], 'video_id': identity}
    if accepted:
        assert capture._resolved_redirect(message, SHORT)['video_id'] == VIDEO
    else:
        with pytest.raises(AppError) as error:
            capture._resolved_redirect(message, SHORT)
        assert error.value.code == 'douyin_resolve_identity'


class MemoryPipe(io.StringIO):
    def close(self):
        pass


class Driver:
    pid = 999999999

    def __init__(self, messages):
        self.stdin = MemoryPipe()
        self.stdout = MemoryPipe(''.join(json.dumps(message) + '\n' for message in messages))
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


def test_resolve_only_aborts_broker_material_and_returns_no_media_or_account_state(tmp_path, monkeypatch):
    driver = Driver([
        {'type': 'observed_media', 'id': 1, 'url': 'https://media.invalid/' + PRIVATE},
        {'type': 'progress', 'response_status_counts': {'received': 2}, 'unknown': PRIVATE},
        {'type': 'result', 'ok': True, 'redirect_chain': [SHORT, PAGE], 'resolved_url': PAGE,
            'video_id': VIDEO, 'http_status': 403, 'title': PRIVATE},
    ])
    monkeypatch.setattr(capture.subprocess, 'Popen', lambda *a, **kw: driver)
    monkeypatch.setattr(capture, '_kill', lambda process: None)
    monkeypatch.setattr(capture.shutil, 'which', lambda name: 'EXPLICIT_FAKE_NODE')
    monkeypatch.setattr(capture._Broker, 'handle', lambda *_a: pytest.fail('No broker fetch in resolve-only'))
    monkeypatch.setattr(capture._Broker, 'finish', lambda *_a: pytest.fail('No media selection in resolve-only'))
    events = []
    result = capture.resolve_douyin_url(SHORT, tmp_path, progress_callback=events.append,
        transport_factory=lambda: httpx.MockTransport(lambda request: pytest.fail('No HTTP call')))
    commands = [json.loads(line) for line in driver.stdin.getvalue().splitlines()]
    assert commands[0]['resolve_only'] is True and 'storage_state' not in commands[0]
    assert any(message.get('id') == 1 and message.get('abort') is True for message in commands)
    assert result == {'video_id': VIDEO, 'resolved_url': PAGE, 'method': 'official_browser_redirect',
        'redirect_count': 1, 'http_status': 403}
    assert events == [{'phase': 'resolving_share', 'requests': 2}]
    assert PRIVATE not in json.dumps(result) and 'media_path' not in result
    assert not list(tmp_path.rglob('*.mp4'))


@pytest.mark.parametrize('url', [
    'http://v.douyin.com/share/', 'https://outside.invalid/video/123',
    'https://user:private@v.douyin.com/share/', 'https://v.douyin.com:8443/share/',
])
def test_invalid_initial_source_never_starts_browser(tmp_path, monkeypatch, url):
    monkeypatch.setattr(capture.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Invalid initial URL'))
    with pytest.raises(AppError) as error:
        capture.resolve_douyin_url(url, tmp_path)
    assert error.value.code == 'unsupported_source'


def test_cancelled_resolution_never_starts_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Cancelled before browser'))
    with pytest.raises(AppError) as error:
        capture.resolve_douyin_url(SHORT, tmp_path, cancelled=lambda: True)
    assert error.value.code == 'cancelled'


def fail_static(*_a, **_k):
    raise AppError('douyin_fetch', 'Explicit simulated static fetch failure')


def test_static_failure_resolves_then_captures_the_same_id_without_account_state(tmp_path, monkeypatch):
    monkeypatch.setattr(media, 'fetch_public', fail_static)
    calls, events = [], []

    def resolve(url, folder, **options):
        calls.append(('resolve', url, options))
        assert url == SHORT and 'storage_state' not in options
        return {'video_id': VIDEO, 'resolved_url': PAGE, 'method': 'official_browser_redirect'}

    def acquire(url, folder, video_id, **options):
        calls.append(('capture', url, options))
        assert url == SHORT and video_id == VIDEO and 'storage_state' not in options
        return {'platform': 'douyin', 'video_id': video_id, 'explicit_mock_material': True}

    monkeypatch.setattr(capture, 'resolve_douyin_url', resolve)
    monkeypatch.setattr(media, '_browser_douyin_source', acquire)
    result = media.douyin_source(SHORT, tmp_path, progress_callback=events.append)
    assert result['video_id'] == VIDEO and [item[0] for item in calls] == ['resolve', 'capture']
    assert [event for event in events if event['phase'] == 'resolving_share'] == [
        {'phase': 'resolving_share', 'completed': 0, 'total': 1},
        {'phase': 'resolving_share', 'completed': 1, 'total': 1}]


@pytest.mark.parametrize('resolved', [
    {'video_id': VIDEO, 'resolved_url': 'https://outside.invalid/video/' + VIDEO, 'method': 'official_browser_redirect'},
    {'video_id': VIDEO, 'resolved_url': PAGE, 'method': 'dom_recommendation'},
    {'video_id': '１２３', 'resolved_url': PAGE, 'method': 'official_browser_redirect'},
    {'video_id': VIDEO, 'resolved_url': PAGE.replace(VIDEO, '999'), 'method': 'official_browser_redirect'},
], ids=['foreign', 'dom-recommendation', 'unicode', 'wrong-target'])
def test_resolved_identity_is_revalidated_before_capture(tmp_path, monkeypatch, resolved):
    monkeypatch.setattr(media, 'fetch_public', fail_static)
    monkeypatch.setattr(capture, 'resolve_douyin_url', lambda *_a, **_k: resolved)
    monkeypatch.setattr(media, '_browser_douyin_source', lambda *_a, **_k: pytest.fail('No target established'))
    with pytest.raises(AppError) as error:
        media.douyin_source(SHORT, tmp_path)
    assert error.value.code == 'douyin_browser_identity'


def test_known_id_does_not_resolve_again_and_cancel_is_not_anonymous_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(media, 'fetch_public', fail_static)
    monkeypatch.setattr(capture, 'resolve_douyin_url', lambda *_a, **_k: pytest.fail('ID already known'))
    monkeypatch.setattr(media, '_browser_douyin_source', lambda url, folder, video_id, **_k: {'video_id': video_id})
    assert media.douyin_source(PAGE, tmp_path) == {'video_id': VIDEO}

    def cancelled(*_a, **_k):
        raise AppError('cancelled', 'Explicit cancellation')

    monkeypatch.setattr(media, 'fetch_public', cancelled)
    with pytest.raises(AppError) as error:
        media.douyin_source(SHORT, tmp_path)
    assert error.value.code == 'cancelled'


@pytest.mark.parametrize('scenario', ['normal', 'foreign', 'recommendation', 'cancelled'])
def test_actual_node_resolve_only_blocks_subresources_recommendations_and_cancel(scenario):
    script = """import fs from 'node:fs';import vm from 'node:vm';
const source=fs.readFileSync('frontend/scripts/capture-douyin.mjs','utf8');
const names=['platformPage','officialRedirect','httpChain','resolveOnly'];
const code=names.map(name=>source.match(new RegExp('(?:async )?function '+name+'\\\\([\\\\s\\\\S]*?\\\\n}'))?.[0]);
if(code.some(x=>!x))throw Error('Required production helper missing');
let handler,respond;const actions=[],events=[],frame={};
const context=vm.createContext({URL,Date,cancelled:process.argv[1]==='cancelled',send:x=>events.push(x)});
vm.runInContext(code.join('\\n'),context);
const initial='https://v.douyin.com/explicit-share/',target='https://www.douyin.com/video/123';
const request=(url,previous=null,navigation=true)=>({url:()=>url,redirectedFrom:()=>previous,isNavigationRequest:()=>navigation,frame:()=>frame});
const run=async req=>{let allowed=false;await handler({request:()=>req,abort:async()=>actions.push('abort'),continue:async()=>{allowed=true;actions.push('continue');}});return allowed;};
const page={mainFrame:()=>frame,on:(name,fn)=>{if(name==='response')respond=fn;},goto:async(url,options)=>{
if(options.waitUntil!=='commit')throw Error('Must not render recommendation content');
const start=request(initial);if(!await run(start))return;
const end=request(process.argv[1]==='foreign'?'https://outside.invalid/video/123':target,start);
if(!await run(end))return;
respond({request:()=>end,status:()=>403,body:()=>{throw Error('No response body or media reads');}});
await run(request('https://media.invalid/video.mp4',null,false));
if(process.argv[1]==='recommendation')await run(request('https://www.douyin.com/video/999'));
},title:()=>{throw Error('No DOM title identity');},evaluate:()=>{throw Error('No hidden or recommended state');}};
const result=await context.resolveOnly(page,{route:async(_,fn)=>{handler=fn;}},{url:initial,native_proxy:true},Date.now()+1000);
console.log(JSON.stringify({result,actions,events}));"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, scenario],
        cwd=capture.PROJECT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and not result.stderr
    report = json.loads(result.stdout)
    assert report['actions'].count('continue') == (2 if scenario in ('normal', 'recommendation') else 1 if scenario == 'foreign' else 0)
    if scenario == 'normal':
        assert report['result']['ok'] is True and report['result']['video_id'] == '123'
        assert report['actions'][-1] == 'abort'  # media request was never followed
    else:
        assert report['result']['ok'] is False
        assert report['result']['code'] == ('cancelled' if scenario == 'cancelled' else 'douyin_resolve_identity')
