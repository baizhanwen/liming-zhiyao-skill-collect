"""Task observability contracts: only synthetic data and held local work."""
import threading

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.security import AppError, Vault
from backend.store import Store
from backend.task_progress import live_snapshot, safe_observation
from backend.tasks import TaskRunner


def fixture_runner(tmp_path, models=None):
    store = Store(tmp_path)
    runner = TaskRunner(store, Vault(tmp_path), models, lambda: None)
    task = runner.create('[显式契约] 本机等待与状态')
    runner.checkpoint(task['id'], source={'title': '[显式契约]', 'text': '本地固定材料',
        'basis': 'source_files', 'snapshot_hash': 'synthetic-source', 'evidence': []})
    return store, runner, task['id']


def active_observer(store, runner, id):
    store.put('tasks', {**store.get('tasks', id), 'status': 'running'})
    runner.run_tokens[id] = 'synthetic-generation'
    runner.stage(id, 'fetch', '[显式契约] 读取来源')
    return runner.observer(id, 'fetch')


@pytest.mark.parametrize('event', [None, [], {'phase': []}, {'phase': {}},
    {'phase': 'unknown'}, {'phase': 1}])
def test_invalid_observer_events_are_ignored(event):
    assert safe_observation(event) is None


def test_observer_accepts_only_known_nonnegative_integer_metrics():
    result = safe_observation({'phase': 'browser', 'proxy_bytes': 14, 'requests': 2,
        'completed': True, 'total': -1, 'downloaded_bytes': 1.5, 'files_read': 2**60,
        'url': 'https://synthetic-private.invalid/?key=synthetic', 'headers': {'Cookie': 'SYNTHETIC'},
        'message': 'SYNTHETIC PRIVATE PAYLOAD', 'observed_video_id': 'SYNTHETIC'})
    assert result['metrics'] == {'proxy_bytes': 14, 'requests': 2}
    assert 'SYNTHETIC' not in str(result) and 'synthetic-private' not in str(result)


def test_reads_advance_observed_time_without_faking_task_activity(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_worker=False)
    runner = app.state.runner
    first = runner.create('[契约] 排队一')
    second = runner.create('[契约] 排队二')
    before = app.state.store.get('tasks', first['id'])
    clock = ['2026-10-30T06:00:10+00:00']
    monkeypatch.setattr('backend.tasks.now', lambda: clock[0])
    with TestClient(app) as client:
        a = client.get('/api/tasks/' + first['id']).json()
        clock[0] = '2026-10-30T06:00:20+00:00'
        b = client.get('/api/tasks/' + first['id']).json()
        all_tasks = client.get('/api/tasks').json()
    assert a['live']['observed_at'] != b['live']['observed_at']
    assert a['live']['last_activity_at'] == b['live']['last_activity_at']
    assert not a['live']['worker_active'] and a['live']['queue_position'] == 1
    assert next(t for t in all_tasks if t['id'] == second['id'])['live']['queue_position'] == 2
    assert app.state.store.get('tasks', first['id']) == before
    assert 'live' not in app.state.store.get('tasks', first['id'])


def test_duplicate_capture_ticks_do_not_refresh_activity_or_write_database(tmp_path, monkeypatch):
    store, runner, id = fixture_runner(tmp_path)
    observe = active_observer(store, runner, id)
    clock = [100.0]
    monkeypatch.setattr('backend.tasks.time.monotonic', lambda: clock[0])
    assert observe({'phase': 'browser', 'proxy_bytes': 4096, 'requests': 3})
    before = store.get('tasks', id)
    clock[0] = 110.0
    assert not observe({'phase': 'browser', 'proxy_bytes': 4096, 'requests': 3,
        'metadata_diagnostics': 'SYNTHETIC SECRET', 'observed_video_id': 'SYNTHETIC'})
    assert store.get('tasks', id) == before
    assert observe({'phase': 'browser', 'proxy_bytes': 8192, 'requests': 4})
    assert store.get('tasks', id)['progress']['metrics']['proxy_bytes'] == 8192


def test_throttle_keeps_phase_completion_and_new_step_metrics(tmp_path, monkeypatch):
    store, runner, id = fixture_runner(tmp_path)
    observe = active_observer(store, runner, id)
    monkeypatch.setattr('backend.tasks.time.monotonic', lambda: 100.0)
    assert observe({'phase': 'frames', 'completed': 0, 'total': 8})
    assert not observe({'phase': 'frames', 'completed': 1, 'total': 8})
    assert observe({'phase': 'frames', 'completed': 8, 'total': 8})
    assert store.get('tasks', id)['progress']['metrics']['completed'] == 8
    runner.stage(id, 'vision', '[契约] 等待视觉')
    assert runner.observer(id, 'vision')({'phase': 'model_vision', 'sampled_frames': 8})
    assert store.get('tasks', id)['progress']['metrics'] == {'sampled_frames': 8}


def test_late_previous_run_and_previous_stage_observers_cannot_change_new_state(tmp_path):
    store, runner, id = fixture_runner(tmp_path)
    old = active_observer(store, runner, id)
    runner.run_tokens[id] = 'new-synthetic-generation'
    before = store.get('tasks', id)
    assert not old({'phase': 'browser', 'proxy_bytes': 100})
    assert store.get('tasks', id) == before
    current = runner.observer(id, 'fetch')
    runner.stage(id, 'analysis', '[契约] 等待文字')
    before = store.get('tasks', id)
    assert not current({'phase': 'browser', 'proxy_bytes': 100})
    assert store.get('tasks', id) == before
    # A retry may re-enter the same named stage in the same worker run.
    runner.stage(id, 'fetch', '[契约] 新一轮获取')
    before = store.get('tasks', id)
    assert not current({'phase': 'browser', 'proxy_bytes': 200})
    assert store.get('tasks', id) == before
    assert runner.observer(id, 'fetch')({'phase': 'browser', 'proxy_bytes': 10})


def test_legacy_telemetry_is_sanitized_on_read_and_before_merge(tmp_path):
    store, runner, id = fixture_runner(tmp_path)
    observe = active_observer(store, runner, id)
    raw = store.get('tasks', id)
    raw['progress'] = {'stage': 'fetch', 'phase': 'browser', 'at': '2026-10-30T06:00:00Z',
        'detail': 'SYNTHETIC SECRET', 'raw_headers': 'SYNTHETIC SECRET',
        'metrics': {'requests': 1, 'url': 'SYNTHETIC PRIVATE URL'}}
    store.put('tasks', raw)
    response = runner.present(store.get('tasks', id))
    assert 'SYNTHETIC' not in str(response['progress'])
    assert 'SYNTHETIC' not in str(response['live'])
    assert store.get('tasks', id)['progress'] == raw['progress']  # read only
    assert observe({'phase': 'browser', 'requests': 2})
    assert 'SYNTHETIC' not in str(store.get('tasks', id)['progress'])


def test_telemetry_storage_failure_does_not_fail_or_repeat_business_call(tmp_path, monkeypatch):
    class Models:
        calls = 0
        def analyze(self, source):
            self.calls += 1
            return {'overview': '[契约] 结果', 'steps': []}
    models = Models()
    store, runner, id = fixture_runner(tmp_path, models)
    put = store.put
    def progress_write_failure(kind, value):
        if kind == 'tasks' and (value.get('progress') or {}).get('metrics', {}).get('requests') == 1:
            raise OSError('[契约] 观测存储失败')
        return put(kind, value)
    monkeypatch.setattr(store, 'put', progress_write_failure)
    # An explicit synthetic source getter emits progress, then real orchestration
    # continues with the same single analysis invocation.
    original = runner._execute
    def execute(task_id):
        runner.stage(task_id, 'fetch', '[契约] 来源')
        assert not runner.observer(task_id, 'fetch')({'phase': 'browser', 'requests': 1})
        return original(task_id)
    monkeypatch.setattr(runner, '_execute', execute)
    runner.run(id)
    assert store.get('tasks', id)['status'] == 'succeeded'
    assert store.get('tasks', id)['attempts'] == 1 and models.calls == 1


def test_cancellation_observer_is_not_swallowed(tmp_path):
    store, runner, id = fixture_runner(tmp_path)
    observe = active_observer(store, runner, id)
    runner.cancel(id)
    with pytest.raises(AppError) as error:
        observe({'phase': 'browser', 'requests': 1})
    assert error.value.code == 'cancelled'


def test_actual_held_worker_and_cancelled_request_are_reported_honestly(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Models:
        def analyze(self, source):
            entered.set()
            assert release.wait(10), 'test must always release its held local operation'
            return {'overview': '[契约] 结果', 'steps': []}
    store, runner, id = fixture_runner(tmp_path, Models())
    worker = threading.Thread(target=runner.run, args=(id,))
    worker.start()
    try:
        assert entered.wait(5)
        first = runner.present(store.get('tasks', id))
        assert first['live']['worker_active'] and first['live']['waiting_on'] == 'text_service'
        assert '不可测量' in first['live']['detail']
        activity = first['live']['last_activity_at']
        assert runner.present(store.get('tasks', id))['live']['last_activity_at'] == activity
        runner.cancel(id)
        cancelled = runner.present(store.get('tasks', id))
        assert cancelled['status'] == 'cancelled' and cancelled['live']['worker_active']
        assert '在途请求' in cancelled['live']['detail']
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    stopped = runner.present(store.get('tasks', id))
    assert stopped['status'] == 'cancelled' and not stopped['live']['worker_active']
    assert '已停止' in stopped['live']['detail'] and not runner.active_ids


def test_retry_has_separate_visible_wait_and_does_not_leave_running_error(tmp_path, monkeypatch):
    class Models:
        calls = 0
        def analyze(self, source):
            self.calls += 1
            if self.calls == 1:
                raise AppError('timeout', '[契约] 服务超时', True)
            return {'overview': '[契约] 第二轮结果', 'steps': []}
    store, runner, id = fixture_runner(tmp_path, Models())
    observed = []
    def retry_wait(timeout):
        observed.append(store.get('tasks', id))
        return False
    monkeypatch.setattr(runner.stop, 'wait', retry_wait)
    runner.run(id)
    waiting = observed[0]
    assert waiting['status'] == 'running' and waiting['error'] is None
    assert waiting['last_error']['code'] == 'timeout'
    assert waiting['progress']['phase'] == 'retry_wait'
    assert waiting['progress']['metrics'] == {'retry_attempt': 2, 'max_attempts': 3}
    assert waiting['stages'][-1]['status'] == 'retrying'
    final = store.get('tasks', id)
    assert final['status'] == 'succeeded' and final['attempts'] == 2
    assert runner.models.calls == 2


def test_recovered_cumulative_attempts_do_not_become_an_impossible_retry_fraction(tmp_path, monkeypatch):
    class Models:
        calls = 0
        def analyze(self, source):
            self.calls += 1
            if self.calls == 1:
                raise AppError('timeout', '[契约] 恢复后首次超时', True)
            return {'overview': '[契约] 已完成', 'steps': []}
    store, runner, id = fixture_runner(tmp_path, Models())
    store.put('tasks', {**store.get('tasks', id), 'attempts': 3})
    waits = []
    monkeypatch.setattr(runner.stop, 'wait', lambda _: waits.append(store.get('tasks', id)) or False)
    runner.run(id)
    assert waits[0]['attempts'] == 5
    assert waits[0]['progress']['metrics'] == {'retry_attempt': 2, 'max_attempts': 3}
    assert '本轮自动尝试 2/3' in waits[0]['stages'][-1]['message']
    assert store.get('tasks', id)['status'] == 'succeeded'


def test_legacy_timing_stops_at_previous_failed_attempt_and_terminal_time_is_frozen():
    task = {'id': 'synthetic', 'status': 'failed', 'stage': 'fetch',
        'created_at': '2026-10-30T06:00:00Z', 'started_at': '2026-10-30T06:00:01Z',
        'ended_at': '2026-10-30T06:03:20Z', 'updated_at': '2026-10-30T06:03:20Z',
        'error': {'message': '[契约] 已停止'}, 'stages': [
            {'stage': 'fetch', 'status': 'running', 'at': '2026-10-30T06:00:02Z'},
            {'stage': 'fetch', 'status': 'failed', 'at': '2026-10-30T06:01:00Z'},
            {'stage': 'fetch', 'status': 'running', 'at': '2026-10-30T06:03:00Z'},
            {'stage': 'fetch', 'status': 'failed', 'at': '2026-10-30T06:03:20Z'}]}
    a = live_snapshot(task, '2026-10-30T08:00:00Z')
    b = live_snapshot(task, '2026-10-30T10:00:00Z')
    assert a['stage_elapsed_seconds'] == b['stage_elapsed_seconds'] == 20
    assert a['elapsed_seconds'] == b['elapsed_seconds'] == 199
    assert a['detail'] == '[契约] 已停止' and a['waiting_on'] == 'none'


def test_cancellation_duration_never_goes_back_when_worker_finishes():
    task = {'id': 'synthetic', 'status': 'cancelled', 'stage': 'analysis',
        'created_at': '2026-10-30T06:00:00Z', 'started_at': '2026-10-30T06:00:00Z',
        'ended_at': '2026-10-30T06:00:10Z', 'stage_started_at': '2026-10-30T06:00:01Z'}
    inflight = live_snapshot(task, '2026-10-30T06:00:30Z', worker_active=True)
    stopped = live_snapshot(task, '2026-10-30T06:00:40Z', worker_active=False)
    assert inflight['elapsed_seconds'] == stopped['elapsed_seconds'] == 10
    assert inflight['stage_elapsed_seconds'] == stopped['stage_elapsed_seconds'] == 9
    assert inflight['cancellation_wait_seconds'] == 20 and stopped['cancellation_wait_seconds'] == 0


@pytest.mark.parametrize('status,active', [('succeeded', False), ('cancelled', True)])
def test_last_activity_age_uses_current_observation_even_when_processing_time_is_frozen(status, active):
    task = {'id': 'synthetic', 'status': status, 'stage': 'complete' if status == 'succeeded' else 'analysis',
        'started_at': '2026-10-30T06:00:00Z', 'ended_at': '2026-10-30T06:00:10Z',
        'last_activity_at': '2026-10-30T06:00:10Z'}
    a = live_snapshot(task, '2026-10-30T06:00:40Z', worker_active=active)
    b = live_snapshot(task, '2026-10-30T06:01:10Z', worker_active=active)
    assert a['elapsed_seconds'] == b['elapsed_seconds'] == 10
    assert a['activity_age_seconds'] == 30 and b['activity_age_seconds'] == 60


def test_recovery_preserves_uncertain_model_boundary_and_resets_safe_queue(tmp_path):
    store, runner, id = fixture_runner(tmp_path)
    store.put('tasks', {**store.get('tasks', id), 'status': 'running', 'stage': 'vision'})
    second = runner.create('[契约] 获取时中断')
    store.put('tasks', {**second, 'status': 'running', 'stage': 'fetch',
        'progress': {'phase': 'browser', 'stage': 'fetch', 'at': '2020-01-01T00:00:00Z'}})
    runner.recover()
    uncertain = runner.present(store.get('tasks', id))
    assert uncertain['status'] == 'waiting_config' and uncertain['error']['code'] == 'interrupted_request'
    assert '未确认' in uncertain['live']['detail']
    queued = runner.present(store.get('tasks', second['id']))
    assert queued['status'] == 'queued' and queued['stage'] == 'queued'
    assert queued['live']['phase'] == 'queued' and not queued['live']['worker_active']
