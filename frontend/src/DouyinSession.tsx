import {useEffect, useRef, useState} from 'react';
import {api} from './api';
import DouyinVerification from './DouyinVerification';
import type {VerificationAction} from './DouyinVerification';
import './DouyinSession.css';

const statusLabels = {
  signed_out: '未连接抖音', opening: '正在准备官方二维码', waiting_scan: '等待手机扫码',
  waiting_confirmation: '已扫码，等待手机确认',
  checking: '正在核验登录状态', logged_in: '抖音会话已验证', expired: '抖音会话已过期',
  verification_required: '需要本人完成官方验证', failed: '抖音连接未完成', cancelled: '本次登录已取消',
};
type SessionStatus = keyof typeof statusLabels;
type Session = {status: SessionStatus; session_saved: boolean; browser_open: boolean; qr_available: boolean; qr_updated_at?: string | number; verified_at?: string; expires_at?: string; error?: {code?: string; message: string}; verification_available: boolean; verification_frame_id?: string; verification_width?: number; verification_height?: number; verification_error?: {code?: string; message: string}};
type Action = 'login' | 'check' | 'cancel' | 'clear' | 'refresh';
type Operation = Action | VerificationAction;

function safeSession(value: unknown): Session {
  if (!value || typeof value !== 'object' || !('status' in value) || typeof value.status !== 'string' || !Object.hasOwn(statusLabels, value.status)) throw new Error('抖音连接状态响应无效，请稍后刷新。');
  const data = value as Record<string, unknown>;
  const error = data.error && typeof data.error === 'object' ? data.error as Record<string, unknown> : null;
  const verificationError = data.verification_error && typeof data.verification_error === 'object' ? data.verification_error as Record<string, unknown> : null;
  return {status: data.status as SessionStatus, session_saved: data.session_saved === true, browser_open: data.browser_open === true,
    qr_available: data.qr_available === true,
    verification_available: data.verification_available === true,
    verification_frame_id: typeof data.verification_frame_id === 'string' && data.verification_frame_id ? data.verification_frame_id : undefined,
    verification_width: typeof data.verification_width === 'number' && Number.isInteger(data.verification_width) && data.verification_width > 0 && data.verification_width <= 16384 ? data.verification_width : undefined,
    verification_height: typeof data.verification_height === 'number' && Number.isInteger(data.verification_height) && data.verification_height > 0 && data.verification_height <= 16384 ? data.verification_height : undefined,
    verification_error: verificationError && typeof verificationError.message === 'string' ? {message: verificationError.message, code: typeof verificationError.code === 'string' ? verificationError.code : undefined} : undefined,
    qr_updated_at: typeof data.qr_updated_at === 'string' || typeof data.qr_updated_at === 'number' && Number.isFinite(data.qr_updated_at) ? data.qr_updated_at : undefined,
    verified_at: typeof data.verified_at === 'string' ? data.verified_at : undefined,
    expires_at: typeof data.expires_at === 'string' ? data.expires_at : undefined,
    error: error && typeof error.message === 'string' ? {message: error.message, code: typeof error.code === 'string' ? error.code : undefined} : undefined};
}
function dateLabel(value?: string): string {
  if (!value || !Number.isFinite(Date.parse(value))) return '未提供';
  return new Date(value).toLocaleString('zh-CN', {hour12: false});
}

export default function DouyinSession({focusKey = 0}: {focusKey?: number}) {
  const [session, setSession] = useState<Session | null>(null);
  const [reading, setReading] = useState(true), [busy, setBusy] = useState<Action | 'verification' | ''>('');
  const [connectionError, setConnectionError] = useState(''), [operationError, setOperationError] = useState('');
  const [failures, setFailures] = useState(0), [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [failedQr, setFailedQr] = useState('');
  const [hiddenQrVersion, setHiddenQrVersion] = useState('');
  const card = useRef<HTMLElement>(null), invoke = useRef<((action: Operation) => Promise<void>) | null>(null);

  useEffect(() => {
    if (focusKey > 0) {card.current?.scrollIntoView({block: 'start'}); card.current?.focus({preventScroll: true});}
  }, [focusKey]);

  useEffect(() => {
    let disposed = false, operationPending = false;
    let timer: ReturnType<typeof setTimeout> | undefined, currentRequest: Promise<void> | null = null;
    let controller: AbortController | null = null, cancelRead: (() => void) | null = null;
    const schedule = () => {if (timer) clearTimeout(timer); if (!disposed) timer = setTimeout(poll, 2500);};
    const request = (action?: Exclude<Action, 'refresh'> | VerificationAction): Promise<void> => {
      const verification = typeof action === 'object';
      const abort = new AbortController(); controller = abort;
      let timedOut = false, interrupted = false;
      cancelRead = action ? null : () => {interrupted = true; abort.abort();};
      const timeout = setTimeout(() => {timedOut = true; abort.abort();}, action ? 30000 : 10000);
      setReading(!action);
      currentRequest = (async () => {
        try {
          const value = await api(verification ? '/douyin/session/verification/action' : '/douyin/session' + (action ? '/' + action : ''), verification ? action : action ? {} : undefined, undefined, {signal: abort.signal});
          const result = safeSession(value);
          if (disposed) return;
          setSession(result); setUpdatedAt(Date.now()); setConnectionError(''); setFailures(0);
        } catch (error: any) {
          if (disposed || interrupted) return;
          const message = verification ? '官网操作结果尚未确认，请等待新画面或检查连接。不会重发点击、输入或按键。' : timedOut ? action ? '请求超时，操作结果尚未确认。请刷新状态，不要重复打开登录窗口。' : '读取抖音连接状态超时。'
            : error.name === 'TypeError' ? '无法连接本地服务，请检查工作台是否仍在运行。' : error.message || '无法读取抖音连接状态。';
          if (action) setOperationError(message);
          else {setConnectionError(message); setFailures(value => value + 1);}
        } finally {
          clearTimeout(timeout); currentRequest = null; controller = null; cancelRead = null;
          if (!disposed) setReading(false);
        }
      })();
      return currentRequest;
    };
    async function poll() {
      if (disposed || operationPending) return;
      await request();
      if (!disposed && !operationPending) schedule();
    }
    const operate = async (action: Operation) => {
      if (disposed || operationPending) return;
      operationPending = true; if (timer) clearTimeout(timer);
      setBusy(typeof action === 'object' ? 'verification' : action); setOperationError('');
      try {
        // An explicit action takes over from a read; it never overlaps another
        // session request or silently repeats a previous login operation.
        if (currentRequest) {cancelRead?.(); await currentRequest;}
        if (!disposed) await request(action === 'refresh' ? undefined : action);
      } finally {
        operationPending = false;
        if (!disposed) {setBusy(''); schedule();}
      }
    };
    invoke.current = operate; void poll();
    return () => {disposed = true; if (timer) clearTimeout(timer); controller?.abort(); if (invoke.current === operate) invoke.current = null;};
  }, []);

  const status = session?.status, verified = status === 'logged_in';
  const active = !!session && (session.browser_open || ['opening', 'waiting_scan', 'waiting_confirmation', 'checking'].includes(session.status));
  const qrUrl = '/api/douyin/session/qr?v=' + encodeURIComponent(String(session?.qr_updated_at ?? 'current'));
  const showQr = active && !!session?.qr_available && hiddenQrVersion !== qrUrl && ['opening', 'waiting_scan', 'checking'].includes(session.status);
  const canRestart = (active || status === 'verification_required') && status !== 'waiting_confirmation' && !(status === 'checking' && !session?.browser_open);
  const perform = (action: Action) => {if (action === 'login') {setFailedQr(''); if (session?.qr_available) setHiddenQrVersion(qrUrl);} void invoke.current?.(action);};
  return <section id="douyin-connection" ref={card} tabIndex={-1} className="panel douyin-session" aria-label="抖音登录与连接">
    <div className="douyin-session-heading"><div><span className="douyin-session-eyebrow">项目专属会话</span><h3>抖音登录与连接</h3><p>默认匿名读取。连接后使用项目专属会话，不读取其他浏览器登录。</p></div>
      <span className={'badge ' + (verified ? 'verified' : status === 'failed' ? 'failed' : active ? 'running' : '')}>{session ? statusLabels[session.status] : '状态尚未确认'}</span></div>
    <div className="douyin-session-state" aria-live="polite">
      {active && status !== 'verification_required' && status !== 'waiting_confirmation' && <p className="douyin-session-instruction">{status === 'checking' && !session?.browser_open ? '正在核验本项目已保存的抖音会话，请等待检查结果。' : showQr ? '用手机抖音扫描下方官方二维码并确认。确认后可检查登录状态。' : '正在等待抖音官网生成二维码；出现后可直接在此扫码。'}</p>}
      {status === 'waiting_confirmation' && <p className="douyin-session-instruction">请在手机抖音确认登录，工作台会自动核验，无需再次扫码。“检查登录状态”只检查当前状态，不会刷新登录挑战。</p>}
      {status === 'verification_required' && <><p className="douyin-session-instruction">需要额外验证，请在下方官方视图由本人操作。临时内容只转送当前官网输入框，不保存；请勿把验证内容发到聊天。</p><DouyinVerification available={session?.verification_available === true} frameId={session?.verification_frame_id} width={session?.verification_width} height={session?.verification_height} error={session?.verification_error} busy={!!busy} connected={!connectionError} onAction={action => {void invoke.current?.(action);}}/></>}
      {showQr && <div className="douyin-session-qr">{failedQr !== qrUrl ? <img src={qrUrl} alt="抖音官方登录二维码" referrerPolicy="no-referrer" draggable={false} onError={() => setFailedQr(qrUrl)}/> : <div className="douyin-session-qr-error"><p role="alert">二维码暂时无法显示，请重新加载或刷新登录状态；这不代表登录成功。</p><button type="button" className="button" onClick={() => setFailedQr('')}>重新加载二维码</button></div>}<small>来自抖音官网的当前登录二维码，仅在本机显示。</small></div>}
      {verified && <p className="douyin-session-verified">会话已通过登录核验。原片获取和分析仍需由任务实际完成。</p>}
      {status === 'expired' && <p>当前会话已过期，{session?.session_saved ? '可先检查已保存会话；若仍过期，请重新扫码登录。' : '请重新扫码登录。'}连接后再返回任务手动重试。</p>}
      {status === 'cancelled' && <p>本次登录已取消；再次连接需要你手动开始。</p>}
      {status === 'failed' && <p>本次连接未完成。请查看原因，处理网络或窗口问题后再手动连接。</p>}
      {session?.error && <p className="douyin-session-error">{session.error.message}</p>}
    </div>
    {session && <dl className="douyin-session-facts"><div><dt>本机会话</dt><dd>{session.session_saved ? '已保存，仅供本项目使用' : '未保存'}</dd></div>
      {(verified || session.verified_at) && <div><dt>最近验证</dt><dd>{dateLabel(session.verified_at)}</dd></div>}
      {session.expires_at && <div><dt>服务报告到期</dt><dd>{dateLabel(session.expires_at)}</dd></div>}
      <div><dt>官方会话</dt><dd>{session.browser_open ? '运行中' : '已结束'}</dd></div></dl>}
    <div className="douyin-session-actions">
      {!active && !canRestart && <button type="button" className="button primary" disabled={!!busy || !session} onClick={() => perform('login')}>{busy === 'login' ? '正在打开…' : verified ? '重新扫码登录' : '扫码登录抖音'}</button>}
      {!active && session?.session_saved && <button type="button" className="button" disabled={!!busy} onClick={() => perform('check')}>{busy === 'check' ? '正在检查…' : '检查已保存会话'}</button>}
      {active && <><button type="button" className="button primary" disabled={!!busy} onClick={() => perform('check')}>{busy === 'check' ? '正在检查…' : '检查登录状态'}</button><button type="button" className="button" disabled={!!busy} onClick={() => perform('cancel')}>{busy === 'cancel' ? '正在取消…' : '取消本次登录'}</button></>}
      {canRestart && <button type="button" className="button" disabled={!!busy} onClick={() => perform('login')}>{busy === 'login' ? '正在重新获取…' : status === 'verification_required' ? '放弃验证并重新扫码' : '重新获取二维码'}</button>}
      <button type="button" className="button" disabled={!!busy} onClick={() => perform('refresh')}>{busy === 'refresh' ? '正在刷新…' : '刷新登录状态'}</button>
      {(session?.session_saved || verified) && <button type="button" className="text-button douyin-session-clear" disabled={!!busy} onClick={() => perform('clear')}>{busy === 'clear' ? '正在退出并清除…' : '退出并清除本机会话'}</button>}
    </div>
    {operationError && <p className="douyin-session-error" role="alert">上次操作结果未确认：{operationError} 请以最近状态更新为准，工作台不会自动重复登录操作。</p>}
    {connectionError && <p className="douyin-session-error" role="alert">状态连接有问题（连续 {failures} 次）：{connectionError} {session ? '保留上次状态，正在自动重连；上次验证不代表当前仍在线。' : '尚未取得登录状态，正在自动重连。'}</p>}
    <div className="douyin-session-footer"><small>{updatedAt ? '最近状态更新：' + new Date(updatedAt).toLocaleTimeString('zh-CN', {hour12: false}) : reading ? '正在读取本机登录状态…' : '登录状态尚未读取。'}</small>
      <p>二维码与官方验证视图来自同一项目专属抖音官网会话，仅在本机操作。登录不保证所有视频可读取；私密内容或平台限制仍可能阻止获取。登录后不会自动重试任务或调用模型。</p><p>退出并清除会话时，正在采集的抖音任务会停止该次采集；已保存内容保留。</p></div>
  </section>;
}
