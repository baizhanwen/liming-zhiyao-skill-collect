"""Explicit offline browser/broker contracts; no supplier or model requests."""
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

import backend.browser_capture as capture
import backend.security as security
from backend.security import AppError

VIDEO_ID = '1234567890123456789'
PAGE = 'https://www.douyin.com/video/' + VIDEO_ID
MEDIA = 'https://cdn.contract.invalid/fixture.mp4?signature=synthetic-private-query&watermark=1'


def broker(tmp_path, handler, **kwargs):
    return capture._Broker(tmp_path, VIDEO_ID, time.monotonic() + 20,
                           transport_factory=lambda: httpx.MockTransport(handler),
                           media_inspector=kwargs.pop('media_inspector', lambda path: {'video': True, 'audio': False, 'duration': 1}), **kwargs)


def manifest(identity=VIDEO_ID):
    return {'aweme_detail': {'aweme_id': identity, 'video': {'play_addr': {'url_list': [MEDIA]}}}}


def request(url=MEDIA, **overrides):
    return {'id': 1, 'url': url, 'method': 'GET', 'headers': {}, 'resource_type': 'media', **overrides}


def test_complete_media_requires_matching_identity_and_hides_session(tmp_path):
    data = b'0000ftyp-explicit-synthetic-media'
    service = broker(tmp_path, lambda r: httpx.Response(200, content=data, headers={'Content-Type': 'video/mp4'}))
    try:
        service._bind_json(json.dumps(manifest('9999999999999999999')).encode(), PAGE)
        reply = service.handle(request(headers={'Cookie': 'visitor=synthetic-current-session'}))
        assert not reply['capture_ready'] and not service.complete()
        service._bind_json(json.dumps(manifest()).encode(), PAGE)
        results = service.finish([], VIDEO_ID)
        assert results[0]['sha256'] == hashlib.sha256(data).hexdigest()
        assert results[0]['coverage'] == [[0, len(data)]] and results[0]['complete']
        dumped = json.dumps(results)
        assert 'signature' not in dumped and 'synthetic-current-session' not in dumped
        assert service.public_title('Title synthetic-current-session ' + MEDIA) == 'Title [REDACTED] [链接]'
    finally:
        service.close()


def test_range_response_is_completed_using_exact_observed_url_and_session(tmp_path):
    data, seen = b'0123456789abcdefghij', []
    def origin(req):
        seen.append((str(req.url), req.headers.get('range')))
        assert req.headers.get('cookie') == 'visitor=synthetic-session'
        if req.headers.get('range', '').startswith('bytes=4-'):
            return httpx.Response(206, content=data[4:], headers={'Content-Type': 'video/mp4', 'Content-Range': 'bytes 4-19/20'})
        return httpx.Response(206, content=data[:4], headers={'Content-Type': 'video/mp4', 'Content-Range': 'bytes 0-3/20'})
    service = broker(tmp_path, origin)
    try:
        service._bind_json(json.dumps(manifest()).encode(), PAGE)
        reply = service.handle(request(headers={'Range': 'bytes=0-3', 'Cookie': 'visitor=synthetic-session'}))
        assert reply['capture_ready']
        result = service.finish([], VIDEO_ID)[0]
        assert Path(result['path']).read_bytes() == data and result['coverage'] == [[0, 20]]
        assert all(url == MEDIA for url, _ in seen)
        assert seen == [(MEDIA, 'bytes=0-3'), (MEDIA, None), (MEDIA, 'bytes=4-19')]
    finally:
        service.close()


def test_changed_range_and_wrong_page_never_return_success(tmp_path):
    service = broker(tmp_path, lambda r: httpx.Response(404))
    try:
        service._bind_json(json.dumps(manifest()).encode(), PAGE)
        service._record(MEDIA, {}, 206, {'content-range': 'bytes 0-3/8'}, b'abcd', 'video/mp4')
        with pytest.raises(AppError):
            service._record(MEDIA, {}, 206, {'content-range': 'bytes 2-5/8'}, b'ZZef', 'video/mp4')
        assert not service.complete()
        with pytest.raises(AppError) as error:
            service.finish([{'src': MEDIA}], '9999999999999999999')
        assert error.value.code == 'douyin_browser_identity'
    finally:
        service.close()


def test_browser_request_private_redirect_is_denied_before_connection(tmp_path, monkeypatch):
    calls = []
    def check(url):
        if urlsplit(url).hostname == 'private.invalid':
            raise AppError('unsafe_url', 'private target rejected')
        return ['93.184.216.34']
    from urllib.parse import urlsplit
    monkeypatch.setattr(security, 'public_url', check)
    def make_transport():
        t = security.PublicTransport(); t.transport.close()
        t.transport = httpx.MockTransport(lambda req: calls.append(str(req.url)) or httpx.Response(302, headers={'Location': 'https://private.invalid/'}))
        return t
    service = capture._Broker(tmp_path, VIDEO_ID, time.monotonic() + 10, transport_factory=make_transport)
    try:
        initial = service.handle(request(PAGE, navigation=True))
        assert initial['status'] == 200 and initial['redirect'] == 'https://private.invalid/'
        assert service.handle(request('https://private.invalid/'))['abort']
        assert len(calls) == 1 and service.errors == {'unsafe_url': 1}
    finally:
        service.close()


def test_broker_decodes_compression_headers_and_keeps_visitor_cookies_internal(tmp_path):
    import gzip
    body = b'normal public page response'
    service = broker(tmp_path, lambda r: httpx.Response(200, content=gzip.compress(body), headers=[
        ('Content-Type', 'text/html'), ('Content-Encoding', 'gzip'),
        ('Set-Cookie', 'visitor=synthetic-one; Path=/'), ('Set-Cookie', 'session=synthetic-two; Path=/')]))
    try:
        result = service.handle(request(PAGE, resource_type='document'))
        import base64
        assert base64.b64decode(result['body']) == body
        assert 'content-encoding' not in result['headers'] and 'content-length' not in result['headers']
        assert result['headers']['set-cookie'].count('\n') == 1
        assert service.public_title('synthetic-one synthetic-two') == '[REDACTED] [REDACTED]'
        assert not list(tmp_path.iterdir()), 'HTML, signed URLs and cookie headers are not persisted'
    finally:
        service.close()


def test_budget_and_cancellation_abort_without_sensitive_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, 'MAX_RESOURCE', 4)
    service = broker(tmp_path, lambda r: httpx.Response(200, content=b'too-long-secret'), cancelled=lambda: False)
    try:
        result = service.handle(request(PAGE, resource_type='script'))
        assert result == {'type': 'reply', 'id': 1, 'abort': True}
        assert service.errors == {'douyin_browser_budget': 1}
        service.cancelled = lambda: True
        assert service.handle(request(PAGE))['abort']
        assert service.errors['cancelled'] == 1
    finally:
        service.close()


def test_native_observation_needs_official_metadata_before_downloading(tmp_path):
    seen = []
    binary = b'0000ftyp-synthetic-bound-native-media'
    service = broker(tmp_path, lambda req: seen.append(str(req.url)) or httpx.Response(200,
        content=binary, headers={'Content-Type': 'video/mp4'}))
    try:
        response = service.handle({'type': 'observed_media', 'id': 1, 'url': MEDIA,
                                   'headers': {'Cookie': 'visitor=synthetic-native'}})
        assert not response['capture_ready'] and not seen
        for source, expected in [('https://ad.invalid/api', False), (PAGE, True)]:
            metadata = tmp_path / ('metadata-' + str(expected) + '.bin')
            metadata.write_text(json.dumps(manifest()))
            result = service.handle({'type': 'observed_metadata', 'id': 2, 'url': source,
                                     'content_type': 'application/json', 'path': str(metadata)})
            assert result['capture_ready'] == expected
            assert not metadata.exists()
        assert seen == [MEDIA]
        output = service.finish([], VIDEO_ID)
        assert output[0]['sha256'] == hashlib.sha256(binary).hexdigest()
        assert 'signature' not in json.dumps(output)
    finally:
        service.close()


def test_native_metadata_cache_cannot_read_outside_capture_root(tmp_path):
    service = broker(tmp_path / 'capture', lambda req: pytest.fail('no network expected'))
    outside = tmp_path / 'must-not-read.json'; outside.write_text(json.dumps(manifest()))
    try:
        result = service.handle({'type': 'observed_metadata', 'id': 3, 'url': PAGE,
                                 'content_type': 'application/json', 'path': str(outside)})
        assert result['abort'] and outside.is_file() and not service.urls
    finally:
        service.close()


def test_published_protocol_relative_media_is_resolved_as_https_without_query_changes(tmp_path):
    service = broker(tmp_path, lambda req: httpx.Response(404))
    try:
        value = MEDIA.removeprefix('https:')
        data = {'aweme_detail': {'aweme_id': VIDEO_ID, 'video': {'play_addr': {'url_list': [value]}}}}
        service._bind_json(json.dumps(data).encode(), PAGE)
        assert service._bound(MEDIA) and service.urls == {MEDIA}
    finally:
        service.close()


def test_current_item_embedded_video_model_json_attests_published_play_url(tmp_path):
    service = broker(tmp_path, lambda req: httpx.Response(404))
    try:
        data = {'aweme_detail': {'aweme_id': VIDEO_ID, 'video': {
            'video_model': json.dumps({'PlayInfoList': [{'MainPlayUrl': MEDIA}]})}}}
        service._bind_json(json.dumps(data).encode(), PAGE)
        assert service._bound(MEDIA)
    finally:
        service.close()


def test_actual_native_redirect_chain_carries_official_identity_to_final_cdn(tmp_path):
    final = 'https://final-cdn.contract.invalid/file.mp4?signature=synthetic-final'
    binary = b'0000ftyp-native-redirect-contract'
    service = broker(tmp_path, lambda req: httpx.Response(200, content=binary, headers={'Content-Type': 'video/mp4'}))
    try:
        # Both redirect and media may be observed before the API metadata finishes.
        service.handle({'type': 'observed_redirect', 'id': 1, 'url': MEDIA, 'location': final})
        service.handle({'type': 'observed_media', 'id': 2, 'url': final, 'headers': {}})
        assert not service.ready()
        path = tmp_path / 'metadata-later.bin'; path.write_text(json.dumps(manifest()))
        reply = service.handle({'type': 'observed_metadata', 'id': 3, 'url': PAGE,
                                'content_type': 'application/json', 'path': str(path)})
        assert reply['capture_ready'] and service._bound(final)
        assert service.finish([], VIDEO_ID)[0]['sha256'] == hashlib.sha256(binary).hexdigest()
        assert not service._bound('https://unrelated.invalid/file.mp4')
    finally:
        service.close()


def test_video_mime_audio_only_cannot_finish_and_declared_split_audio_is_required(tmp_path):
    audio = MEDIA + '&track=audio'
    current = {'video': False, 'audio': True, 'duration': 144.9}
    service = broker(tmp_path, lambda req: httpx.Response(200, content=b'fixture',
        headers={'Content-Type': 'video/mp4'}), media_inspector=lambda path: dict(current))
    try:
        service.urls.add(MEDIA)
        assert not service.handle(request())['capture_ready']
        assert service.finish([], VIDEO_ID) == []
        # A separate true video is ready only after the declared audio stream exists.
        service.expected_audio = True
        service.urls.add(audio)
        current.update(video=True, audio=False)
        assert service.handle(request(audio))['capture_ready']
        files = service.finish([], VIDEO_ID)
        assert len(files) == 2 and any(item['probe']['audio'] for item in files)
        assert any(item['probe']['video'] for item in files)
    finally:
        service.close()


def test_script_runtime_and_fail_closed_structure():
    text = capture.SCRIPT.read_text()
    assert 'launchPersistentContext' not in text
    assert 'if (options.native_proxy)' in text and 'return await route.continue();' in text
    assert "serviceWorkers: 'block'" in text and 'routeWebSocket' in text
    assert 'chromiumSandbox: true' in text and 'disable_non_proxied_udp' in text
    assert "bypass: '<-loopback>'" in text and 'MAP * ~NOTFOUND' in text
    assert 'ok: ready &&' in text


def test_real_project_renderer_with_explicit_offline_public_fixture(tmp_path):
    """Actual project Chromium/JS/RPC; all HTTP is the synthetic in-process origin."""
    node = shutil.which('node')
    if not node or not shutil.which('ffmpeg'):
        pytest.skip('requires project Node/Chromium and optional FFmpeg')
    runtime = subprocess.run([node, str(capture.SCRIPT), '--check-runtime'], capture_output=True, text=True, timeout=10)
    if runtime.returncode:
        pytest.skip('project Chromium has not been installed')
    clip = tmp_path / 'self-authored.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'color=c=blue:s=160x120:r=10', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000',
        '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(clip)], check=True, timeout=20)
    binary = clip.read_bytes()
    seen = []
    def origin(req):
        seen.append((req.url.host, req.url.path, req.headers.get('cookie')))
        if req.url.host == 'v.douyin.com':
            return httpx.Response(302, headers={'Location': PAGE})
        if req.url.path == '/video/' + VIDEO_ID:
            assert 'cookie' not in req.headers, 'a new session starts without existing cookies'
            return httpx.Response(200, text='<title>Explicit offline fixture</title><video></video>'
                '<script>fetch("/fixture-metadata").then(r=>r.json()).then(d=>{'
                'document.querySelector("video").src=d.aweme_detail.video.play_addr.url_list[0];});</script>',
                headers={'Content-Type': 'text/html', 'Set-Cookie': 'visitor=synthetic-fixture; Path=/'})
        if req.url.path == '/fixture-metadata':
            assert 'synthetic-fixture' in req.headers.get('cookie', '')
            return httpx.Response(200, json=manifest())
        if req.url.host == 'cdn.contract.invalid':
            return httpx.Response(302, headers={'Location': 'https://media-final.contract.invalid/actual.mp4?signature=synthetic-final'})
        if req.url.host == 'media-final.contract.invalid':
            return httpx.Response(200, content=binary, headers={'Content-Type': 'video/mp4'})
        return httpx.Response(404)
    result = capture.capture_douyin('https://v.douyin.com/synthetic', tmp_path / 'capture', video_id=VIDEO_ID, timeout=12,
                                   transport_factory=lambda: httpx.MockTransport(origin))
    assert result['observed_video_id'] == VIDEO_ID and result['acquisition']['method'] == 'project_browser'
    assert result['media_files'][0]['sha256'] == hashlib.sha256(binary).hexdigest()
    assert result['media_files'][0]['coverage'] == [[0, len(binary)]]
    assert Path(result['media_paths'][0]).is_file()
    assert 'signature' not in json.dumps(result) and 'synthetic-fixture' not in json.dumps(result)
    assert {host for host, _, _ in seen} == {'v.douyin.com', 'www.douyin.com', 'cdn.contract.invalid', 'media-final.contract.invalid'}
