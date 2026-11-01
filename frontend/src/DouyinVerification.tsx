import {useEffect, useRef, useState} from 'react';
import type {PointerEvent} from 'react';
import './DouyinVerification.css';

export type VerificationAction = {frame_id: string} & (
  {kind: 'click'; x: number; y: number} | {kind: 'text'; text: string} |
  {kind: 'key'; key: 'Enter' | 'Tab' | 'Backspace'} | {kind: 'refresh'}
);

export default function DouyinVerification({purpose = 'verification', available, frameId, width, height, error, busy, connected, onAction}: {
  purpose?: 'verification' | 'login_prompt';
  available: boolean; frameId?: string; width?: number; height?: number; error?: {message: string};
  busy: boolean; connected: boolean; onAction: (action: VerificationAction) => void;
}) {
  const [text, setText] = useState(''), [loadedFrame, setLoadedFrame] = useState(''), [failedFrame, setFailedFrame] = useState('');
  const [lockedFrame, setLockedFrame] = useState('');
  const [zoom, setZoom] = useState(0);
  const lock = useRef(''), image = useRef<HTMLImageElement>(null);
  const pointerFrame = useRef<{id: string; pointerId: number} | null>(null);
  const valid = available && !!frameId && Number.isInteger(width) && Number.isInteger(height) && width! > 0 && height! > 0;
  const consumed = !!frameId && lockedFrame === frameId;
  const mayRefresh = valid && !busy && connected && !consumed;
  const mayInteract = mayRefresh && loadedFrame === frameId && failedFrame !== frameId;
  const loginPrompt = purpose === 'login_prompt';
  useEffect(() => {setText('');}, [available, connected, busy]);

  function send(payload: VerificationAction) {
    if (!frameId || lock.current === frameId || busy || !connected || !valid) return;
    lock.current = frameId; setLockedFrame(frameId); setText('');
    onAction(payload);
  }
  function clickFrame(event: PointerEvent<HTMLButtonElement>) {
    const down = pointerFrame.current; pointerFrame.current = null;
    if (!mayInteract || !frameId || !width || !height || !event.isPrimary || event.button !== 0 || down?.id !== frameId || down.pointerId !== event.pointerId) return;
    const box = image.current?.getBoundingClientRect();
    if (!box || box.width <= 0 || box.height <= 0 || event.clientX < box.left || event.clientX >= box.right || event.clientY < box.top || event.clientY >= box.bottom) return;
    const x = Math.min(width - 1, Math.floor((event.clientX - box.left) * width / box.width));
    const y = Math.min(height - 1, Math.floor((event.clientY - box.top) * height / box.height));
    send({frame_id: frameId, kind: 'click', x, y});
  }
  function sendText() {
    if (!mayInteract || !frameId || text.length === 0 || text.length > 128) return;
    const value = text; setText(''); send({frame_id: frameId, kind: 'text', text: value});
  }
  return <section className="douyin-verification" aria-label={loginPrompt ? '抖音官网登录提示视图' : '抖音官方验证视图'}>
    <div className="douyin-verification-heading"><h4>{loginPrompt ? '官网登录提示' : '官方验证视图'}</h4><span>同一官网会话 · 由本人操作</span></div>
    <p className="douyin-verification-boundary">{loginPrompt ? '这是同一项目专属抖音官网的当前提示，不代表登录成功，也不表示一定需要身份验证。请先查看官网内容，再由本人决定是否操作。' : '这是项目专属抖音官网的当前画面，不是工作台复制的短信表单。请由你本人点击官网控件；工作台不会自动发送短信、完成题目或填写验证码。'}</p>
    <div className="douyin-verification-zoom" aria-label="官方画面缩放">{([{value: 0, label: '适应宽度'}, {value: 1, label: '100%'}, {value: 1.5, label: '150%'}]).map(option => <button type="button" className="button" key={option.value} aria-pressed={zoom === option.value} onClick={() => setZoom(option.value)}>{option.label}</button>)}</div>
    {valid && failedFrame !== frameId ? <div className="douyin-verification-viewport"><div className="douyin-verification-surface" style={{width: zoom ? width! * zoom : '100%', maxWidth: zoom ? undefined : width}}>
      <button type="button" className="douyin-verification-frame" aria-label={loginPrompt ? '点击官网登录提示画面' : '点击官方验证画面'} disabled={!mayInteract}
        onPointerDown={event => {pointerFrame.current = mayInteract && frameId && event.isPrimary && event.button === 0 ? {id: frameId, pointerId: event.pointerId} : null;}}
        onPointerUp={clickFrame} onPointerCancel={() => {pointerFrame.current = null;}}>
        <img key={frameId} ref={image} src={'/api/douyin/session/verification?v=' + encodeURIComponent(frameId)} alt={loginPrompt ? '抖音官网当前登录提示画面' : '抖音官网当前验证画面'} draggable={false} referrerPolicy="no-referrer"
          onLoad={() => setLoadedFrame(frameId)} onError={() => {setFailedFrame(frameId); setText('');}}/>
      </button></div></div> : <div className="douyin-verification-placeholder" role={valid ? 'alert' : undefined}>{valid ? '官网画面暂时无法显示。可在当前帧仍有效时刷新官方画面；不会重复上一操作。' : loginPrompt ? '正在等待官网登录提示画面；尚未确认登录。若长时间未恢复，请检查工作台连接，或取消后重新登录。' : '正在等待官网验证画面。若长时间未恢复，请检查工作台连接，或取消后重新登录。'}</div>}
    {!connected && <p className="douyin-verification-warning" role="alert">状态连接有问题，当前画面可能已过时。连接恢复前暂停官网操作，请先刷新登录状态。</p>}
    {(consumed || busy || (valid && loadedFrame !== frameId && failedFrame !== frameId)) && <p className="douyin-verification-pending">本次操作或画面结果待确认。收到新的官方画面并加载完成前暂停交互，不会重复发送操作。</p>}
    {error && <p className="douyin-verification-warning" role="alert">{error.message}</p>}
    <div className="douyin-verification-input"><label htmlFor="douyin-official-transient">临时输入（不保存）</label>
      <p>先点击上方官网输入框。内容仅传当前官网输入框，不保存；发送后立即清空。请勿把验证内容发到聊天。</p>
      <div><input id="douyin-official-transient" type="password" autoComplete="off" autoCorrect="off" spellCheck={false} maxLength={128} value={text} disabled={!mayRefresh}
        data-lpignore="true" data-1p-ignore="true" onChange={event => setText(event.target.value)} onKeyDown={event => {if (event.key === 'Enter') event.preventDefault();}}/>
        <button type="button" className="button" disabled={!mayInteract || !text.length || text.length > 128} onClick={sendText}>输入到官网</button></div></div>
    <div className="douyin-verification-keys">{(['Tab', 'Enter', 'Backspace'] as const).map(key => <button type="button" className="button" key={key} disabled={!mayInteract} onClick={() => frameId && send({frame_id: frameId, kind: 'key', key})}>{key}</button>)}
      <button type="button" className="button" disabled={!mayRefresh} onClick={() => frameId && send({frame_id: frameId, kind: 'refresh'})}>刷新官方画面</button></div>
    <p className="douyin-verification-note">每次操作后等待新画面再继续。刷新官方画面只获取当前官网状态，不重发点击或输入；没有有效新帧时请等待或检查登录状态。</p>
  </section>;
}
