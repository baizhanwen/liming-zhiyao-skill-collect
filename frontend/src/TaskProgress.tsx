import { useEffect, useState } from 'react';
import { stageNames } from './api';
import type { Doc } from './api';
import './TaskProgress.css';

export type TaskRefreshState = {
  pending: boolean; lastSuccessAt: number | null; lastAttemptAt: number | null;
  error: string | null; failures: number; failureStartedAt: number | null;
};
export const initialTaskRefresh: TaskRefreshState = {pending: false, lastSuccessAt: null, lastAttemptAt: null, error: null, failures: 0, failureStartedAt: null};

export function formatElapsedSeconds(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—';
  const seconds = Math.floor(value), days = Math.floor(seconds / 86400), hours = Math.floor(seconds / 3600) % 24;
  const minutes = Math.floor(seconds / 60) % 60, remainder = seconds % 60;
  return days ? `${days}天${hours}小时` : hours ? `${hours}小时${minutes}分` : minutes ? `${minutes}分${remainder}秒` : `${remainder}秒`;
}

const phases: Record<string, string> = {
  public_page: '读取公开页面', browser: '浏览器读取媒体', validating_media: '核对原片与音画轨道', merging_media: '整理 / 合并音视频',
  audio_chunks: '分段音轨', frames: '准备抽样画面', github_metadata: '读取仓库元数据', github_tree: '扫描仓库目录',
  github_files: '检查所选来源文件', github_fallback: '通过公开 Git 读取来源', model_transcription: '等待转写服务响应',
  model_vision: '等待视觉服务响应', model_analysis: '等待文字服务响应', transcription_complete: '已收到音频转写',
  vision_complete: '已收到画面分析', retry_wait: '等待有限重试', saving: '保存结果', complete: '处理完成',
};
const waitLabels: Record<string, string> = {source: '来源服务', local_media: '本地媒体处理', transcription_service: '转写服务', vision_service: '视觉服务', text_service: '文字服务', github: 'GitHub', queue: '工作队列'};
const terminal = new Set(['succeeded', 'success', 'completed', 'failed', 'cancelled', 'canceled', 'waiting_config', 'waiting_material', 'needs_config', 'needs_material']);

function secondsBetween(start: unknown, end: number | null): number | null {
  if (typeof start !== 'string' || end == null) return null;
  const timestamp = Date.parse(start);
  return Number.isFinite(timestamp) ? Math.max(0, (end - timestamp) / 1000) : null;
}
function measured(value: unknown): value is number {return typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value) && value >= 0;}
function bytes(value: number): string {return value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : value >= 1024 ? `${Math.round(value / 1024)} KB` : `${value} B`;}
function metricsFor(metrics: Doc, phase: string): string[] {
  const items: string[] = [];
  if (measured(metrics.completed) && measured(metrics.total) && metrics.total > 0) items.push(`${phases[phase] || '本步骤'}：${metrics.completed} / ${metrics.total}`);
  if (measured(metrics.transcribed_chunks)) items.push(`已转写音频段：${metrics.transcribed_chunks}${measured(metrics.total_chunks) ? ` / ${metrics.total_chunks}` : ''}`);
  if (measured(metrics.sampled_frames)) items.push(`已准备抽样画面：${metrics.sampled_frames}${measured(metrics.total_frames) ? ` / ${metrics.total_frames}` : ''}`);
  if (measured(metrics.files_read)) items.push(`已纳入来源快照：${metrics.files_read} 个文本文件`);
  if (measured(metrics.downloaded_bytes)) items.push(`实际取回数据：${bytes(metrics.downloaded_bytes)}`);
  if (measured(metrics.proxy_bytes)) items.push(`网页代理总流量：${bytes(metrics.proxy_bytes)}`);
  if (measured(metrics.requests)) items.push(`已收到来源响应：${metrics.requests} 次`);
  if (measured(metrics.media_responses)) items.push(`媒体响应：${metrics.media_responses} 次`);
  if (measured(metrics.complete_bound_media)) items.push(`完整视频候选：${metrics.complete_bound_media}`);
  if (measured(metrics.retry_attempt)) items.push(`本轮尝试：${metrics.retry_attempt}${measured(metrics.max_attempts) ? ` / ${metrics.max_attempts}` : ''}`);
  return items;
}

export default function TaskProgress({task, connection, stageLabel, onRefresh, onOpen, compact = false}: {
  task: Doc; connection: TaskRefreshState; stageLabel?: string; onRefresh?: () => void; onOpen?: () => void; compact?: boolean;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {setNow(Date.now()); const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer);}, [task.id]);
  const live = task.live || {}, progress = task.progress || {}, hasLive = typeof live.worker_active === 'boolean';
  const events: Doc[] = Array.isArray(task.stages) ? task.stages : [];
  const lastEvent = events.at(-1) || {}, queued = ['queued', 'pending'].includes(task.status);
  const phase = queued ? '' : live.phase || progress.phase || '';
  const active = ['queued', 'pending', 'running'].includes(task.status), cancelled = ['cancelled', 'canceled'].includes(task.status);
  const cancelling = cancelled && live.worker_active === true;
  const ageOfRead = connection.lastSuccessAt == null ? null : Math.max(0, (now - connection.lastSuccessAt) / 1000);
  const readFresh = !connection.error && ageOfRead != null && ageOfRead < 12;
  const sinceRead = ageOfRead ?? 0;
  // Freeze extrapolation at the first failed read or a stale snapshot, instead
  // of jumping back to the original server value when connection state changes.
  const extrapolation = Math.min(12, connection.error && connection.failureStartedAt != null && connection.lastSuccessAt != null
    ? Math.max(0, (connection.failureStartedAt - connection.lastSuccessAt) / 1000) : sinceRead);
  // Legacy responses lack measured durations. Anchor them to a confirmed read
  // too, so a disconnected clock cannot continue beyond its stated estimate.
  const end = terminal.has(task.status)
    ? task.ended_at && Number.isFinite(Date.parse(task.ended_at)) ? Date.parse(task.ended_at) : null
    : connection.lastSuccessAt == null ? null : connection.lastSuccessAt + (active ? extrapolation * 1000 : 0);
  const elapsed = measured(live.elapsed_seconds) || (typeof live.elapsed_seconds === 'number' && Number.isFinite(live.elapsed_seconds))
    ? Math.max(0, live.elapsed_seconds) + (active ? extrapolation : 0)
    : secondsBetween(task.started_at || (['queued', 'pending'].includes(task.status) ? task.created_at : null), end);
  const stageElapsed = typeof live.stage_elapsed_seconds === 'number' && Number.isFinite(live.stage_elapsed_seconds)
    ? Math.max(0, live.stage_elapsed_seconds) + (active ? extrapolation : 0)
    : secondsBetween(live.stage_started_at || [...events].reverse().find(event => event.stage === task.stage)?.at, end);
  const activityAge = typeof live.activity_age_seconds === 'number' && Number.isFinite(live.activity_age_seconds)
    ? Math.max(0, live.activity_age_seconds) + sinceRead : secondsBetween(live.last_activity_at || progress.at || lastEvent.at, now);
  const cancellationWait = cancelling && typeof live.cancellation_wait_seconds === 'number' && Number.isFinite(live.cancellation_wait_seconds)
    ? Math.max(0, live.cancellation_wait_seconds) + extrapolation : null;
  const stage = stageLabel || stageNames[task.stage] || task.stage || '状态待读取';
  const heading = cancelling ? '取消已请求，等待当前请求结束' : cancelled ? '已取消后续处理'
    : ['queued', 'pending'].includes(task.status) ? '任务正在排队' : task.status === 'running' ? `当前阶段：${stage}`
    : task.status === 'failed' ? '任务失败' : ['waiting_material', 'needs_material'].includes(task.status) ? '来源获取未完成'
    : ['waiting_config', 'needs_config'].includes(task.status) ? task.error?.code === 'interrupted_request' ? '等待手动确认后继续' : '需要配置后继续'
    : ['succeeded', 'success', 'completed'].includes(task.status) ? '处理已完成' : '正在读取任务状态';
  const detail = String(live.detail || progress.detail || lastEvent.message || (['queued', 'pending'].includes(task.status) ? '等待后台接手，不会重复提交任务。' : '等待下一条已保存的阶段记录。')).replace(/工作线程/g, '后台');
  const metricItems = queued ? [] : metricsFor(live.metrics || progress.metrics || {}, phase);
  const noActivity = active && task.status !== 'queued' && task.status !== 'pending' && activityAge != null && activityAge >= 60;
  const refreshLabel = connection.lastSuccessAt == null ? '尚未成功刷新状态' : `最近成功刷新：${new Date(connection.lastSuccessAt).toLocaleTimeString('zh-CN', {hour12: false})}（${formatElapsedSeconds(ageOfRead)}前）`;
  const workerLabel = cancelling ? '正在结束在途请求；收到停止确认前，不会显示为全部停止。'
    : hasLive ? task.status === 'running' ? live.worker_active ? '后台正在处理本任务。' : '暂未确认后台执行状态，请结合最近刷新和阶段记录查看。'
      : cancelled ? '后台已停止处理本任务。' : null
      : cancelled ? '取消已记录；当前接口没有提供在途请求是否结束的信息。' : active ? '暂未确认后台执行状态；当前接口未提供实时后台信息，不能仅凭旧记录判断任务已停止。' : null;
  return <section className={`task-progress-card${compact ? ' compact' : ''}`} aria-label={compact ? '正在进行的任务进度' : '当前任务进度'}>
    <div className="task-progress-heading"><div><span className="task-progress-eyebrow">{compact ? '正在进行的任务' : '任务进度'}</span><h2 aria-live="polite"><span className={`task-progress-dot${active || cancelling ? ' active' : ''}`}/>{heading}</h2></div>
      <div className="task-progress-buttons">{onOpen && <button className="button" type="button" onClick={onOpen}>查看任务进度</button>}{onRefresh && <button className="button" type="button" aria-busy={connection.pending} onClick={onRefresh} disabled={connection.pending}>{connection.pending ? '正在刷新状态…' : '刷新状态'}</button>}</div></div>
    {compact && <p className="task-progress-title">{task.title || '已保存的任务'}</p>}
    {task.status !== 'running' && <p className="task-progress-worker">所在步骤：{stage}</p>}
    <div className="task-progress-clock" aria-live="off"><span>任务历时 <strong>{formatElapsedSeconds(elapsed)}</strong></span><span>本步骤已用时 <strong>{formatElapsedSeconds(stageElapsed)}</strong></span>{measured(task.attempts) && task.attempts > 0 && <span>已尝试 <strong>{task.attempts}</strong> 次</span>}{cancellationWait != null && <span>等待请求结束 <strong>{formatElapsedSeconds(cancellationWait)}</strong></span>}{measured(live.queue_position) && live.queue_position > 0 && <span>队列第 <strong>{live.queue_position}</strong> 位</span>}</div>
    <p className="task-progress-detail">{phases[phase] && <strong>{phases[phase]} · </strong>}{detail}</p>
    {workerLabel && <p className="task-progress-worker">{workerLabel}</p>}
    {['transcription_service', 'vision_service', 'text_service'].includes(live.waiting_on) && task.status === 'running' && <p className="task-progress-wait">等待{waitLabels[live.waiting_on]}返回下一步结果，当前调用没有可测量的生成百分比。</p>}
    {metricItems.length > 0 && <ul className="task-progress-metrics">{metricItems.slice(0, compact ? 3 : 10).map(item => <li key={item}>{item}</li>)}</ul>}
    <div className="task-progress-connection" aria-live="polite">{connection.error ? <p role="alert"><strong>任务状态刷新失败</strong>（连续 {connection.failures} 次）：{connection.error}。保留上次结果，正在自动重连。</p>
      : connection.pending && connection.lastSuccessAt == null ? <p>正在读取状态，尚未确认当前进展。</p>
      : connection.pending && ageOfRead != null && ageOfRead >= 8 ? <p>状态读取仍在等待响应，暂不能确认最新进展。</p>
      : ageOfRead != null && ageOfRead >= 12 ? <p>较久未成功刷新，当前显示上次状态。</p>
      : connection.lastSuccessAt != null ? <p>状态连接正常，定时刷新中。</p> : <p>实时状态尚未读取。</p>}</div>
    {noActivity && <p className="task-progress-slow">当前步骤已等待较久，已有 {formatElapsedSeconds(activityAge)}未收到新进展；{readFresh ? '状态连接正常。' : '请先核对状态连接。'}外部请求或本地处理可能仍在等待，不能据此判断已卡死。</p>}
    <div className="task-progress-freshness"><small>{refreshLabel}</small>{activityAge != null && <small>最近真实进展：{formatElapsedSeconds(activityAge)}前</small>}</div>
    {(connection.error || (ageOfRead != null && ageOfRead >= 12)) && <p className="task-progress-snapshot">用时根据最近快照短暂估算后已停止增加，刷新恢复后再更新；刷新失败不等于任务处理失败。</p>}
  </section>;
}
