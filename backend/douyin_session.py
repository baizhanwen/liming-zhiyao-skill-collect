"""Project-owned Douyin login; encrypted local state and metadata-only status.

The runner uses a fresh context, never a system profile. Its JSONL IPC is kept
in memory; cookies/localStorage are never logged or written to a plain file.
"""
from __future__ import annotations

import copy
import base64
import binascii
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .browser_capture import PROJECT, _PinnedProxy, _kill, _renderer_env
from .security import AppError

STATE_KEY = 'douyin_storage_state'
METADATA_ID = 'douyin_session'
MAX_STATE_BYTES = 256 * 1024
MAX_IPC_BYTES = 512 * 1024
MAX_IPC_TOTAL = 2 * 1024 * 1024
MAX_QR_BYTES = 64 * 1024
MAX_QR_IMAGES = 20
MAX_VERIFICATION_BYTES = 256 * 1024
MAX_VERIFICATION_TOTAL = 16 * 1024 * 1024
MAX_VERIFICATION_FRAMES = 48
MAX_VERIFICATION_ACTIONS = 128
VERIFICATION_KEYS = frozenset({'Tab', 'Enter', 'Escape', 'Backspace', 'Delete',
    'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'Space'})
VERIFICATION_MESSAGES = {
    'input_focus': '请先点击官方验证画面中的输入框，再输入内容',
    'action_failed': '本次操作未完成，请等待新的官方验证画面后重试',
    'stale_frame': '官方验证画面已变化，请等待新画面后再操作',
    'budget': '本次官方验证已达到操作上限，请重新打开登录窗口',
}
LOGIN_SECONDS = 600
CHECK_SECONDS = 45
LOCAL_STORAGE_KEYS = frozenset({'passport_csrf_token', 'passport_csrf_token_default', 's_v_web_id', 'ttwid'})
AUTH_COOKIES = frozenset({'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard'})
STATUSES = frozenset({'signed_out', 'opening', 'waiting_scan', 'waiting_confirmation', 'waiting_browser', 'checking', 'logged_in',
    'expired', 'verification_required', 'failed', 'cancelled'})
VIEW_STATUSES = frozenset({'waiting_browser', 'verification_required'})
MESSAGES = {
    'douyin_session_cancelled': '本次登录操作已取消',
    'douyin_session_timeout': '官方登录检查等待超时，可重新打开登录或检查会话',
    'douyin_session_not_authenticated': '官方页面尚未确认有效登录，请重新扫码或完成官方验证',
    'douyin_session_verification_required': '本次官方验证未完成，会话已结束；请重新连接后在项目内继续验证',
    'douyin_session_runtime': '项目登录浏览器不可用，请检查安装并重新运行 setup',
    'douyin_session_budget': '官方登录窗口超过本次网络预算，请稍后重新打开',
    'douyin_session_protocol': '项目登录检查未返回有效结果，请重新打开登录窗口',
    'douyin_session_storage': '本机会话未能安全保存或读取，请检查资料目录权限',
    'douyin_session_expired': '本地保存的认证 Cookie 已到期，请检查登录或重新扫码',
    'douyin_session_qr_expired': '官网二维码已失效，请点击重新获取二维码',
}


def _error(code):
    code = 'douyin_session_cancelled' if code == 'cancelled' else code
    code = code if isinstance(code, str) and code in MESSAGES else 'douyin_session_protocol'
    return {'code': code, 'message': MESSAGES[code]}


def _platform_host(host):
    return isinstance(host, str) and any(host == root or host.endswith('.' + root)
        for root in ('douyin.com', 'iesdouyin.com'))


def _text(value, maximum, *, empty=True):
    return (isinstance(value, str) and (empty or bool(value)) and len(value.encode('utf-8')) <= maximum
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def sanitize_storage_state(value):
    """Bound and independently filter a Playwright state to official HTTPS use.

    Cookie attributes retain the official site's values. Even cookies whose
    secure flag is false are only restored by a renderer limited to HTTPS.
    """
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol']) from None
    if len(encoded) > MAX_STATE_BYTES or not isinstance(value, dict):
        raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
    cookies, origins = value.get('cookies', []), value.get('origins', [])
    if not isinstance(cookies, list) or len(cookies) > 128 or not isinstance(origins, list) or len(origins) > 16:
        raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
    output = {'cookies': [], 'origins': []}
    seen_cookies, seen_origins = set(), set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
        domain = cookie.get('domain')
        if (not _text(domain, 253, empty=False)
                or not re.fullmatch(r'\.?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*', domain)
                or not _platform_host(domain.lstrip('.').lower())):
            continue
        name, content, path = cookie.get('name'), cookie.get('value'), cookie.get('path', '/')
        expiry = cookie.get('expires', -1)
        if (not _text(name, 128, empty=False) or not re.fullmatch(r'[!#$%&\'*+.^_`|~0-9A-Za-z-]+', name)
                or not _text(content, 8192) or not _text(path, 1024, empty=False) or not path.startswith('/')
                or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry < -1
                or expiry > 253402300799 or type(cookie.get('httpOnly')) is not bool
                or type(cookie.get('secure')) is not bool or cookie.get('sameSite') not in ('Strict', 'Lax', 'None')):
            raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
        key = (name, domain.lower(), path)
        if key in seen_cookies:
            raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
        seen_cookies.add(key)
        output['cookies'].append({'name': name, 'value': content, 'domain': domain.lower(),
            'path': path, 'expires': expiry, 'httpOnly': cookie['httpOnly'],
            'secure': cookie['secure'], 'sameSite': cookie['sameSite']})
    for item in origins:
        if not isinstance(item, dict) or not _text(item.get('origin'), 512, empty=False):
            raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
        try:
            parsed = urlsplit(item['origin'])
            allowed = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and _platform_host(parsed.hostname)
                and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            allowed = False
        if not allowed:
            continue
        origin = 'https://' + parsed.hostname + (':443' if parsed.port else '')
        if origin in seen_origins:
            raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
        seen_origins.add(origin)
        entries = item.get('localStorage', [])
        if not isinstance(entries, list) or len(entries) > 64:
            raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
        selected, names = [], set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
            name = entry.get('name')
            if not isinstance(name, str) or name not in LOCAL_STORAGE_KEYS:
                continue
            if name in names or not _text(entry.get('value'), 16384):
                raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
            names.add(name); selected.append({'name': name, 'value': entry['value']})
        if selected:
            output['origins'].append({'origin': origin, 'localStorage': selected})
    if not output['cookies'] and not output['origins']:
        raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
    return output


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _expired(value):
    return bool(value and datetime.fromisoformat(value).timestamp() <= time.time())


def _auth_expiry(state):
    expiries = [cookie['expires'] for cookie in state['cookies']
        if cookie['name'] in AUTH_COOKIES and cookie['expires'] >= 0]
    return datetime.fromtimestamp(min(expiries), timezone.utc).isoformat() if expiries else None


def _state_digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest() if state else None


def _qr_png(value):
    if not isinstance(value, str) or len(value) > ((MAX_QR_BYTES + 2) // 3) * 4:
        return None
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    if (not 45 <= len(data) <= MAX_QR_BYTES or data[:8] != b'\x89PNG\r\n\x1a\n'
            or data[8:16] != b'\x00\x00\x00\rIHDR'
            or not data.endswith(b'\x00\x00\x00\x00IEND\xaeB`\x82')
            or not 0 < int.from_bytes(data[16:20], 'big') <= 2048
            or not 0 < int.from_bytes(data[20:24], 'big') <= 2048):
        return None
    return data


def _frame_id(value):
    if type(value) is int and 0 <= value < 10 ** 20:
        value = str(value)
    if not isinstance(value, str):
        return None
    return value if re.fullmatch(r'(?:[0-9]{1,20}|[A-Fa-f0-9]{32}|[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12})', value) else None


def _verification_jpeg(value, width, height):
    if (type(width) is not int or type(height) is not int or not 0 < width <= 2048
            or not 0 < height <= 2048 or not isinstance(value, str)
            or len(value) > ((MAX_VERIFICATION_BYTES + 2) // 3) * 4):
        return None
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    if not 12 <= len(data) <= MAX_VERIFICATION_BYTES or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        return None
    position = 2
    # Read dimensions from the JPEG SOF header without decoding image pixels.
    while position + 4 <= len(data):
        if data[position] != 255:
            return None
        while position < len(data) and data[position] == 255:
            position += 1
        if position >= len(data):
            return None
        marker = data[position]; position += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            return None
        size = int.from_bytes(data[position:position + 2], 'big')
        if size < 2 or position + size > len(data):
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if size < 8:
                return None
            actual_height = int.from_bytes(data[position + 3:position + 5], 'big')
            actual_width = int.from_bytes(data[position + 5:position + 7], 'big')
            return data if (actual_width, actual_height) == (width, height) else None
        if marker == 0xDA:
            return None
        position += size
    return None


class _LoginBudget:
    def __init__(self, seconds, cancelled):
        self.deadline, self.cancelled = time.monotonic() + seconds, cancelled

    def _check(self):
        if self.cancelled():
            raise AppError('cancelled', MESSAGES['douyin_session_cancelled'])
        if time.monotonic() >= self.deadline:
            raise AppError('douyin_session_timeout', MESSAGES['douyin_session_timeout'])


class _NodeRunner:
    """Blocking JSONL runner; invoked only by the service's background thread."""
    def __init__(self):
        self.process = None
        self.stopping = threading.Event()
        self.write_lock = threading.Lock()

    def send(self, message):
        with self.write_lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.stdin.write(json.dumps(message, ensure_ascii=True) + '\n')
                    self.process.stdin.flush()
                except (OSError, ValueError):
                    pass

    def close(self):
        self.stopping.set()
        if self.process and self.process.poll() is None:
            # It is our dedicated process group, including its temporary browser.
            # Do not wait on stdin (a paused child may fill its pipe), or depend
            # on a daemon thread surviving application shutdown.
            try:
                _kill(self.process)
            except (OSError, subprocess.SubprocessError):
                pass

    def run(self, options, on_message, cancelled):
        node = shutil.which('node')
        script = PROJECT / 'frontend/scripts/douyin-login.mjs'
        if not node or not script.is_file():
            raise AppError('douyin_session_runtime', MESSAGES['douyin_session_runtime'])
        cancelled = lambda outer=cancelled: self.stopping.is_set() or outer()
        budget = _LoginBudget(options['timeout_ms'] / 1000, cancelled)
        proxy = _PinnedProxy(('127.0.0.1', 0), budget)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        messages, reader_stop = queue.Queue(maxsize=32), threading.Event()
        reader = None

        def enqueue(value):
            while not reader_stop.is_set():
                try:
                    messages.put(value, timeout=.2)
                    return
                except queue.Full:
                    continue

        def read():
            total, images, verification_total, verification_frames = 0, 0, 0, 0
            while not reader_stop.is_set():
                try:
                    line = self.process.stdout.readline(MAX_IPC_BYTES + 1)
                    length = len(line.encode('utf-8'))
                    if not line or length > MAX_IPC_BYTES or not line.endswith('\n'):
                        enqueue(None); return
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        enqueue(None); return
                    if value.get('type') == 'verification_frame':
                        verification_total += length
                        verification_frames += 1
                        if verification_total > MAX_VERIFICATION_TOTAL or verification_frames > MAX_VERIFICATION_FRAMES:
                            enqueue(None); return
                    else:
                        total += length
                        if total > MAX_IPC_TOTAL:
                            enqueue(None); return
                    if value.get('type') == 'qr':
                        images += 1
                        if images > MAX_QR_IMAGES:
                            enqueue(None); return
                    enqueue(value)
                except (ValueError, OSError, UnicodeError):
                    enqueue(None); return

        try:
            try:
                self.process = subprocess.Popen([node, str(script)], cwd=PROJECT, env=_renderer_env(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', start_new_session=(os.name != 'nt'))
            except OSError:
                raise AppError('douyin_session_runtime', MESSAGES['douyin_session_runtime']) from None
            reader = threading.Thread(target=read, daemon=True); reader.start()
            self.send({'type': 'start', **options,
                'guard_proxy': f'http://127.0.0.1:{proxy.server_address[1]}'})
            while True:
                budget._check()
                try:
                    message = messages.get(timeout=.2)
                except queue.Empty:
                    if self.process.poll() is not None:
                        break
                    continue
                if message is None:
                    break
                if message.get('type') == 'progress':
                    on_message({'type': 'progress', 'phase': message.get('phase')})
                elif message.get('type') == 'qr':
                    on_message({'type': 'qr', 'image_base64': message.get('image_base64')})
                elif message.get('type') == 'qr_clear':
                    on_message({'type': 'qr_clear'})
                elif message.get('type') == 'qr_expired':
                    on_message({'type': 'qr_expired'})
                elif message.get('type') == 'verification_frame':
                    on_message({key: message.get(key) for key in
                        ('type', 'image_base64', 'width', 'height', 'frame_id')})
                elif message.get('type') == 'verification_clear':
                    on_message({'type': 'verification_clear'})
                elif message.get('type') == 'verification_error':
                    on_message({'type': 'verification_error', 'code': message.get('code')})
                elif message.get('type') == 'result':
                    return message
            raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
        finally:
            reader_stop.set()
            self.close()
            if self.process:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _kill(self.process)
                if os.name != 'nt':
                    _kill(self.process)
                for stream in (self.process.stdin, self.process.stdout):
                    if stream:
                        stream.close()
            proxy.stop_tunnels()
            proxy.shutdown(); proxy.server_close(); proxy_thread.join(timeout=2)
            if reader:
                reader.join(timeout=1)
            while not messages.empty():
                try:
                    messages.get_nowait()
                except queue.Empty:
                    break


class DouyinSessionService:
    """One local session; injectable runners use run(options, callback, cancelled)."""
    def __init__(self, store, vault, runner_factory=None):
        self.store, self.vault = store, vault
        self.runner_factory = runner_factory or _NodeRunner
        self.lock = threading.RLock()
        self.generation, self.active, self.closed = 0, None, False
        self._state, self.verified_at, self.expires_at = None, None, None
        self._status, self._error, self.usable = 'signed_out', None, False
        try:
            metadata = self.store.get('connections', METADATA_ID) or {}
            raw = self.vault.get(STATE_KEY)
            if raw:
                if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_STATE_BYTES:
                    raise ValueError('invalid state')
                self._state = sanitize_storage_state(json.loads(raw))
                self.verified_at = _timestamp(metadata.get('verified_at'))
                self.expires_at = _auth_expiry(self._state)
                self.usable = bool(self.verified_at and metadata.get('usable') is True
                    and metadata.get('state_digest') == _state_digest(self._state))
                if self.usable:
                    self._status = 'logged_in'
                elif metadata.get('status') in ('expired', 'verification_required'):
                    self._status = metadata['status']
                else:
                    self._status, self._error = 'signed_out', _error('douyin_session_not_authenticated')
        except Exception:
            self._state, self.usable = None, False
            self._status, self._error = 'failed', _error('douyin_session_storage')

    def _snapshot_locked(self):
        status, error = self._status, self._error
        if not self.active and self.usable and _expired(self.expires_at):
            status, error = 'expired', _error('douyin_session_expired')
        return {'status': status, 'session_saved': self._state is not None,
            'verified_at': self.verified_at, 'expires_at': self.expires_at,
            'browser_open': bool(self.active and self.active['browser_open']),
            'qr_available': bool(self.active and self.active['qr_image']),
            'qr_updated_at': self.active['qr_updated_at'] if self.active else None,
            'verification_available': bool(self.active and status in VIEW_STATUSES
                and self.active['verification_image']),
            'verification_frame_id': self.active['verification_id'] if self.active else None,
            'verification_width': self.active['verification_width'] if self.active else None,
            'verification_height': self.active['verification_height'] if self.active else None,
            'verification_error': copy.deepcopy(self.active['verification_error']) if self.active else None,
            'error': copy.deepcopy(error)}

    def status(self):
        with self.lock:
            return self._snapshot_locked()

    def _persist_locked(self):
        try:
            metadata = {key: value for key, value in self._snapshot_locked().items()
                if key not in ('qr_available', 'qr_updated_at') and not key.startswith('verification_')}
            self.store.put('connections', {'id': METADATA_ID, **metadata,
                'usable': self.usable, 'state_digest': _state_digest(self._state)})
        except Exception:
            raise AppError('douyin_session_storage', MESSAGES['douyin_session_storage']) from None

    def _cancel_locked(self):
        old, self.active = self.active, None
        self.generation += 1
        if old:
            old['cancel'].set()
            self._clear_qr(old)
            self._clear_verification(old)
        return old.get('runner') if old else None

    @staticmethod
    def _stop(runner):
        if runner:
            try:
                runner.close()
            except Exception:
                pass

    def _start(self, mode, state=None):
        with self.lock:
            if self.closed:
                return self._snapshot_locked()
            old = self._cancel_locked()
            active = {'generation': self.generation, 'runner': None,
                'cancel': threading.Event(), 'browser_open': False, 'mode': mode,
                'qr_image': None, 'qr_updated_at': None, 'qr_count': 0, 'check_sent': False,
                'verification_image': None, 'verification_id': None, 'verification_node_id': None,
                'verification_width': None, 'verification_height': None, 'verification_error': None,
                'verification_count': 0, 'verification_actions': 0, 'verification_seen': set(),
                'verification_started': False}
            self.active = active
            self._status, self._error = 'opening', None
            try:
                self._persist_locked()
            except AppError:
                self.active = None
                self._status, self._error = 'failed', _error('douyin_session_storage')
            else:
                thread = threading.Thread(target=self._work, args=(active, mode, state),
                    name='douyin-login', daemon=True)
                active['thread'] = thread
                thread.start()
            result = self._snapshot_locked()
        self._stop(old)
        return result

    def start_login(self):
        return self._start('login')

    def check_login(self):
        command_runner = None
        with self.lock:
            if self.active:
                runner = self.active['runner']
                if runner and not self.active['check_sent']:
                    self.active['check_sent'] = True
                    command_runner = runner
                result = self._snapshot_locked()
            else:
                state = copy.deepcopy(self._state)
                if not state:
                    return self._snapshot_locked()
                return self._start('check', state)
        if command_runner:
            command_runner.send({'type': 'check'})
        return result

    def _work(self, active, mode, state):
        runner = None
        try:
            runner = self.runner_factory()
            with self.lock:
                if self.active is not active or active['cancel'].is_set():
                    return
                active['runner'] = runner
            options = {'mode': mode, 'timeout_ms': (LOGIN_SECONDS if mode == 'login' else CHECK_SECONDS) * 1000}
            if state is not None:
                options['storage_state'] = state

            def progress(message):
                kind = message.get('type')
                if kind in ('verification_frame', 'verification_clear', 'verification_error'):
                    with self.lock:
                        if self.active is not active or active['cancel'].is_set() or self._status not in VIEW_STATUSES:
                            return
                        if kind == 'verification_clear':
                            self._clear_verification(active)
                        elif kind == 'verification_error':
                            code = message.get('code')
                            if isinstance(code, str) and code in VERIFICATION_MESSAGES:
                                active['verification_error'] = {'code': code, 'message': VERIFICATION_MESSAGES[code]}
                        else:
                            identity = _frame_id(message.get('frame_id'))
                            image = _verification_jpeg(message.get('image_base64'), message.get('width'), message.get('height'))
                            if identity is None or image is None or identity in active['verification_seen']:
                                return
                            if active['verification_count'] >= MAX_VERIFICATION_FRAMES:
                                self._clear_verification(active)
                                active['verification_error'] = {'code': 'budget', 'message': VERIFICATION_MESSAGES['budget']}
                                return
                            active['verification_seen'].add(identity)
                            active['verification_count'] += 1
                            active['verification_image'] = image
                            active['verification_id'] = str(uuid.uuid4())
                            active['verification_node_id'] = identity
                            active['verification_width'], active['verification_height'] = message['width'], message['height']
                            active['verification_error'] = None
                    return
                if message.get('type') == 'qr_clear':
                    with self.lock:
                        if self.active is active and not active['cancel'].is_set() and mode == 'login':
                            self._clear_qr(active)
                    return
                if message.get('type') == 'qr_expired':
                    with self.lock:
                        if (self.active is active and not active['cancel'].is_set()
                                and mode == 'login' and not active['verification_started']):
                            self._clear_qr(active)
                            self._status = 'waiting_scan'
                            self._error = _error('douyin_session_qr_expired')
                    return
                if message.get('type') == 'qr':
                    with self.lock:
                        if (self.active is not active or active['cancel'].is_set()
                                or mode != 'login' or self._status in ('waiting_confirmation', 'waiting_browser')
                                or active['verification_started']):
                            return
                        image = _qr_png(message.get('image_base64'))
                        if image is None or image == active['qr_image']:
                            return
                        active['qr_count'] += 1
                        if active['qr_count'] > MAX_QR_IMAGES:
                            self._clear_qr(active)
                            return
                        active['qr_image'] = image
                        active['qr_updated_at'] = datetime.now(timezone.utc).isoformat()
                        if self._error and self._error['code'] == 'douyin_session_qr_expired':
                            self._error = None
                    return
                phase = message.get('phase')
                if phase not in ('opening', 'waiting_scan', 'waiting_confirmation', 'waiting_browser', 'checking', 'verification_required'):
                    return
                with self.lock:
                    if self.active is active and not active['cancel'].is_set():
                        if active['verification_started'] and phase in (
                                'opening', 'waiting_scan', 'waiting_confirmation', 'waiting_browser', 'checking'):
                            return
                        if (self._error and self._error['code'] == 'douyin_session_qr_expired'
                                and phase in ('opening', 'checking')):
                            return
                        if phase == 'verification_required':
                            active['verification_started'] = True
                            self._clear_qr(active)
                        previous_phase = self._status
                        self._status = phase
                        if not self._error or self._error['code'] != 'douyin_session_qr_expired':
                            self._error = None
                        active['check_sent'] = False
                        if phase in ('waiting_confirmation', 'waiting_browser') or (phase == 'checking' and mode == 'check'):
                            self._clear_qr(active)
                        if phase not in VIEW_STATUSES or phase != previous_phase:
                            self._clear_verification(active)
                        active['browser_open'] = True
                        self._persist_locked()

            result = runner.run(options, progress, active['cancel'].is_set)
            if not isinstance(result, dict):
                raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
            with self.lock:
                if self.active is not active or active['cancel'].is_set():
                    return
                if (result.get('ok') is True and result.get('authenticated') is True
                        and result.get('verification_method') == 'official_profile_response'):
                    sanitized = sanitize_storage_state(result.get('storage_state'))
                    expiry = _auth_expiry(sanitized)
                    if (_expired(expiry) or not any(cookie['name'] in ('sessionid', 'sessionid_ss', 'sid_tt')
                            and cookie['value'] for cookie in sanitized['cookies'])):
                        raise AppError('douyin_session_protocol', MESSAGES['douyin_session_protocol'])
                    try:
                        self.vault.set(STATE_KEY, json.dumps(sanitized, ensure_ascii=False, allow_nan=False))
                    except Exception:
                        raise AppError('douyin_session_storage', MESSAGES['douyin_session_storage']) from None
                    self._state, self.usable = sanitized, True
                    self.verified_at = datetime.now(timezone.utc).isoformat()
                    self.expires_at, self._status, self._error = expiry, 'logged_in', None
                else:
                    raw = result.get('error')
                    code = raw.get('code') if isinstance(raw, dict) else None
                    self._error = _error(code)
                    if code == 'douyin_session_not_authenticated' and mode == 'check':
                        self.usable, self._status = False, 'expired'
                    elif code == 'douyin_session_verification_required':
                        self.usable, self._status = False, 'verification_required'
                    elif code == 'douyin_session_cancelled':
                        self._status = 'cancelled'
                    else:
                        self._status = 'failed'
                self._clear_qr(active)
                self._clear_verification(active)
                self.active = None
                self._persist_locked()
        except Exception as exc:
            with self.lock:
                if self.generation == active['generation'] and not active['cancel'].is_set():
                    self._clear_qr(active)
                    self._clear_verification(active)
                    self.active = None
                    self._status = 'cancelled' if isinstance(exc, AppError) and exc.code == 'cancelled' else 'failed'
                    self._error = _error(exc.code if isinstance(exc, AppError) else 'douyin_session_protocol')
                    if isinstance(exc, AppError) and exc.code == 'douyin_session_storage':
                        self.usable = False
                    try:
                        self._persist_locked()
                    except Exception:
                        pass
        finally:
            self._stop(runner)

    def cancel_login(self):
        with self.lock:
            had_active = self.active is not None
            old = self._cancel_locked()
            if had_active:
                self._status, self._error = 'cancelled', None
            self._persist_locked()
            result = self._snapshot_locked()
        self._stop(old)
        return result

    def clear(self):
        with self.lock:
            old = self._cancel_locked()
            self._state, self.usable = None, False
            self.verified_at = self.expires_at = None
            self._status, self._error = 'signed_out', None
            try:
                self.vault.set(STATE_KEY, '')
            except Exception:
                self._status, self._error = 'failed', _error('douyin_session_storage')
            self._persist_locked()
            result = self._snapshot_locked()
        self._stop(old)
        return result

    def storage_state(self):
        """Internal-only secret snapshot; never expose through settings/API/logs."""
        with self.lock:
            return copy.deepcopy(self._state) if self.usable and not _expired(self.expires_at) else None

    @staticmethod
    def _clear_qr(active):
        active['qr_image'] = None
        active['qr_updated_at'] = None

    def qr_image(self, updated_at=None):
        """Current official QR image only; never persisted or returned as JSON."""
        with self.lock:
            if (not self.active or self.active['mode'] != 'login'
                    or (updated_at is not None and updated_at != self.active['qr_updated_at'])):
                return None
            return self.active['qr_image']

    @staticmethod
    def _clear_verification(active):
        for key in ('verification_image', 'verification_id', 'verification_node_id',
                    'verification_width', 'verification_height', 'verification_error'):
            active[key] = None

    def verification_image(self, frame_id=None):
        with self.lock:
            if (not self.active or self._status not in VIEW_STATUSES
                    or (frame_id is not None and frame_id != self.active['verification_id'])):
                return None
            return self.active['verification_image']

    def verification_action(self, value):
        """Relay an explicit user action to the exact current official frame."""
        with self.lock:
            active = self.active
            if (not active or active['cancel'].is_set() or self._status not in VIEW_STATUSES
                    or not active['verification_image'] or not isinstance(value, dict)
                    or value.get('frame_id') != active['verification_id']):
                raise AppError('douyin_verification_stale', VERIFICATION_MESSAGES['stale_frame'], status=409)
            kind = value.get('kind')
            allowed = {'click': {'frame_id', 'kind', 'x', 'y'}, 'text': {'frame_id', 'kind', 'text'},
                'key': {'frame_id', 'kind', 'key'}, 'refresh': {'frame_id', 'kind'}}
            if not isinstance(kind, str) or kind not in allowed or set(value) != allowed[kind]:
                raise AppError('douyin_verification_action', '官方验证操作格式无效')
            command = {'type': 'verification_action', 'frame_id': active['verification_node_id'], 'kind': kind}
            if kind == 'click':
                x, y = value['x'], value['y']
                if (type(x) is not int or type(y) is not int or not 0 <= x < active['verification_width']
                        or not 0 <= y < active['verification_height']):
                    raise AppError('douyin_verification_action', '请在当前官方验证画面范围内点击')
                command.update(x=x, y=y)
            elif kind == 'text':
                text = value['text']
                if (not isinstance(text, str) or not 0 < len(text) <= 128
                        or any(unicodedata.category(char).startswith('C') for char in text)):
                    raise AppError('douyin_verification_action', '输入内容最多128个字符，且不能包含控制字符')
                command['text'] = text
            elif kind == 'key':
                if not isinstance(value['key'], str) or value['key'] not in VERIFICATION_KEYS:
                    raise AppError('douyin_verification_action', '此按键不在官方验证操作范围内')
                command['key'] = value['key']
            if active['verification_actions'] >= MAX_VERIFICATION_ACTIONS:
                self._clear_verification(active)
                active['verification_error'] = {'code': 'budget', 'message': VERIFICATION_MESSAGES['budget']}
                raise AppError('douyin_verification_budget', VERIFICATION_MESSAGES['budget'], status=409)
            active['verification_actions'] += 1
            runner = active['runner']
            self._clear_verification(active)
        try:
            runner.send(command)
        except Exception:
            with self.lock:
                if self.active is active and self._status in VIEW_STATUSES:
                    active['verification_error'] = {'code': 'action_failed', 'message': VERIFICATION_MESSAGES['action_failed']}
        return self.status()

    def close(self):
        with self.lock:
            self.closed = True
            old = self._cancel_locked()
        self._stop(old)
