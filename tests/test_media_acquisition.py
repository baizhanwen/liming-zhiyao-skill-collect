"""Offline acquisition/security contracts; all sources and credentials are synthetic."""
import hashlib
import json
import socket
from urllib.parse import quote

import httpx
import pytest

import backend.media as media
import backend.security as security
from backend.security import AppError


HOST = 'public.contract.invalid'
VIDEO_ID = '1234567890123456789'
PAGE_URL = f'https://www.iesdouyin.com/share/video/{VIDEO_ID}/'
PUBLISHED_URL = 'https://media.contract.invalid/clip.mp4?watermark=1&signature=synthetic-only'


def dns_answer(**overrides):
    return {'Status': 0, 'TC': False, 'CD': False,
            'Question': [{'name': HOST + '.', 'type': 1}],
            'Answer': [{'name': HOST + '.', 'type': 1, 'TTL': 60, 'data': '93.184.216.34'}],
            **overrides}


def system_dns(monkeypatch, *ips):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in ips])


def test_fakeip_is_resolved_to_public_ip_without_weakening_guard(monkeypatch):
    system_dns(monkeypatch, '198.18.0.10', '198.19.1.9')
    calls = []
    monkeypatch.setattr(security, '_resolve_fakeip', lambda host: calls.append(host) or ['93.184.216.34'])
    assert security.public_url('https://' + HOST) == ['93.184.216.34']
    assert calls == [HOST]


@pytest.mark.parametrize('ips', [('127.0.0.1',), ('198.18.0.10', '10.0.0.1'),
                                ('198.18.0.10', '93.184.216.34'), ('169.254.169.254',), ('::1',)])
def test_other_private_or_mixed_dns_never_uses_doh(monkeypatch, ips):
    system_dns(monkeypatch, *ips)
    monkeypatch.setattr(security, '_resolve_fakeip', lambda host: pytest.fail('must not query DoH'))
    with pytest.raises(AppError) as error:
        security.public_url('https://' + HOST)
    assert error.value.code == 'unsafe_url'


def test_literal_fakeip_cannot_be_promoted_to_hostname_lookup(monkeypatch):
    system_dns(monkeypatch, '198.18.0.10')
    monkeypatch.setattr(security, '_resolve_fakeip', lambda host: pytest.fail('literal IP is forbidden'))
    with pytest.raises(AppError):
        security.public_url('https://198.18.0.10')


def test_fixed_doh_preserves_tls_host_sni_and_validates_cname_chain():
    def resolver(request):
        assert request.url.host == '8.8.8.8' and request.url.scheme == 'https'
        assert request.headers['host'] == 'dns.google'
        assert request.extensions['sni_hostname'] == 'dns.google'
        assert dict(request.url.params) == {'name': HOST, 'type': 'A'}
        assert 'cookie' not in request.headers
        assert request.extensions['timeout']['connect'] == 5
        return httpx.Response(200, json=dns_answer(Answer=[
            {'name': HOST, 'type': 5, 'TTL': 20, 'data': 'edge.contract.invalid.'},
            {'name': 'edge.contract.invalid.', 'type': 1, 'TTL': 30, 'data': '93.184.216.34'}]))
    assert security._resolve_fakeip(HOST, httpx.MockTransport(resolver)) == ['93.184.216.34']


@pytest.mark.parametrize('response', [
    dns_answer(Status=3), dns_answer(TC=True), dns_answer(CD=True), dns_answer(Answer=[]),
    dns_answer(Question=[{'name': 'other.contract.invalid', 'type': 1}]),
    dns_answer(Answer=[{'name': HOST, 'type': 1, 'data': '127.0.0.1'}]),
    dns_answer(Answer=[{'name': HOST, 'type': 1, 'data': '198.18.0.10'}]),
    dns_answer(Answer=[{'name': HOST, 'type': 1, 'data': '93.184.216.34'},
                       {'name': HOST, 'type': 1, 'data': '10.0.0.1'}]),
    dns_answer(Answer=[{'name': 'unrelated.invalid', 'type': 1, 'data': '93.184.216.34'}]),
    dns_answer(Answer=[{'name': HOST, 'type': 5, 'data': 'alias.invalid'},
                       {'name': 'alias.invalid', 'type': 5, 'data': HOST},
                       {'name': HOST, 'type': 1, 'data': '93.184.216.34'}]),
])
def test_invalid_doh_cannot_supply_an_address_or_response_text(response):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={**response, 'private_echo': 'never-display-this'}))
    with pytest.raises(AppError) as error:
        security._resolve_fakeip(HOST, transport)
    assert error.value.code == 'dns' and error.value.retryable
    assert 'never-display-this' not in error.value.message


def test_doh_does_not_follow_redirect_or_expose_location():
    calls = []
    def resolver(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={'Location': 'http://127.0.0.1/private?signature=synthetic'})
    with pytest.raises(AppError) as error:
        security._resolve_fakeip(HOST, httpx.MockTransport(resolver))
    assert calls == ['8.8.8.8'] and 'signature' not in error.value.message


@pytest.mark.parametrize('mode', ['oversized', 'timeout', 'malformed'])
def test_resolver_failures_are_bounded_and_sanitized(mode):
    def resolver(request):
        if mode == 'timeout':
            raise httpx.ReadTimeout('synthetic-private-echo', request=request)
        return httpx.Response(200, content=(b'x' * 65537 if mode == 'oversized' else b'not-json-synthetic-private-echo'))
    with pytest.raises(AppError) as error:
        security._resolve_fakeip(HOST, httpx.MockTransport(resolver))
    assert error.value.code == 'dns' and 'synthetic-private-echo' not in error.value.message


def test_doh_short_cache_expires_and_is_not_used_for_other_hosts(monkeypatch):
    calls = []
    def resolver(request):
        calls.append(request.url.params['name'])
        data = dns_answer(Question=[{'name': request.url.params['name'], 'type': 1}],
                          Answer=[{'name': request.url.params['name'], 'type': 1, 'TTL': 120, 'data': '93.184.216.34'}])
        return httpx.Response(200, json=data)
    monkeypatch.setattr(security.httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(resolver))
    security._DNS_CACHE.clear()
    try:
        assert security._resolve_fakeip(HOST) == security._resolve_fakeip(HOST)
        assert calls == [HOST]
        assert 0 < security._DNS_CACHE[HOST][0] - security.time.monotonic() <= 30
        security._DNS_CACHE[HOST] = (0, ('93.184.216.34',))
        security._resolve_fakeip(HOST)
        security._resolve_fakeip('second.contract.invalid')
        assert calls == [HOST, HOST, 'second.contract.invalid']
    finally:
        security._DNS_CACHE.clear()


def test_public_fetch_does_not_replay_set_cookie_on_redirect(monkeypatch):
    seen = []
    def origin(request):
        seen.append(request)
        assert 'cookie' not in request.headers
        if request.url.path == '/first':
            return httpx.Response(302, headers={'Set-Cookie': 'synthetic=session; Path=/', 'Location': '/second'})
        return httpx.Response(200, content=b'explicit contract')
    monkeypatch.setattr(security, 'PublicTransport', lambda: httpx.MockTransport(origin))
    assert security.fetch_public('https://' + HOST + '/first')[0] == b'explicit contract'
    assert len(seen) == 2


def test_redirect_destination_is_revalidated_before_connection(monkeypatch):
    def addresses(host, *args, **kwargs):
        ip = '93.184.216.34' if host == HOST else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', addresses)
    seen = []
    def origin(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={'Location': 'https://private.contract.invalid/secret'})
    transport = security.PublicTransport()
    transport.transport.close()
    transport.transport = httpx.MockTransport(origin)
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        with pytest.raises(AppError) as error:
            client.get('https://' + HOST)
    assert error.value.code == 'unsafe_url' and seen == ['93.184.216.34']


def router_page(items):
    return '<title>Explicit synthetic fixture</title><script>window._ROUTER_DATA = ' + json.dumps(
        {'loaderData': {'video_(id)/page': {'videoInfoRes': {'item_list': items}}}}) + ';</script>'


def test_parser_uses_requested_item_not_first_recommendation_and_preserves_url():
    page = router_page([
        {'aweme_id': '9999999999999999999', 'video': {'play_addr': {'url_list': ['https://wrong.invalid/other.mp4']}}},
        {'aweme_id': VIDEO_ID, 'video': {'play_addr': {'url_list': [PUBLISHED_URL]}}},
    ])
    assert media._published_media(page, VIDEO_ID) == [(PUBLISHED_URL, 'public_item_play_addr')]
    assert media._published_media(page, '8888888888888888888') == []


def test_percent_encoded_render_json_nested_bitrate_and_camelcase():
    data = {'detail': {'aweme_id': VIDEO_ID, 'video': {'bit_rate': [
        {'play_addr': {'url_list': [PUBLISHED_URL]}}], 'playAddr': PUBLISHED_URL}}}
    page = '<script id="RENDER_DATA" type="application/json">' + quote(json.dumps(data)) + '</script>'
    result = media._published_media(page, VIDEO_ID)
    assert len(result) == 1 and result[0][0] == PUBLISHED_URL


def test_html_media_requires_its_own_matching_identity():
    page = '<video src="https://wrong.invalid/unknown.mp4"></video>'
    page += '<video data-video-id="' + VIDEO_ID + '" src="' + PUBLISHED_URL + '"></video>'
    assert media._published_media(page, VIDEO_ID) == [(PUBLISHED_URL, 'public_video_element')]
    assert media._published_media('<meta property="og:video" content="' + PUBLISHED_URL + '">', VIDEO_ID) == []


def test_obtain_media_validates_actual_file_and_saves_hash_without_signed_url(tmp_path, monkeypatch):
    page = router_page([{'aweme_id': VIDEO_ID, 'video': {'play_api': PUBLISHED_URL}}]).encode()
    downloaded = b'explicit synthetic media bytes'
    seen = []
    def fetch(url, max_bytes=2_000_000, **kwargs):
        seen.append(url)
        assert 'iPhone' in kwargs['headers']['User-Agent']
        return (page, PAGE_URL, {}) if url == PAGE_URL else (downloaded, PUBLISHED_URL, {})
    monkeypatch.setattr(media, 'fetch_public', fetch)
    def probe(path):
        assert path.read_bytes() == downloaded
        return {'video': True, 'audio': True, 'duration': 144.799}
    monkeypatch.setattr(media, 'inspect_video', probe)
    result = media.douyin_source(PAGE_URL, tmp_path)
    assert seen == [PAGE_URL, PUBLISHED_URL]
    assert result['acquisition']['media_sha256'] == hashlib.sha256(downloaded).hexdigest()
    assert result['acquisition']['method'] == 'public_item_play_api'
    assert result['acquisition']['probe']['audio']
    assert 'signature' not in json.dumps(result) and PUBLISHED_URL not in json.dumps(result)
    assert (tmp_path / 'douyin.mp4').read_bytes() == downloaded
    assert not list(tmp_path.glob('*.part'))


def test_download_failure_is_sanitized_and_partial_file_removed(tmp_path, monkeypatch):
    page = router_page([{'aweme_id': VIDEO_ID, 'video': {'playAddr': PUBLISHED_URL}}]).encode()
    monkeypatch.setattr(media, 'fetch_public', lambda url, *args, **kwargs: (page, PAGE_URL, {}) if url == PAGE_URL else (b'<html>not video</html>', PUBLISHED_URL, {}))
    monkeypatch.setattr(media, 'inspect_video', lambda path: (_ for _ in ()).throw(RuntimeError(PUBLISHED_URL)))
    monkeypatch.setattr(media, '_browser_douyin_source', lambda *args, **kwargs: (_ for _ in ()).throw(
        AppError('douyin_browser_material', '[契约] 项目采集未取得原片')))
    with pytest.raises(AppError) as error:
        media.douyin_source(PAGE_URL, tmp_path)
    assert error.value.code == 'douyin_browser_material' and 'signature' not in error.value.message
    assert not (tmp_path / 'douyin.mp4').exists() and not list(tmp_path.glob('*.part'))


def test_static_summary_is_not_media_and_requires_product_browser(tmp_path, monkeypatch):
    page = ('<script>document.cookie="synthetic";</script><script>window._ROUTER_DATA=' +
            json.dumps({'loaderData': {'video_(id)/page': {'itemId': VIDEO_ID, 'serverToken': 'synthetic-public-value'}}}) +
            '</script><p>AI summary is not video</p>').encode()
    monkeypatch.setattr(media, 'fetch_public', lambda *args, **kwargs: (page, PAGE_URL, {}))
    monkeypatch.setattr(media, 'inspect_video', lambda path: pytest.fail('no actual media to inspect'))
    called=[]
    def unavailable(*args,**kwargs):
        called.append(args[2])
        raise AppError('douyin_browser_material','[契约] 独立采集器未取得原片')
    monkeypatch.setattr(media, '_browser_douyin_source', unavailable)
    with pytest.raises(AppError) as error:
        media.douyin_source(PAGE_URL, tmp_path)
    assert error.value.code == 'douyin_browser_material' and called==[VIDEO_ID]
    assert not (tmp_path / 'douyin.mp4').exists()


def test_desktop_page_falls_back_to_public_share_for_same_id_only(tmp_path, monkeypatch):
    desktop = 'https://www.douyin.com/video/' + VIDEO_ID
    calls = []
    def fetch(url, *args, **kwargs):
        calls.append(url)
        if url == desktop:
            return b'<script>/* challenge is not executed */</script>', desktop, {}
        return router_page([{'aweme_id': '9999999999999999999', 'video': {'playAddr': PUBLISHED_URL}}]).encode(), PAGE_URL, {}
    monkeypatch.setattr(media, 'fetch_public', fetch)
    monkeypatch.setattr(media, '_browser_douyin_source', lambda *args, **kwargs: (_ for _ in ()).throw(
        AppError('douyin_browser_material','[契约] 独立采集器未取得指定原片')))
    with pytest.raises(AppError) as error:
        media.douyin_source(desktop, tmp_path)
    assert error.value.code == 'douyin_browser_material' and calls == [desktop, PAGE_URL]


@pytest.mark.parametrize('duration', ['nan', 'inf', '-1', '1201'])
def test_probe_rejects_nonfinite_and_out_of_range_duration(monkeypatch, tmp_path, duration):
    monkeypatch.setattr(media, 'run_media', lambda *args: json.dumps({'format': {'duration': duration}, 'streams': [{'codec_type': 'video'}]}).encode())
    with pytest.raises(AppError) as error:
        media.inspect_video(tmp_path / 'synthetic.mp4')
    assert error.value.code == 'duration'
