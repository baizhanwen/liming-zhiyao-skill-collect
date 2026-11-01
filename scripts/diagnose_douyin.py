"""Read-only Douyin share-page diagnostic, compatible with installed v0.2.2.

Does not open a browser, download original media, load the app/vault/database,
or call models. The network worker has a separate hard deadline. Reports omit
URLs, IDs, IP addresses, response content, headers and exception messages.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

PAGE_LIMIT = 2_000_000
NETWORK_SECONDS = 40
RUNTIME_SECONDS = 10
CATEGORIES = {'none', 'invalid_input', 'missing_project', 'missing_dependencies',
    'dns', 'tls', 'timeout', 'http_status', 'redirect_limit', 'connection',
    'public_address_guard', 'non_platform_redirect', 'non_page_response',
    'page_size_limit', 'worker_error', 'unexpected'}


def project_root():
    for path in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent, Path.cwd()):
        if (path/'backend/security.py').is_file() and (path/'backend/media.py').is_file():
            return path
    return None


def helper_environment():
    allowed = {'PATH', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
        'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
        'LANG', 'LC_ALL', 'LC_CTYPE', 'XDG_CACHE_HOME', 'PLAYWRIGHT_BROWSERS_PATH',
        'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    result = {key: os.environ[key] for key in os.environ if key.upper() in allowed}
    result.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    return result


def platform_url(value):
    try:
        p = urlsplit(value)
        return (p.scheme == 'https' and not p.username and not p.password
            and p.port in (None, 443) and bool(p.hostname)
            and any(p.hostname == host or p.hostname.endswith('.'+host)
                    for host in ('douyin.com', 'iesdouyin.com')))
    except (ValueError, AttributeError):
        return False


def normalize_input(value):
    if not isinstance(value, str) or len(value) > 8000:
        return None
    # Share text may have line breaks; the extracted URL itself may not.
    match = re.search(r'https?://[^\s<>"，。；、]+', value)
    if not match:
        return None
    url = match[0].rstrip(').!）]')
    if any(ord(char) < 32 or ord(char) == 127 for char in url) or not platform_url(url):
        return None
    return url


def classified_error(error):
    """Use types/codes only. Never serialize or inspect exception messages."""
    import httpx
    from backend.security import AppError
    chain, pending, seen = [], [error], set()
    while pending and len(chain) < 12:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item)); chain.append(item)
        pending.extend([item.__cause__, item.__context__])
        pending.extend(arg for arg in item.args if isinstance(arg, BaseException))
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return 'tls', None
    if any(isinstance(item, socket.gaierror) for item in chain):
        return 'dns', None
    for item in chain:
        if isinstance(item, AppError):
            return {'dns': 'dns', 'unsafe_url': 'public_address_guard',
                'too_large': 'page_size_limit', 'source_timeout': 'timeout'}.get(item.code, 'unexpected'), None
    if any(isinstance(item, httpx.TimeoutException) for item in chain):
        return 'timeout', None
    if any(isinstance(item, httpx.TooManyRedirects) for item in chain):
        return 'redirect_limit', None
    for item in chain:
        if isinstance(item, httpx.HTTPStatusError):
            return 'http_status', item.response.status_code
    if any(isinstance(item, httpx.NetworkError) for item in chain):
        return 'connection', None
    return 'unexpected', None


class DiagnosticStop(Exception):
    def __init__(self, category):
        self.category = category


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error echoes unknown arguments, possibly including
        # a pasted secret or signed link. Emit a fixed report instead.
        self.exit(2, json.dumps({'status': 'failed', 'category': 'invalid_arguments'})+'\n')


def probe_page(url, transport_factory=None):
    import httpx
    from backend.security import PublicTransport
    from backend.media import _MOBILE_UA, _video_id
    report = {'status': 'failed', 'category': 'unexpected', 'http_status': None,
        'origin_request_attempts': 0, 'page_bytes': 0, 'platform_url_has_video_id': False,
        'platform_redirect_has_video_id': False, 'hops': []}
    if not platform_url(url):
        report['category'] = 'invalid_input'
        return report

    class GuardedTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = (transport_factory or PublicTransport)()

        def handle_request(self, request):
            current = str(request.url)
            if not platform_url(current):
                raise DiagnosticStop('non_platform_redirect')
            report['origin_request_attempts'] += 1
            response = self.inner.handle_request(request)
            hop = {'status': response.status_code,
                'request_url_has_video_id': bool(_video_id(current))}
            location = response.headers.get('location')
            if 300 <= response.status_code < 400 and location:
                destination = urljoin(current, location)
                hop['redirect_is_platform'] = platform_url(destination)
                hop['redirect_url_has_video_id'] = platform_url(destination) and bool(_video_id(destination))
                report['platform_redirect_has_video_id'] |= hop['redirect_url_has_video_id']
            report['hops'].append(hop)
            if 300 <= response.status_code < 400:
                # httpx consumes redirect bodies before following Location.
                # Keep headers only, closing the actual stream without reading.
                response.close()
                return httpx.Response(response.status_code, headers=response.headers,
                    stream=httpx.ByteStream(b''), extensions=response.extensions)
            if 200 <= response.status_code < 300:
                mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                if mime not in ('text/html', 'application/xhtml+xml', 'application/json', 'text/plain'):
                    response.close()
                    raise DiagnosticStop('non_page_response')
            return response

        def close(self):
            self.inner.close()

    def no_cookies(request):
        request.headers.pop('cookie', None)

    try:
        # Mirrors v0.2.2 share fetching: same checked/pinned transport, UA,
        # no proxy environment, no cookies and at most five redirects.
        with httpx.Client(transport=GuardedTransport(), timeout=25, max_redirects=5,
                follow_redirects=True, trust_env=False, event_hooks={'request': [no_cookies]}) as client:
            deadline = time.monotonic()+30
            with client.stream('GET', url, headers={'User-Agent': _MOBILE_UA}) as response:
                response.raise_for_status()
                report['http_status'] = response.status_code
                for chunk in response.iter_bytes(chunk_size=65536):
                    report['page_bytes'] += len(chunk)
                    if report['page_bytes'] > PAGE_LIMIT:
                        raise DiagnosticStop('page_size_limit')
                    if time.monotonic() > deadline:
                        raise DiagnosticStop('timeout')
                report['platform_url_has_video_id'] = bool(_video_id(str(response.url)))
                report.update(status='page_received', category='none')
    except DiagnosticStop as error:
        report['category'] = error.category
    except Exception as error:
        report['category'], report['http_status'] = classified_error(error)
    return report


def runtime_check(root):
    node = shutil.which('node')
    script = root/'frontend/scripts/capture-douyin.mjs'
    result = {'node_found': bool(node), 'capture_script_present': script.is_file(),
        'browser_installed': False, 'status': 'unavailable'}
    if not node or not script.is_file():
        return result
    try:
        process = subprocess.run([node, str(script), '--check-runtime'], cwd=root,
            capture_output=True, text=True, encoding='utf-8', timeout=RUNTIME_SECONDS,
            env=helper_environment())
        if len(process.stdout) > 16384:
            return result
        raw = json.loads(process.stdout)
        if not isinstance(raw, dict):
            return result
        ready = process.returncode == 0 and raw.get('ok') is True and raw.get('browser_installed') is True
        result.update(browser_installed=ready, status='ready' if ready else 'unavailable')
        version = raw.get('playwright_version')
        if isinstance(version, str) and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}', version):
            result['playwright_version'] = version
    except subprocess.TimeoutExpired:
        result['status'] = 'timeout'
    except Exception:
        pass
    return result


def clean_network_report(raw):
    """Whitelist worker output; never forward raw stdout or stderr."""
    result = {'status': 'failed', 'category': 'worker_error'}
    if (not isinstance(raw, dict) or not isinstance(raw.get('category'), str)
            or raw['category'] not in CATEGORIES):
        return result
    result['category'] = raw['category']
    if raw.get('status') in ('failed', 'page_received'):
        result['status'] = raw['status']
    status = raw.get('http_status')
    result['http_status'] = status if type(status) is int and 100 <= status <= 599 else None
    for key in ('platform_url_has_video_id', 'platform_redirect_has_video_id'):
        result[key] = raw.get(key) is True
    for key, maximum in (('origin_request_attempts', 6), ('page_bytes', PAGE_LIMIT+65536)):
        value = raw.get(key)
        result[key] = value if type(value) is int and 0 <= value <= maximum else None
    result['hops'] = []
    for hop in raw.get('hops', [])[:6] if isinstance(raw.get('hops'), list) else []:
        if not isinstance(hop, dict) or type(hop.get('status')) is not int or not 100 <= hop['status'] <= 599:
            continue
        value = {'status': hop['status']}
        for key in ('request_url_has_video_id', 'redirect_is_platform', 'redirect_url_has_video_id'):
            if key in hop:
                value[key] = hop[key] is True
        result['hops'].append(value)
    return result


def run_network_worker(url, root):
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker'],
            input=json.dumps({'url': url}), cwd=root, capture_output=True,
            text=True, encoding='utf-8', timeout=NETWORK_SECONDS, env=helper_environment())
        if result.returncode != 0 or len(result.stdout) > 16384:
            return {'status': 'failed', 'category': 'worker_error'}
        return clean_network_report(json.loads(result.stdout))
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for this worker on timeout. The worker
        # never starts children, so no browser descendants can remain.
        return {'status': 'failed', 'category': 'timeout', 'hard_deadline_seconds': NETWORK_SECONDS}
    except Exception:
        return {'status': 'failed', 'category': 'worker_error'}


def main():
    logging.disable(sys.maxsize)
    sys.dont_write_bytecode = True
    root = project_root()
    parser = SafeParser(description='Read-only share-page diagnostic; no original media or model calls.')
    parser.add_argument('--url')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if root is None:
        print(json.dumps({'status': 'failed', 'category': 'missing_project'})); return 1
    sys.path.insert(0, str(root))
    if args.worker:
        try:
            raw = json.loads(sys.stdin.read(16384))
            url = normalize_input(raw.get('url')) if isinstance(raw, dict) else None
            result = probe_page(url) if url else {'status': 'failed', 'category': 'invalid_input'}
        except ImportError:
            result = {'status': 'failed', 'category': 'missing_dependencies'}
        except Exception:
            result = {'status': 'failed', 'category': 'worker_error'}
        print(json.dumps(clean_network_report(result), ensure_ascii=True)); return 0
    url = normalize_input(args.url)
    if not url:
        print(json.dumps({'status': 'failed', 'category': 'invalid_input'})); return 1
    try:
        from backend import __version__
        version = __version__ if re.fullmatch(r'\d+\.\d+\.\d+', __version__) else 'unknown'
    except Exception:
        version = 'unknown'
    started = time.monotonic()
    report = {'diagnostic_version': 1, 'application_version': version,
        'system': platform.system() if platform.system() in ('Windows', 'Darwin', 'Linux') else 'other',
        'python': platform.python_version(),
        'proxy_environment_present': any(key in os.environ for key in
            ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')),
        'custom_tls_environment_present': any(key in os.environ for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR')),
        'runtime': runtime_check(root),
        'ffmpeg_found': bool(shutil.which('ffmpeg')), 'ffprobe_found': bool(shutil.which('ffprobe')),
        'network': run_network_worker(url, root),
        'scope': 'Share-page requests and runtime file checks only. No browser launch, original media pipeline, model calls, app credential reads, database writes or user browser sessions.',
        'interpretation': 'A page response, URL video ID or installed browser does not prove original media acquisition or analysis.'}
    report['elapsed_seconds'] = round(time.monotonic()-started, 3)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
