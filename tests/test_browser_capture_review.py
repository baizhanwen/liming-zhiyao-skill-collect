"""Independent offline review of the browser broker; no browser/site/model use."""
import gzip
import json
import socket
import time
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import backend.browser_capture as capture
from backend.security import AppError, PublicTransport
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VIDEO_ID = '1234567890123456789'
TARGET = 'https://media.contract.invalid/target.mp4?signature=synthetic-only'
OTHER = 'https://media.contract.invalid/recommended.mp4?signature=synthetic-only'


def broker(tmp_path, respond):
    return capture._Broker(tmp_path, VIDEO_ID, time.monotonic() + 30,
                           transport_factory=lambda: httpx.MockTransport(respond),
                           media_inspector=lambda path: {'video': b'audio' not in path.read_bytes(),
                                                         'audio': b'audio' in path.read_bytes(), 'duration': 1.0})


def test_decoded_gzip_and_duplicate_cookie_headers_are_returned_correctly(tmp_path):
    body = b'<html>Explicit anonymous visitor contract.</html>'
    encoded = gzip.compress(body)

    def respond(request):
        assert request.headers['cookie'] == 'visitor=ANON_CONTRACT_ONLY'
        assert 'authorization' not in request.headers and 'proxy-authorization' not in request.headers
        assert request.headers['accept-encoding'] == 'gzip, deflate'
        return httpx.Response(200, content=encoded, headers=[('Content-Type', 'text/html'),
            ('Content-Encoding', 'gzip'), ('Content-Length', str(len(encoded))),
            ('Set-Cookie', 'first=CONTRACT_FIRST; Path=/; HttpOnly'),
            ('Set-Cookie', 'second=CONTRACT_SECOND; Expires=Wed, 09 Jun 2032 10:18:14 GMT; Path=/')])

    b = broker(tmp_path, respond)
    try:
        status, headers, decoded, path = b._request('https://page.contract.invalid/visit', 'GET',
            {'Cookie': 'visitor=ANON_CONTRACT_ONLY', 'Authorization': 'Bearer SYNTHETIC_FORBIDDEN',
             'Proxy-Authorization': 'SYNTHETIC_FORBIDDEN'}, resource_type='document')
        assert status == 200 and decoded == body and path is None
        assert 'content-encoding' not in headers and 'content-length' not in headers
        assert headers['set-cookie'].split('\n') == ['first=CONTRACT_FIRST; Path=/; HttpOnly',
            'second=CONTRACT_SECOND; Expires=Wed, 09 Jun 2032 10:18:14 GMT; Path=/']
        assert b.public_title('CONTRACT_FIRST CONTRACT_SECOND ANON_CONTRACT_ONLY') == '[REDACTED] [REDACTED] [REDACTED]'
    finally:
        b.close()


def test_complete_audio_does_not_signal_original_video_ready(tmp_path):
    b = broker(tmp_path, lambda request: httpx.Response(200, content=b'contract audio only',
                                                       headers={'content-type': 'audio/mp4'}))
    try:
        b.urls.add(TARGET)
        reply = b.handle({'type': 'fetch', 'id': 1, 'url': TARGET, 'method': 'GET',
                          'resource_type': 'media', 'headers': {}})
        assert not reply.get('abort')
        assert reply['capture_ready'] is False
    finally:
        b.close()


def test_same_page_recommendation_cannot_bind_itself_from_all_video_elements(tmp_path):
    b = broker(tmp_path, lambda request: httpx.Response(200, content=b'contract unrelated video',
                                                       headers={'content-type': 'video/mp4'}))
    try:
        b._request(OTHER, 'GET', {}, resource_type='media')
        assert b.complete() == []
        candidates = b.finish([{'src': 'blob:https://www.douyin.com/target-opaque'},
                               {'src': OTHER}], VIDEO_ID)
        assert candidates == []
    finally:
        b.close()


def test_metadata_binding_excludes_other_items_even_when_they_appear_first(tmp_path):
    b = broker(tmp_path, lambda request: httpx.Response(200, content=b'contract video',
                                                       headers={'content-type': 'video/mp4'}))
    try:
        b._bind_json(json.dumps({'items': [
            {'aweme_id': '9876543210987654321', 'video': {'play_addr': {'url_list': [OTHER]}}},
            {'aweme_id': VIDEO_ID, 'video': {'play_addr': {'url_list': [TARGET]}}}]}).encode(),
            'https://www-hj.douyin.com/public-metadata')
        assert b._bound(TARGET) and not b._bound(OTHER)
        b._request(OTHER, 'GET', {}, resource_type='media')
        assert b.complete() == []
        b._request(TARGET, 'GET', {}, resource_type='media')
        output = b.finish([], VIDEO_ID)
        assert len(output) == 1 and output[0]['complete']
        assert 'signature' not in json.dumps(output)
    finally:
        b.close()


@pytest.mark.parametrize('origin', ['https://ads.contract.invalid/metadata',
    'https://www.douyin.com.evil.invalid/metadata', 'http://www.douyin.com/metadata',
    'https://user:synthetic@www.douyin.com/metadata'])
def test_untrusted_json_origin_cannot_attest_the_requested_item(tmp_path, origin):
    b = broker(tmp_path, lambda request: httpx.Response(404))
    try:
        b._bind_json(json.dumps({'aweme_id': VIDEO_ID, 'video': {
            'play_addr': {'uri': 'synthetic-target-uri', 'url_list': [TARGET]}}}).encode(), origin)
        assert not b._bound(TARGET) and b.metadata_hashes == set()
    finally:
        b.close()


def test_matching_uri_on_another_host_or_path_cannot_attest_media(tmp_path):
    b = broker(tmp_path, lambda request: httpx.Response(404))
    try:
        b._bind_json(json.dumps({'aweme_id': VIDEO_ID, 'video': {
            'play_addr': {'uri': 'synthetic-target-uri', 'url_list': [TARGET]}}}).encode(),
            'https://www-hj.douyin.com/public-metadata')
        assert not b._bound('https://media.contract.invalid/target.mp4?vid=synthetic-target-uri')
        assert b._bound(TARGET + '&temp=synthetic-player-cache')
        assert not b._bound('https://ads.contract.invalid/target.mp4?vid=synthetic-target-uri')
        assert not b._bound('https://media.contract.invalid/recommendation.mp4?vid=synthetic-target-uri')
        assert not b._bound('http://media.contract.invalid/target.mp4?vid=synthetic-target-uri')
        assert not b._bound('https://media.contract.invalid:8443/target.mp4?vid=synthetic-target-uri')
    finally:
        b.close()


@pytest.mark.parametrize('url', [
    'https://external.contract.invalid/video/' + VIDEO_ID,
    'https://www.douyin.com.evil.invalid/video/' + VIDEO_ID,
    'http://www.douyin.com/video/' + VIDEO_ID,
    'https://user:synthetic@www.douyin.com/video/' + VIDEO_ID,
    'https://www.douyin.com:444/video/' + VIDEO_ID,
    'file://www.douyin.com/video/' + VIDEO_ID,
])
def test_invalid_platform_navigation_is_rejected_before_browser_process(tmp_path, monkeypatch, url):
    monkeypatch.setattr(capture.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Must not launch a browser'))
    with pytest.raises(AppError) as error:
        capture.capture_douyin(url, tmp_path, video_id=VIDEO_ID)
    assert error.value.code == 'unsupported_source'
    assert list(tmp_path.iterdir()) == []


def test_each_redirect_route_is_revalidated_before_any_private_connection(tmp_path, monkeypatch):
    def addresses(host, *args, **kwargs):
        address = '93.184.216.34' if host == 'page.contract.invalid' else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', addresses)
    connections = []

    def respond(request):
        connections.append(request.url.host)
        return httpx.Response(302, headers={'Location': 'https://private.contract.invalid/local-only'})

    transport = PublicTransport()
    transport.transport.close()
    transport.transport = httpx.MockTransport(respond)
    b = capture._Broker(tmp_path, VIDEO_ID, time.monotonic() + 30,
                        transport_factory=lambda: transport)
    try:
        status, headers, _, _ = b._request('https://page.contract.invalid/start', 'GET', {})
        assert status == 302  # Python does not follow before the browser's next route.
        with pytest.raises(AppError) as error:
            b._request(headers['location'], 'GET', {})
        assert error.value.code == 'unsafe_url'
        assert connections == ['93.184.216.34']
    finally:
        b.close()


def test_incomplete_ranges_and_overlapping_changed_content_never_finish(tmp_path):
    responses = [(b'abcd', 'bytes 0-3/12'), (b'CHNG', 'bytes 2-5/12')]

    def respond(request):
        payload, content_range = responses.pop(0)
        return httpx.Response(206, content=payload, headers={'content-type': 'video/mp4', 'content-range': content_range})

    b = broker(tmp_path, respond)
    try:
        b._request(TARGET, 'GET', {'Range': 'bytes=0-3'}, resource_type='media')
        assert b.complete() == []
        with pytest.raises(AppError) as error:
            b._request(TARGET, 'GET', {'Range': 'bytes=2-5'}, resource_type='media')
        assert error.value.code == 'douyin_browser_range'
        b.urls.add(TARGET)
        assert b.complete() == []
    finally:
        b.close()


def test_same_pinned_ip_cannot_reuse_another_host_tls_certificate(tmp_path, monkeypatch):
    """A real local TLS exchange, with a fresh synthetic cert trusted for A only.

    Both test hosts are explicitly pinned to this local fixture. No production
    DNS guard is relaxed and no external server, browser, or account is used.
    """
    import backend.security as security
    first, second = 'a.capture.contract.invalid', 'b.capture.contract.invalid'
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, first)])
    at = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(at - timedelta(days=1)).not_valid_after(at + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(first)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'fixture-cert.pem', tmp_path / 'fixture-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    received = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            received.append(self.headers['Host'])
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_server.load_cert_chain(cert_path, key_path)
    server.socket = tls_server.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    original_transport = httpx.HTTPTransport
    trust_fixture = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setattr(security, 'public_url', lambda url: ['127.0.0.1'])
    monkeypatch.setattr(security.httpx, 'HTTPTransport', lambda **kwargs: original_transport(verify=trust_fixture, **kwargs))
    try:
        with httpx.Client(transport=PublicTransport(), trust_env=False) as client:
            port = server.server_address[1]
            assert client.get(f'https://{first}:{port}/first').status_code == 200
            # This second host has no valid certificate at the pinned endpoint.
            # Reusing A's TLS session would incorrectly return 200 here.
            with pytest.raises(httpx.ConnectError) as error:
                client.get(f'https://{second}:{port}/second')
            assert 'certificate verify failed' in str(error.value).lower()
        assert received == [f'{first}:{port}']
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


# Native CONNECT route: these tests use only local sockets and injected public
# resolution/dialing. They do not start Chromium or fetch an external video.
class ProxyBudget:
    def __init__(self):
        self.deadline = time.monotonic() + 10
        self.cancelled = False

    def _check(self):
        if self.cancelled or time.monotonic() > self.deadline:
            raise AppError('cancelled', 'Synthetic cancelled/deadline fixture')


@contextmanager
def native_proxy(*, resolver=None, dialer=None, budget=None):
    budget = budget or ProxyBudget()
    calls, peers = [], []

    def socket_pair(ip, port, timeout):
        calls.append((ip, port, timeout))
        remote, peer = socket.socketpair()
        peer.settimeout(2)
        peers.append(peer)
        return remote

    proxy = capture._PinnedProxy(('127.0.0.1', 0), budget,
                                  resolver=resolver or (lambda url: ['93.184.216.34']),
                                  dialer=dialer or socket_pair)
    worker = threading.Thread(target=lambda: proxy.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    try:
        yield proxy, calls, peers
    finally:
        proxy.stop_tunnels()
        for peer in peers:
            peer.close()
        proxy.shutdown()
        proxy.server_close()
        worker.join(timeout=2)


def proxy_client(proxy, line, headers=''):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(proxy.server_address)
    try:
        client.sendall((line + '\r\n' + headers + '\r\n').encode('ascii'))
    except ConnectionError:
        return client, b''
    response = bytearray()
    while b'\r\n\r\n' not in response:
        try:
            chunk = client.recv(4096)
        except ConnectionError:
            break
        if not chunk:
            break
        response.extend(chunk)
    return client, bytes(response)


def test_native_connect_dials_validated_numeric_ip_and_relays_opaque_bytes():
    resolved = []
    with native_proxy(resolver=lambda url: resolved.append(url) or ['93.184.216.34', '1.1.1.1']) as (proxy, calls, peers):
        client, response = proxy_client(proxy, 'CONNECT public.contract.invalid:443 HTTP/1.1',
                                        'Proxy-Authorization: SYNTHETIC_NOT_FOR_ORIGIN\r\n')
        try:
            assert response == b'HTTP/1.1 200 Connection Established\r\n\r\n'
            assert resolved == ['https://public.contract.invalid:443']
            assert calls[0][:2] == ('93.184.216.34', 443)
            assert .1 <= calls[0][2] <= 10
            client.sendall(b'\x16\x03\x03OPAQUE_CONTRACT_CLIENT_BYTES')
            assert peers[0].recv(4096) == b'\x16\x03\x03OPAQUE_CONTRACT_CLIENT_BYTES'
            peers[0].sendall(b'\x16\x03\x03OPAQUE_CONTRACT_SERVER_BYTES')
            assert client.recv(4096) == b'\x16\x03\x03OPAQUE_CONTRACT_SERVER_BYTES'
            # CONNECT headers, including synthetic proxy credentials, are not
            # forwarded into the opaque origin connection.
            assert proxy.bytes == len(b'\x16\x03\x03OPAQUE_CONTRACT_CLIENT_BYTES') + len(b'\x16\x03\x03OPAQUE_CONTRACT_SERVER_BYTES')
        finally:
            client.close()


@pytest.mark.parametrize('line', [
    'GET https://public.contract.invalid/private HTTP/1.1',
    'CONNECT public.contract.invalid:80 HTTP/1.1', 'CONNECT public.contract.invalid:8443 HTTP/1.1',
    'CONNECT public.contract.invalid HTTP/1.1', 'CONNECT user:synthetic@public.contract.invalid:443 HTTP/1.1',
    'CONNECT public.contract.invalid:443/path HTTP/1.1', 'CONNECT public.contract.invalid:443?query=synthetic HTTP/1.1',
    'CONNECT public.contract.invalid:443 HTTP/2', 'CONNECT public.contract.invalid:443@elsewhere HTTP/1.1',
    'CONNECT [::1%25lo0]:443 HTTP/1.1',
])
def test_native_connect_rejects_plain_http_ports_and_credential_authority_before_dns(line, capsys):
    with native_proxy(resolver=lambda url: pytest.fail('Must reject before resolution')) as (proxy, calls, _):
        client, response = proxy_client(proxy, line)
        client.close()
        assert response.startswith(b'HTTP/1.1 403') and calls == []
    assert 'synthetic' not in capsys.readouterr().err


@pytest.mark.parametrize('addresses', [['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'],
    ['::1'], ['::ffff:127.0.0.1'], ['198.18.0.4'], ['93.184.216.34', '10.0.0.1'], []])
def test_native_connect_rejects_private_mixed_or_reserved_resolver_answers(addresses):
    with native_proxy(resolver=lambda url: addresses) as (proxy, calls, _):
        client, response = proxy_client(proxy, 'CONNECT public.contract.invalid:443 HTTP/1.1')
        client.close()
        assert response.startswith(b'HTTP/1.1 403') and calls == []


def test_native_connect_resolver_failure_has_no_direct_or_system_fallback():
    def failed_resolver(url):
        raise AppError('dns', 'Synthetic unavailable resolver')
    with native_proxy(resolver=failed_resolver) as (proxy, calls, _):
        client, response = proxy_client(proxy, 'CONNECT public.contract.invalid:443 HTTP/1.1')
        client.close()
        assert response.startswith(b'HTTP/1.1 403') and calls == []


def test_native_connect_fakeip_doh_answer_is_pinned_without_second_hostname_resolution(monkeypatch):
    import backend.security as security
    dns_calls, doh_calls = [], []

    def system_dns(host, *args, **kwargs):
        dns_calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.12', 443))]

    def doh(request):
        doh_calls.append(request)
        assert request.url.host == '8.8.8.8'
        assert request.headers['host'] == request.extensions['sni_hostname'] == 'dns.google'
        return httpx.Response(200, json={'Status': 0, 'Question': [{'name': 'public.contract.invalid', 'type': 1}],
            'Answer': [{'name': 'public.contract.invalid', 'type': 1, 'TTL': 0, 'data': '93.184.216.34'}]})

    monkeypatch.setattr(security.socket, 'getaddrinfo', system_dns)
    monkeypatch.setattr(security, '_DNS_CACHE', {})
    monkeypatch.setattr(security.httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(doh))
    with native_proxy(resolver=security.public_url) as (proxy, calls, _):
        client, response = proxy_client(proxy, 'CONNECT public.contract.invalid:443 HTTP/1.1')
        client.close()
        assert response.startswith(b'HTTP/1.1 200')
        assert calls[0][:2] == ('93.184.216.34', 443)
    assert dns_calls == ['public.contract.invalid'] and len(doh_calls) == 1


def test_native_numeric_dialer_never_resolves_a_hostname(monkeypatch):
    calls = []

    class NumericSocket:
        def settimeout(self, value):
            calls.append(('timeout', value))

        def connect(self, address):
            calls.append(('connect', address))

        def close(self):
            pass

    monkeypatch.setattr(capture.socket, 'socket', lambda family, kind: calls.append(('socket', family, kind)) or NumericSocket())
    monkeypatch.setattr(capture.socket, 'getaddrinfo', lambda *args, **kwargs: pytest.fail('Numeric connect must not re-resolve'))
    capture._dial_pinned('93.184.216.34', 443, 2)
    capture._dial_pinned('2606:4700:4700::1111', 443, 2)
    assert ('connect', ('93.184.216.34', 443)) in calls
    assert ('connect', ('2606:4700:4700::1111', 443)) in calls


def test_native_connect_byte_budget_closes_instead_of_forwarding_excess(monkeypatch):
    monkeypatch.setattr(capture, 'MAX_TOTAL', 8)
    with native_proxy() as (proxy, calls, peers):
        client, response = proxy_client(proxy, 'CONNECT public.contract.invalid:443 HTTP/1.1')
        try:
            assert response.startswith(b'HTTP/1.1 200')
            client.sendall(b'12345678')
            assert peers[0].recv(8) == b'12345678'
            client.sendall(b'9')
            assert peers[0].recv(1) == b''
            assert proxy.bytes == 9 and proxy.denied >= 1
        finally:
            client.close()


@pytest.mark.parametrize('limit', ['total', 'active', 'stopped'])
def test_native_connect_connection_budgets_do_not_dial(limit):
    with native_proxy() as (proxy, calls, _):
        if limit == 'total':
            proxy.connections = 256
        elif limit == 'active':
            proxy.active = 32
        else:
            proxy.stopping.set()
        client, response = proxy_client(proxy, 'CONNECT public.contract.invalid:443 HTTP/1.1')
        client.close()
        assert b'200' not in response and calls == [] and proxy.denied >= 1


@pytest.mark.parametrize('mode', ['cancelled', 'expired'])
def test_native_connect_deadline_and_cancel_stop_before_resolution(mode):
    budget = ProxyBudget()
    if mode == 'cancelled':
        budget.cancelled = True
    else:
        budget.deadline = time.monotonic() - 1
    with native_proxy(budget=budget, resolver=lambda url: pytest.fail('No resolution after stop')) as (proxy, calls, _):
        client, response = proxy_client(proxy, 'CONNECT public.contract.invalid:443 HTTP/1.1')
        client.close()
        assert response.startswith(b'HTTP/1.1 403') and calls == []


PUBLISHED = 'https://media.contract.invalid/target.mp4?signature=synthetic-signed-value&watermark=1&dup=a&dup=b&empty='


@pytest.mark.parametrize('observed,allowed', [
    (PUBLISHED, True), (PUBLISHED + '&temp=synthetic-cache', True),
    (PUBLISHED.replace('media.contract.invalid', 'media.contract.invalid:443') + '&temp=synthetic', True),
    (PUBLISHED + '&auth=synthetic', False), (PUBLISHED + '&temp=x&auth=synthetic', False),
    (PUBLISHED.replace('signature=synthetic-signed-value', 'signature=changed'), False),
    (PUBLISHED.replace('signature=synthetic-signed-value&', ''), False),
    (PUBLISHED.replace('&watermark=1', ''), False), (PUBLISHED.replace('&dup=b', ''), False),
    (PUBLISHED.replace('dup=a&dup=b', 'dup=b&dup=a'), False),
    (PUBLISHED.replace('&empty=', ''), False), (PUBLISHED.replace('&empty=', '&empty=changed'), False),
    (PUBLISHED.replace('https:', 'http:'), False), (PUBLISHED.replace('media.contract.invalid', 'other.contract.invalid'), False),
    (PUBLISHED.replace('/target.mp4', '/other.mp4'), False),
    (PUBLISHED.replace('media.contract.invalid', 'media.contract.invalid:8443'), False),
    (PUBLISHED.replace('media.contract.invalid', 'synthetic@media.contract.invalid'), False),
])
def test_native_temp_exception_preserves_every_published_query_value(tmp_path, observed, allowed):
    b = broker(tmp_path, lambda request: httpx.Response(404))
    try:
        b._bind_json(json.dumps({'aweme_id': VIDEO_ID, 'video': {'play_addr': {'url_list': [PUBLISHED]}}}).encode(),
                     'https://www-hj.douyin.com/official-item')
        assert b._bound(observed) is allowed
    finally:
        b.close()


def test_native_published_temp_value_cannot_be_replaced(tmp_path):
    b = broker(tmp_path, lambda request: httpx.Response(404))
    try:
        b.urls.add(PUBLISHED + '&temp=published')
        assert b._bound(PUBLISHED + '&temp=published')
        assert not b._bound(PUBLISHED + '&temp=replaced')
    finally:
        b.close()


def test_native_observed_url_is_downloaded_unchanged_and_secrets_stay_out_of_result(tmp_path):
    observed = PUBLISHED + '&temp=cache%2B123'
    requested = []

    def respond(request):
        requested.append(str(request.url))
        assert request.headers['cookie'] == 'visitor=SYNTHETIC_ANONYMOUS_COOKIE'
        return httpx.Response(200, content=b'contract video bytes', headers={'content-type': 'video/mp4'})

    b = broker(tmp_path, respond)
    try:
        b._bind_json(json.dumps({'aweme_id': VIDEO_ID, 'video': {'play_addr': {'url_list': [PUBLISHED]}}}).encode(),
                     'https://www-hj.douyin.com/official-item')
        reply = b.handle({'type': 'observed_media', 'id': 10, 'url': observed,
                          'headers': {'Cookie': 'visitor=SYNTHETIC_ANONYMOUS_COOKIE'}})
        assert reply['capture_ready'] is True and requested == [observed]
        public_result = b.finish([], VIDEO_ID)
        serialized = json.dumps(public_result)
        assert 'synthetic-signed-value' not in serialized and 'SYNTHETIC_ANONYMOUS_COOKIE' not in serialized
        assert 'cache%2B123' not in serialized
    finally:
        b.close()


def test_native_misleading_video_mime_with_only_audio_track_cannot_finish(tmp_path):
    b = capture._Broker(tmp_path, VIDEO_ID, time.monotonic() + 10,
        transport_factory=lambda: httpx.MockTransport(lambda request: httpx.Response(200,
            content=b'pretend-video-mime-actual-audio-fixture', headers={'content-type': 'video/mp4'})),
        media_inspector=lambda path: {'video': False, 'audio': True, 'duration': 144.8})
    try:
        b.urls.add(TARGET)
        assert not b.handle({'type': 'observed_media', 'id': 11, 'url': TARGET, 'headers': {}})['capture_ready']
        assert b.finish([], VIDEO_ID) == []
    finally:
        b.close()


def test_native_connect_preserves_end_to_end_origin_tls_hostname_validation(tmp_path):
    """The tunnel never terminates TLS: a real local A-only cert rejects B."""
    first, second = 'a.native.contract.invalid', 'b.native.contract.invalid'
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, first)])
    at = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(at - timedelta(days=1))
            .not_valid_after(at + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(first)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'native-contract-cert.pem', tmp_path / 'native-contract-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    received = []

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers['Host'])
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')

        def log_message(self, *args):
            pass

    origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    origin.socket = tls.wrap_socket(origin.socket, server_side=True)
    worker = threading.Thread(target=lambda: origin.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    dialed = []

    def local_fixture(ip, port, timeout):
        dialed.append((ip, port))
        remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote.settimeout(timeout)
        remote.connect(origin.server_address)
        return remote

    trust = ssl.create_default_context(cafile=str(cert_path))
    try:
        with native_proxy(dialer=local_fixture) as (proxy, _, _):
            client, response = proxy_client(proxy, f'CONNECT {first}:443 HTTP/1.1')
            assert response.startswith(b'HTTP/1.1 200')
            with trust.wrap_socket(client, server_hostname=first) as connection:
                connection.sendall(f'GET /fixture HTTP/1.1\r\nHost: {first}\r\nConnection: close\r\n\r\n'.encode())
                assert b'200 OK' in connection.recv(4096)
            client, response = proxy_client(proxy, f'CONNECT {second}:443 HTTP/1.1')
            assert response.startswith(b'HTTP/1.1 200')
            try:
                with pytest.raises(ssl.SSLCertVerificationError):
                    trust.wrap_socket(client, server_hostname=second)
            finally:
                client.close()
        assert received == [first]
        assert dialed == [('93.184.216.34', 443), ('93.184.216.34', 443)]
    finally:
        origin.shutdown()
        origin.server_close()
        worker.join(timeout=2)
