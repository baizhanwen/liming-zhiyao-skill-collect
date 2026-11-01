"""Offline customer-diagnostic contracts; no browser, site, model or app data."""
import json
import socket
import ssl
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from backend.security import AppError
from scripts import diagnose_douyin as diagnostic


VIDEO_ID = '1234567890123456789'
SHARE = 'https://v.douyin.com/SYNTHETIC_SHARE/'
PAGE = 'https://www.douyin.com/video/' + VIDEO_ID
PRIVATE = 'SYNTHETIC_PRIVATE_SENTINEL'
UNSAFE_TEXT = PRIVATE + ' C:\\Users\\synthetic 192.0.2.7 Cookie=synthetic '
SIGNED = PAGE + '?signature=' + PRIVATE


@pytest.fixture(autouse=True)
def no_external_process_or_dns(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('This diagnostic contract must remain offline')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(diagnostic.subprocess, 'run', forbidden)


def clean(value):
    text = json.dumps(value)
    for forbidden in (PRIVATE, VIDEO_ID, '192.0.2.7', 'C:\\Users', 'Cookie=', 'signature='):
        assert forbidden not in text


def test_helpers_receive_only_runtime_environment_not_credentials_or_preload_hooks(monkeypatch):
    for key in ('NODE_OPTIONS', 'NODE_PATH', 'SSLKEYLOGFILE', 'PYTHONPATH',
                'PYTHONHOME', 'MINIMAX_API_KEY', 'HTTPS_PROXY'):
        monkeypatch.setenv(key, PRIVATE)
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', 'SYNTHETIC_BROWSER_CACHE')
    monkeypatch.setenv('SSL_CERT_FILE', 'SYNTHETIC_CA_FILE')
    result = diagnostic.helper_environment()
    assert PRIVATE not in result.values()
    assert result['PLAYWRIGHT_BROWSERS_PATH'] == 'SYNTHETIC_BROWSER_CACHE'
    assert result['SSL_CERT_FILE'] == 'SYNTHETIC_CA_FILE'
    assert result['PYTHONUTF8'] == '1' and result['PYTHONDONTWRITEBYTECODE'] == '1'


@pytest.mark.parametrize('value,expected', [
    (SHARE, SHARE),
    ('分享文字\n' + SHARE + ' 复制链接', SHARE),
    ('请查看 ' + SHARE + '）', SHARE),
    (PAGE, PAGE),
    ('https://www.iesdouyin.com/share/video/' + VIDEO_ID + '/',
     'https://www.iesdouyin.com/share/video/' + VIDEO_ID + '/'),
])
def test_normalize_accepts_platform_links_and_share_text(value, expected):
    assert diagnostic.normalize_input(value) == expected
    assert diagnostic.platform_url(expected)


@pytest.mark.parametrize('value', [
    None, [], '', 'x' * 8001,
    'http://www.douyin.com/video/123',
    'https://www.douyin.com.evil.invalid/video/123',
    'https://evil.invalid/?next=https://www.douyin.com/video/123',
    'https://user:synthetic@www.douyin.com/video/123',
    'https://www.douyin.com:8443/video/123',
    'https://www.douyin.com:80/video/123',
    'https://www.douyin.com:bad/video/123',
    'https://www.douyin.com/video/\x00synthetic',
])
def test_invalid_input_never_reaches_network(value):
    assert diagnostic.normalize_input(value) is None


@pytest.mark.parametrize('destination', [
    'https://other.contract.invalid/video/123',
    'https://www.douyin.com.evil.invalid/video/123',
    'http://www.douyin.com/video/123',
    'https://127.0.0.1/video/123',
    'https://user:synthetic@www.douyin.com/video/123',
    'https://www.douyin.com:8443/video/123',
])
def test_redirect_is_rejected_before_the_destination_transport(destination):
    requested = []

    def origin(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={'Location': destination})

    report = diagnostic.probe_page(SHARE, lambda: httpx.MockTransport(origin))
    assert requested == [SHARE]
    assert report['category'] == 'non_platform_redirect'
    assert report['origin_request_attempts'] == 1
    assert report['hops'][0]['redirect_is_platform'] is False
    clean(report)


class UnreadableStream(httpx.SyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False

    def __iter__(self):
        self.reads += 1
        raise AssertionError('A media or redirect body must never be consumed')
        yield b''

    def close(self):
        self.closed = True


@pytest.mark.parametrize('mime', [
    'video/mp4', 'audio/mp4', 'application/octet-stream',
    'video/mp4; charset=utf-8', 'application/vnd.apple.mpegurl',
])
def test_non_page_mime_closes_the_stream_without_reading(mime):
    body = UnreadableStream()
    report = diagnostic.probe_page(PAGE, lambda: httpx.MockTransport(lambda request:
        httpx.Response(200, headers={'content-type': mime}, stream=body)))
    assert body.reads == 0 and body.closed
    assert report['category'] == 'non_page_response' and report['page_bytes'] == 0
    clean(report)


def test_platform_redirect_closes_body_strips_cookies_and_reports_id_as_boolean():
    body, seen = UnreadableStream(), []

    def origin(request):
        seen.append(str(request.url))
        assert 'cookie' not in request.headers
        if str(request.url) == SHARE:
            return httpx.Response(302, headers={'Location': SIGNED,
                'Set-Cookie': 'visitor=' + PRIVATE + '; Path=/'}, stream=body)
        return httpx.Response(200, text='<html>Public page only, no media verified.</html>',
                              headers={'content-type': 'text/html'})

    report = diagnostic.probe_page(SHARE, lambda: httpx.MockTransport(origin))
    assert seen == [SHARE, SIGNED] and body.reads == 0 and body.closed
    assert report['status'] == 'page_received' and report['category'] == 'none'
    assert report['origin_request_attempts'] == 2
    assert report['platform_url_has_video_id'] is True
    assert report['platform_redirect_has_video_id'] is True
    assert report['hops'] == [
        {'status': 302, 'request_url_has_video_id': False,
         'redirect_is_platform': True, 'redirect_url_has_video_id': True},
        {'status': 200, 'request_url_has_video_id': True}]
    clean(report)


def test_redirect_loop_has_a_fixed_limit_and_no_location_in_report():
    seen = []

    def origin(request):
        seen.append(1)
        return httpx.Response(302, headers={'Location': SIGNED})

    report = diagnostic.probe_page(SHARE, lambda: httpx.MockTransport(origin))
    assert report['category'] == 'redirect_limit' and len(seen) == 6
    assert report['origin_request_attempts'] == 6
    clean(report)


def nested_error(inner):
    outer = httpx.ConnectError(UNSAFE_TEXT)
    outer.__cause__ = inner
    return outer


@pytest.mark.parametrize('error,category', [
    (nested_error(ssl.SSLCertVerificationError(1, UNSAFE_TEXT)), 'tls'),
    (nested_error(socket.gaierror(-2, UNSAFE_TEXT)), 'dns'),
    (httpx.ReadTimeout(UNSAFE_TEXT), 'timeout'),
    (httpx.ConnectTimeout(UNSAFE_TEXT), 'timeout'),
    (httpx.ConnectError(UNSAFE_TEXT), 'connection'),
    (AppError('unsafe_url', UNSAFE_TEXT), 'public_address_guard'),
    (AppError('dns', UNSAFE_TEXT), 'dns'),
    (AppError('source_timeout', UNSAFE_TEXT), 'timeout'),
    (Exception(UNSAFE_TEXT), 'unexpected'),
])
def test_typed_failures_do_not_echo_exception_details(error, category, capsys):
    def origin(request):
        raise error

    report = diagnostic.probe_page(SHARE, lambda: httpx.MockTransport(origin))
    assert report['status'] == 'failed' and report['category'] == category
    clean(report)
    assert capsys.readouterr() == ('', '')


def test_http_status_excludes_error_url_headers_and_body():
    report = diagnostic.probe_page(SIGNED, lambda: httpx.MockTransport(lambda request:
        httpx.Response(403, text=UNSAFE_TEXT, headers={'content-type': 'text/html',
            'Set-Cookie': 'visitor=' + PRIVATE})))
    assert report['category'] == 'http_status' and report['http_status'] == 403
    clean(report)


def runtime_fixture(tmp_path, monkeypatch):
    script = tmp_path / 'frontend/scripts/capture-douyin.mjs'
    script.parent.mkdir(parents=True)
    script.write_text('// Synthetic runtime metadata fixture', encoding='utf-8')
    monkeypatch.setattr(diagnostic.shutil, 'which', lambda name: 'SYNTHETIC_NODE')


@pytest.mark.parametrize('version,expected_version', [('1.64.1', '1.64.1'), (UNSAFE_TEXT, None)])
def test_runtime_report_whitelists_fields_and_does_not_launch_browser(tmp_path, monkeypatch, version, expected_version):
    runtime_fixture(tmp_path, monkeypatch)

    def run(args, **kwargs):
        assert args[-1] == '--check-runtime' and kwargs['timeout'] == 10
        assert kwargs['capture_output'] is True and 'shell' not in kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True,
            'browser_installed': True, 'playwright_version': version,
            'executable_path': UNSAFE_TEXT, 'unknown': UNSAFE_TEXT}), stderr=UNSAFE_TEXT)

    monkeypatch.setattr(diagnostic.subprocess, 'run', run)
    report = diagnostic.runtime_check(tmp_path)
    assert report['browser_installed'] is True and report['status'] == 'ready'
    assert set(report) <= {'node_found', 'capture_script_present', 'browser_installed',
                           'status', 'playwright_version'}
    assert report.get('playwright_version') == expected_version
    clean(report)


@pytest.mark.parametrize('failure,status', [('timeout', 'timeout'), ('dirty_json', 'unavailable')])
def test_runtime_timeout_or_dirty_json_has_a_fixed_report(tmp_path, monkeypatch, failure, status):
    runtime_fixture(tmp_path, monkeypatch)

    def run(args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 10, output=UNSAFE_TEXT, stderr=UNSAFE_TEXT)
        return SimpleNamespace(returncode=0, stdout=UNSAFE_TEXT, stderr=UNSAFE_TEXT)

    monkeypatch.setattr(diagnostic.subprocess, 'run', run)
    report = diagnostic.runtime_check(tmp_path)
    assert report['status'] == status and report['browser_installed'] is False
    clean(report)


@pytest.mark.parametrize('raw', [None, [], PRIVATE, {'category': PRIVATE}, {'category': []}, {'category': {}}])
def test_malformed_worker_report_is_replaced_with_fixed_error(raw):
    assert diagnostic.clean_network_report(raw) == {'status': 'failed', 'category': 'worker_error'}


def test_clean_worker_report_drops_untrusted_fields_and_bounds_counts():
    report = diagnostic.clean_network_report({'status': 'page_received', 'category': 'none',
        'http_status': True, 'origin_request_attempts': 999, 'page_bytes': -1,
        'platform_url_has_video_id': PRIVATE, 'platform_redirect_has_video_id': True,
        'url': SIGNED, 'headers': UNSAFE_TEXT, 'body': UNSAFE_TEXT,
        'hops': [{'status': 302, 'request_url_has_video_id': True,
                  'redirect_url_has_video_id': PRIVATE, 'Location': SIGNED},
                 {'status': True, 'url': SIGNED}]})
    assert report['http_status'] is None and report['origin_request_attempts'] is None
    assert report['page_bytes'] is None and report['platform_url_has_video_id'] is False
    assert report['platform_redirect_has_video_id'] is True
    assert report['hops'] == [{'status': 302, 'request_url_has_video_id': True,
                              'redirect_url_has_video_id': False}]
    clean(report)


@pytest.mark.parametrize('output,returncode', [
    (UNSAFE_TEXT, 0), ('[1,2]', 0), (json.dumps({'category': PRIVATE}), 0),
    ('x' * 16385, 0), ('{}', 1),
])
def test_worker_dirty_or_failed_output_never_escapes(tmp_path, monkeypatch, output, returncode, capsys):
    def run(args, **kwargs):
        assert args[-1] == '--worker' and SIGNED not in args
        assert json.loads(kwargs['input']) == {'url': SIGNED}
        assert kwargs['timeout'] == 40 and 'shell' not in kwargs
        return SimpleNamespace(returncode=returncode, stdout=output, stderr=UNSAFE_TEXT)

    monkeypatch.setattr(diagnostic.subprocess, 'run', run)
    report = diagnostic.run_network_worker(SIGNED, tmp_path)
    assert report == {'status': 'failed', 'category': 'worker_error'}
    assert capsys.readouterr() == ('', '')
    clean(report)


def test_worker_timeout_returns_fixed_deadline_without_exception_output(tmp_path, monkeypatch):
    def run(args, **kwargs):
        assert kwargs['timeout'] == 40
        raise subprocess.TimeoutExpired(args, 40, output=UNSAFE_TEXT, stderr=UNSAFE_TEXT)

    monkeypatch.setattr(diagnostic.subprocess, 'run', run)
    assert diagnostic.run_network_worker(SIGNED, tmp_path) == {
        'status': 'failed', 'category': 'timeout', 'hard_deadline_seconds': 40}


def test_invalid_cli_arguments_have_fixed_error_without_echo(monkeypatch, capsys):
    monkeypatch.setattr(diagnostic.sys, 'argv', ['diagnose_douyin.py', '--unknown', UNSAFE_TEXT])
    monkeypatch.setattr(diagnostic.logging, 'disable', lambda level: None)
    monkeypatch.setattr(diagnostic, 'project_root', lambda: None)
    with pytest.raises(SystemExit) as error:
        diagnostic.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert json.loads(captured.err) == {'status': 'failed', 'category': 'invalid_arguments'}
