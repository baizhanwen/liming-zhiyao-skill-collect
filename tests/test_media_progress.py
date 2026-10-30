"""Offline media progress contracts; no browser, supplier, model, or user data."""
import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest

import backend.browser_capture as browser
import backend.media as media
from backend.security import AppError


VIDEO = '1234567890123456789'
PAGE = 'https://www.douyin.com/video/' + VIDEO
SIGNED = 'https://cdn.contract.invalid/clip.mp4?signature=SYNTHETIC_PRIVATE'
PHASES = {'public_page', 'browser', 'validating_media', 'merging_media', 'audio_chunks', 'frames'}
COUNTS = {'completed', 'total', 'requests', 'downloaded_bytes', 'proxy_connections', 'proxy_bytes',
          'media_responses', 'complete_bound_media'}


def assert_safe(events):
    for event in events:
        assert event['phase'] in PHASES
        assert set(event) <= {'phase', *COUNTS}
        assert all(type(value) is int and value >= 0 for key, value in event.items() if key != 'phase')
    serialized = json.dumps(events)
    assert 'SYNTHETIC_PRIVATE' not in serialized and 'https://' not in serialized


def static_origin(monkeypatch):
    page, binary, calls = b'<title>Explicit offline page</title>', b'explicit media fixture', []
    def fetch(url, *args, **kwargs):
        calls.append(url)
        return (page, PAGE, {}) if url == PAGE else (binary, SIGNED, {})
    monkeypatch.setattr(media, 'fetch_public', fetch)
    monkeypatch.setattr(media, '_published_media', lambda *args: [(SIGNED, 'explicit_contract')])
    monkeypatch.setattr(media, 'inspect_video', lambda path: {'duration': 10, 'video': True, 'audio': True})
    monkeypatch.setattr(browser, 'capture_douyin', lambda *args, **kwargs: pytest.fail('static fixture must not launch browser'))
    return page, binary, calls


def captured_files(folder, payloads=(b'AV fixture',)):
    work = Path(folder) / 'owned-contract-files'; work.mkdir(parents=True, exist_ok=True)
    files = []
    for index, payload in enumerate(payloads):
        path = work / f'{index}.mp4'; path.write_bytes(payload)
        files.append({'path': str(path), 'video_id': VIDEO, 'size': len(payload), 'complete': True,
                      'coverage': [[0, len(payload)]], 'sha256': hashlib.sha256(payload).hexdigest()})
    return {'media_paths': [item['path'] for item in files], 'media_files': files,
            'observed_video_id': VIDEO, 'resolved_url': PAGE, 'reported_duration': 10,
            'title': 'SYNTHETIC_PRIVATE title', 'acquisition': {}}


def probe_fixture(path):
    payload = Path(path).read_bytes()
    return {'duration': 10, 'video': not payload.startswith(b'AUDIO'), 'audio': not payload.startswith(b'VIDEO')}


def test_static_page_and_media_report_only_actual_responses_bytes_and_validation(tmp_path, monkeypatch):
    page, binary, calls = static_origin(monkeypatch)
    events = []
    result = media.douyin_source(PAGE, tmp_path, progress_callback=events.append)
    public = [event for event in events if event['phase'] == 'public_page']
    assert [event['requests'] for event in public] == [0, 1, 2]
    assert [event['downloaded_bytes'] for event in public] == [0, len(page), len(page) + len(binary)]
    assert all('total' not in event for event in public), 'unknown HTTP work must not become an estimated percent'
    assert [event['completed'] for event in events if event['phase'] == 'validating_media'] == [0, 1]
    assert calls == [PAGE, SIGNED] and Path(result['media_path']).read_bytes() == binary
    assert_safe(events)


def test_browser_fallback_forwards_filtered_measured_counts_and_deduplicates(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(media, 'fetch_public', lambda *args, **kwargs: (b'public summary only', PAGE, {}))
    monkeypatch.setattr(media, 'inspect_video', probe_fixture)
    cancelled = lambda: False
    def capture(url, folder, **options):
        assert options['cancelled'] is cancelled
        callback = options['progress_callback']
        raw = {'response_status_counts': {'200': 5, '206': 2}, 'proxy_connections': 3, 'proxy_bytes': 2048,
               'observed_media': 4, 'complete_media': 1, 'downloaded_bytes': 'not-a-count',
               'headers': {'cookie': 'SYNTHETIC_PRIVATE'}, 'observed_video_id': VIDEO,
               'title': 'SYNTHETIC_PRIVATE', 'metadata_diagnostics': [SIGNED], 'binding_diagnostic': {'url': SIGNED}}
        callback(raw)
        callback({**raw, 'metadata_diagnostics': ['changed private data is not progress']})
        callback({**raw, 'proxy_bytes': 4096})
        return captured_files(folder)
    monkeypatch.setattr(browser, 'capture_douyin', capture)
    result = media.douyin_source(PAGE, tmp_path, cancelled=cancelled, progress_callback=events.append)
    observed = [event for event in events if event['phase'] == 'browser']
    assert observed == [{'phase': 'browser'},
        {'phase': 'browser', 'requests': 7, 'proxy_connections': 3, 'proxy_bytes': 2048,
         'media_responses': 4, 'complete_bound_media': 1},
        {'phase': 'browser', 'requests': 7, 'proxy_connections': 3, 'proxy_bytes': 4096,
         'media_responses': 4, 'complete_bound_media': 1}]
    assert 'downloaded_bytes' not in observed[-1]
    assert Path(result['media_path']).is_file()
    assert_safe(events)


def test_invalid_browser_counter_types_do_not_create_progress(tmp_path, monkeypatch):
    events = []
    def capture(url, folder, **options):
        options['progress_callback']({'proxy_connections': True, 'proxy_bytes': -1,
            'observed_media': 1.5, 'complete_media': '2', 'response_status_counts': {'200': False},
            'requests': 2**80, 'downloaded_bytes': float('nan')})
        return captured_files(folder)
    monkeypatch.setattr(browser, 'capture_douyin', capture)
    monkeypatch.setattr(media, 'inspect_video', probe_fixture)
    media._browser_douyin_source(PAGE, tmp_path, VIDEO, progress_callback=events.append)
    assert [event for event in events if event['phase'] == 'browser'] == [{'phase': 'browser'}]
    assert_safe(events)


@pytest.mark.parametrize('exception', [RuntimeError('SYNTHETIC_PRIVATE'), AppError('observer_error', 'SYNTHETIC_PRIVATE')])
def test_optional_progress_failure_does_not_change_static_business_result(tmp_path, monkeypatch, exception):
    _, binary, _ = static_origin(monkeypatch)
    def broken(event):
        raise exception
    result = media.douyin_source(PAGE, tmp_path, progress_callback=broken)
    assert Path(result['media_path']).read_bytes() == binary


@pytest.mark.parametrize('phase', ['public_page', 'validating_media'])
def test_cancelled_progress_is_not_swallowed_by_static_fallback(tmp_path, monkeypatch, phase):
    _, _, calls = static_origin(monkeypatch)
    cancellation = AppError('cancelled', 'explicit contract cancellation')
    def cancel(event):
        if event['phase'] == phase and (phase != 'public_page' or event.get('completed') == 1):
            raise cancellation
    with pytest.raises(AppError) as error:
        media.douyin_source(PAGE, tmp_path, progress_callback=cancel)
    assert error.value is cancellation
    assert len(calls) == (1 if phase == 'public_page' else 2)
    assert not (tmp_path / 'douyin.mp4').exists() and not (tmp_path / 'douyin.mp4.part').exists()


def test_split_track_validation_and_merge_counts_follow_actual_work(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(browser, 'capture_douyin', lambda url, folder, **options:
                        captured_files(folder, (b'VIDEO fixture', b'AUDIO fixture')))
    monkeypatch.setattr(media, 'inspect_video', probe_fixture)
    def mux(args):
        assert '-c' in args and args[args.index('-c') + 1] == 'copy'
        assert events[-1] == {'phase': 'merging_media', 'completed': 0, 'total': 1}
        Path(args[-1]).write_bytes(b'AV mux fixture')
    monkeypatch.setattr(media, 'run_media', mux)
    media._browser_douyin_source(PAGE, tmp_path, VIDEO, progress_callback=events.append)
    assert [event for event in events if event['phase'] == 'validating_media'] == [
        {'phase': 'validating_media', 'completed': 0, 'total': 2},
        {'phase': 'validating_media', 'completed': 1, 'total': 2},
        {'phase': 'validating_media', 'completed': 2, 'total': 2},
        {'phase': 'validating_media', 'completed': 0, 'total': 1},
        {'phase': 'validating_media', 'completed': 1, 'total': 1}]
    assert [event['completed'] for event in events if event['phase'] == 'merging_media'] == [0, 1]
    assert_safe(events)


def test_failed_merge_does_not_emit_merge_complete_or_leave_final_media(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(browser, 'capture_douyin', lambda url, folder, **options:
                        captured_files(folder, (b'VIDEO fixture', b'AUDIO fixture')))
    monkeypatch.setattr(media, 'inspect_video', probe_fixture)
    def fail(args):
        raise AppError('media_processing', 'explicit mux failure')
    monkeypatch.setattr(media, 'run_media', fail)
    with pytest.raises(AppError) as error:
        media._browser_douyin_source(PAGE, tmp_path, VIDEO, progress_callback=events.append)
    assert error.value.code == 'media_processing'
    assert [event['completed'] for event in events if event['phase'] == 'merging_media'] == [0]
    assert not (tmp_path / 'douyin.mp4').exists() and not (tmp_path / 'douyin.mp4.part').exists()


def test_filtered_browser_callback_propagates_cancel_before_media_validation(tmp_path, monkeypatch):
    def capture(url, folder, **options):
        options['progress_callback']({'proxy_connections': 1, 'metadata_diagnostics': [SIGNED]})
        pytest.fail('callback cancellation must terminate capture')
    monkeypatch.setattr(browser, 'capture_douyin', capture)
    monkeypatch.setattr(media, 'inspect_video', lambda *args: pytest.fail('cancel before file checks'))
    def cancel(event):
        if event.get('proxy_connections') == 1:
            raise AppError('cancelled', 'explicit browser cancellation')
    with pytest.raises(AppError) as error:
        media._browser_douyin_source(PAGE, tmp_path, VIDEO, progress_callback=cancel)
    assert error.value.code == 'cancelled'


def media_commands(monkeypatch, meta):
    calls = []
    monkeypatch.setattr(media, 'inspect_video', lambda path: dict(meta))
    def run(args):
        calls.append(args)
        assert args[args.index('-protocol_whitelist') + 1] == 'file,pipe'
        Path(args[-1]).write_bytes(b'explicit generated fixture')
    monkeypatch.setattr(media, 'run_media', run)
    return calls


def test_prepare_media_counts_real_and_reused_files_without_estimated_percent(tmp_path, monkeypatch):
    calls = media_commands(monkeypatch, {'duration': 250, 'audio': True, 'video': True})
    folder = tmp_path / 'processed'; folder.mkdir()
    (folder / 'audio-0.wav').write_bytes(b'explicit cached audio')
    (folder / 'frame-0.jpg').write_bytes(b'explicit cached frame')
    events = []
    prepared = media.prepare_media(tmp_path / 'fixture.mp4', folder, 'deep', progress_callback=events.append)
    assert [event['completed'] for event in events if event['phase'] == 'audio_chunks'] == [0, 1, 2, 3]
    assert [event['completed'] for event in events if event['phase'] == 'frames'] == list(range(9))
    assert len(calls) == 9 and len(prepared['chunks']) == 3 and len(prepared['frames']) == 8
    assert [item['time'] for item in prepared['chunks']] == [0, 120, 240]
    assert_safe(events)


@pytest.mark.parametrize('cancel', [False, True])
def test_failed_or_cancelled_audio_work_never_reports_remaining_work_complete(tmp_path, monkeypatch, cancel):
    calls = media_commands(monkeypatch, {'duration': 250, 'audio': True, 'video': True})
    original = media.run_media
    failure = AppError('media_processing', 'explicit segment failure')
    def run(args):
        if len(calls) == 1 and not cancel:
            raise failure
        return original(args)
    monkeypatch.setattr(media, 'run_media', run)
    events = []
    def progress(event):
        events.append(event)
        if cancel and event.get('phase') == 'audio_chunks' and event.get('completed') == 1:
            raise AppError('cancelled', 'explicit segment cancellation')
    with pytest.raises(AppError) as error:
        media.prepare_media(tmp_path / 'fixture.mp4', tmp_path / 'processed', progress_callback=progress)
    assert error.value.code == ('cancelled' if cancel else 'media_processing')
    assert [event['completed'] for event in events if event['phase'] == 'audio_chunks'] == [0, 1]
    assert not any(event['phase'] == 'frames' for event in events) and len(calls) == 1


@pytest.mark.parametrize('kind', ['cancelled', 'ordinary_error'])
def test_capture_progress_exception_branch_preserves_cancellation_without_launching_browser(tmp_path, monkeypatch, kind):
    """Run the real driver loop over an explicit in-memory child-process fixture."""
    class FakeGuard:
        server_address = ('127.0.0.1', 9)
        def __init__(self, *args):
            pass
        def serve_forever(self):
            pass
        def shutdown(self):
            pass
        def server_close(self):
            pass
    class FakeProcess:
        pid = 999999999
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(json.dumps({'type': 'progress', 'response_status_counts': {'200': 1}}) + '\n' +
                json.dumps({'type': 'result', 'ok': False, 'code': 'douyin_browser_material'}) + '\n')
            self.exited = False
        def poll(self):
            return 0 if self.exited else None
        def wait(self, timeout=None):
            self.exited = True
            return 0
    process = FakeProcess()
    monkeypatch.setattr(browser.shutil, 'which', lambda name: '/explicit-contract-node')
    monkeypatch.setattr(browser.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(browser, '_Guard', FakeGuard)
    monkeypatch.setattr(browser, '_kill', lambda process: None)
    calls = []
    def progress(event):
        calls.append(event)
        if kind == 'cancelled':
            raise AppError('cancelled', 'explicit driver cancellation')
        raise RuntimeError('SYNTHETIC_PRIVATE observer failure')
    with pytest.raises(AppError) as error:
        browser.capture_douyin(PAGE, tmp_path, video_id=VIDEO, timeout=3,
            transport_factory=lambda: httpx.MockTransport(lambda req: pytest.fail('no HTTP expected')),
            progress_callback=progress)
    assert error.value.code == ('cancelled' if kind == 'cancelled' else 'douyin_browser_material')
    assert len(calls) == 1 and process.exited and process.stdout.closed and process.stdin.closed
