"""Safe task observations, separate from unobservable supplier generation progress."""
from datetime import datetime, timezone


PHASE_DETAILS = {
    'queued': '任务已排队，等待本机工作线程接手',
    'public_page': '正在读取公开页面，查找指定视频的原片来源',
    'browser': '正在通过项目浏览器读取公开页面和媒体；网页流量不等于原片下载完成',
    'validating_media': '正在核对媒体完整性、视频身份和实际音视频轨道',
    'merging_media': '正在本机整理已取得的媒体，必要时合并音视频轨道',
    'audio_chunks': '正在本机准备音频分段',
    'frames': '正在本机准备抽样画面',
    'github_metadata': '正在读取 GitHub 仓库信息和固定版本',
    'github_tree': '正在读取 GitHub 文件目录',
    'github_files': '正在检查 GitHub 来源文件',
    'github_fallback': 'GitHub API 暂不可用，正在通过匿名公开 Git 读取固定版本',
    'model_transcription': '正在等待当前音频段的转写结果；服务内部进度不可测量',
    'transcription_complete': '已保存实际返回的音频转写结果',
    'model_vision': '正在等待抽样画面的理解结果；服务内部进度不可测量',
    'vision_complete': '已保存实际返回的抽样画面分析',
    'model_analysis': '正在等待内容分析结果；服务内部进度不可测量',
    'project_links': '正在根据已保存证据检索和核验 GitHub 项目',
    'retry_wait': '本轮请求失败，正在短暂等待后自动重试',
    'saving': '正在保存分析和知识卡片',
    'complete': '分析和知识卡片已保存',
}
METRIC_KEYS = frozenset({
    'completed', 'total', 'requests', 'downloaded_bytes', 'proxy_connections',
    'proxy_bytes', 'media_responses', 'complete_bound_media', 'transcribed_chunks',
    'total_chunks', 'sampled_frames', 'total_frames', 'files_read', 'files_total',
    'retry_attempt', 'max_attempts',
})
STAGE_PHASES = {
    'queued': 'queued', 'fetch': 'public_page', 'media': 'validating_media',
    'transcription': 'model_transcription', 'vision': 'model_vision',
    'analysis': 'model_analysis', 'project_links': 'project_links',
    'save': 'saving', 'complete': 'complete',
}


def safe_observation(event):
    """No free-form observer text, URLs, IDs, headers, or supplier payloads."""
    if not isinstance(event, dict) or not isinstance(event.get('phase'), str) or event['phase'] not in PHASE_DETAILS:
        return None
    phase = event['phase']
    metrics = {k: v for k, v in event.items()
               if k in METRIC_KEYS and type(v) is int and 0 <= v <= 2**53 - 1}
    return {'phase': phase, 'detail': PHASE_DETAILS[phase], 'metrics': metrics}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, OverflowError):
        return None


def safe_progress_record(record):
    """Legacy or malformed telemetry never forwards arbitrary observer fields."""
    if not isinstance(record, dict):
        return None
    metrics = record.get('metrics') if isinstance(record.get('metrics'), dict) else {}
    value = safe_observation({**metrics, 'phase': record.get('phase')})
    if value is None:
        return None
    value.update(stage=record.get('stage') if isinstance(record.get('stage'), str) and record['stage'] in STAGE_PHASES else '',
                 at=record.get('at') if timestamp(record.get('at')) else None)
    return value


def seconds_since(start, end):
    a, b = timestamp(start), timestamp(end)
    return max(0, int((b-a).total_seconds())) if a and b else 0


def live_snapshot(task, observed_at, worker_active=False, queue_position=None):
    """A read never advances actual activity or persists a synthetic heartbeat."""
    events = task.get('stages') or []
    latest = events[-1] if events else {}
    started = task.get('stage_started_at')
    if not started:
        # Legacy tasks have no explicit step timer. Use the start of the most
        # recent contiguous run of this stage, not an unrelated old attempt.
        for event in reversed(events):
            if event.get('stage') != task.get('stage'):
                break
            if started and event.get('status') in ('failed', 'retrying', 'cancelled'):
                break
            started = event.get('at') or started
    started = started or task.get('started_at') or task.get('created_at')
    progress = safe_progress_record(task.get('progress')) or {}
    if progress.get('stage') != task.get('stage'):
        progress = {}
    activity = task.get('last_activity_at') or progress.get('at') or latest.get('at') or task.get('updated_at') or task.get('created_at')
    end = observed_at if task.get('status') in ('queued', 'running') else task.get('ended_at') or task.get('updated_at') or observed_at
    phase = progress.get('phase') if isinstance(progress.get('phase'), str) else ''
    phase = phase or STAGE_PHASES.get(task.get('stage'), '')
    waiting = {'fetch': 'source', 'media': 'local_media', 'transcription': 'transcription_service',
               'vision': 'vision_service', 'analysis': 'text_service', 'project_links': 'github'}.get(task.get('stage'), 'none')
    if task.get('status') == 'queued':
        phase, waiting = 'queued', 'queue'
    elif task.get('status') not in ('running',) and not worker_active:
        waiting = 'none'
    detail = progress.get('detail') or PHASE_DETAILS.get(phase, latest.get('message') or '等待处理')
    if task.get('status') == 'queued':
        detail = PHASE_DETAILS['queued']
    if task.get('status') in ('failed', 'waiting_material', 'waiting_config'):
        detail = (task.get('error') or {}).get('message') or '当前步骤已停止，请查看原因后继续'
    if task.get('status') == 'cancelled':
        detail = '已取消；正在等待在途请求结束，后续步骤不会继续' if worker_active else '任务已取消，工作线程已停止处理本任务'
    result = {'observed_at': observed_at, 'worker_active': bool(worker_active),
              'last_activity_at': activity, 'stage_started_at': started,
              'elapsed_seconds': seconds_since(task.get('started_at') or task.get('created_at'), end),
              'stage_elapsed_seconds': seconds_since(started, end),
              'activity_age_seconds': seconds_since(activity, observed_at),
              'cancellation_wait_seconds': seconds_since(task.get('ended_at'), observed_at) if task.get('status') == 'cancelled' and worker_active else 0,
              'phase': phase, 'detail': detail, 'waiting_on': waiting,
              'metrics': (safe_observation({'phase': phase, **(progress.get('metrics') if isinstance(progress.get('metrics'), dict) else {})}) or {}).get('metrics', {})}
    if queue_position is not None:
        result['queue_position'] = queue_position
    return result
