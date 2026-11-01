// Dedicated official-site window. State travels only over private JSONL IPC.
import readline from 'node:readline';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const STORE_KEYS = new Set(['passport_csrf_token', 'passport_csrf_token_default', 's_v_web_id', 'ttwid']);
const DOMAIN = /^\.?(?:[a-z0-9-]+\.)*(?:douyin\.com|iesdouyin\.com)$/i;
const AUTH_COOKIES = new Set(['sessionid', 'sessionid_ss', 'sid_tt']);
const QR_PANEL = '#douyin_login_comp_scan_code';
const QR_IMAGE = '#douyin_login_comp_scan_code #animate_qrcode_container img[aria-label="二维码"]';
export function officialPage(value) {
  try { const u = new URL(value); return u.protocol === 'https:' && !u.username && !u.password && (!u.port || u.port === '443') && DOMAIN.test(u.hostname); }
  catch { return false; }
}
function positiveId(value) { return /^(?!0+$)\d{1,32}$/.test(String(value ?? '')); }
function authEndpoint(url) {
  return officialPage(url) && ['/aweme/v1/web/user/profile/self/', '/passport/web/user/info/'].includes(new URL(url).pathname.replace(/\/?$/, '/'));
}
export function officialAuthProof(url, status, value) {
  if (status !== 200 || !officialPage(url) || !value || typeof value !== 'object' || Array.isArray(value)) return false;
  const route = new URL(url).pathname.replace(/\/?$/, '/');
  if (route === '/aweme/v1/web/user/profile/self/') {
    const user = value.user;
    return Boolean(value.status_code === 0 && user && typeof user === 'object' && (positiveId(user.uid) || positiveId(user.user_id)) && user.is_login !== false);
  }
  if (route === '/passport/web/user/info/') {
    const user = value.data;
    return Boolean((value.message === 'success' || value.status_code === 0) && user && typeof user === 'object' && user.is_login !== false &&
      (positiveId(user.user_id) || positiveId(user.uid) || positiveId(user.user_id_str)));
  }
  return false;
}
export function hasAuthCookie(cookies, now = Date.now() / 1000) {
  return Array.isArray(cookies) && cookies.some(c => c && DOMAIN.test(c.domain || '') && AUTH_COOKIES.has(c.name) &&
    typeof c.value === 'string' && c.value.length > 0 && (c.expires === -1 || (typeof c.expires === 'number' && c.expires > now)));
}
export function officialAuthDenial(url, status, value) {
  if (!authEndpoint(url) || !value || typeof value !== 'object' || Array.isArray(value)) return false;
  if (status === 401) return true;
  const user = value.user || value.data;
  return Boolean(status === 200 && (value.is_login === false || (user && typeof user === 'object' && user.is_login === false)));
}
export function minimalStorageState(value) {
  if (!value || typeof value !== 'object' || !Array.isArray(value.cookies) || !Array.isArray(value.origins)) throw new Error('protocol');
  const cookies = value.cookies.filter(c => c && DOMAIN.test(c.domain || '')).map(c => {
    const selected = {};
    for (const key of ['name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite']) if (key in c) selected[key] = c[key];
    return selected;
  });
  const origins = value.origins.filter(o => officialPage(o?.origin) && new URL(o.origin).pathname === '/').map(o => ({
    origin: new URL(o.origin).origin,
    localStorage: (Array.isArray(o.localStorage) ? o.localStorage : []).filter(item => STORE_KEYS.has(item?.name) && typeof item.value === 'string').map(item => ({ name: item.name, value: item.value })),
  })).filter(o => o.localStorage.length);
  const state = { cookies, origins };
  if (Buffer.byteLength(JSON.stringify(state)) > 256 * 1024) throw new Error('budget');
  return state;
}
function proxyAllowed(value) {
  try { const u = new URL(value); return u.protocol === 'http:' && ['127.0.0.1', '[::1]'].includes(u.hostname) && Number(u.port) > 0 && Number(u.port) <= 65535 && !u.username && !u.password && u.pathname === '/' && !u.search && !u.hash; }
  catch { return false; }
}
export async function visibleVerification(page) {
  if (!officialPage(page.url())) return false;
  return page.getByText(/身份验证|为保障账号安全，请先完成身份验证|请完成(?:安全)?验证|拖动滑块|按住滑块|完成拼图/, { exact: false }).first().isVisible().catch(() => false);
}
// Hold the actual modal element across its SMS substeps. Text is an entry
// signal, never an exit signal, and no input values are inspected.
async function verificationRoot(page) {
  const title = page.getByText(/身份验证|为保障账号安全，请先完成身份验证|请完成(?:安全)?验证|拖动滑块|按住滑块|完成拼图/, { exact: false }).first();
  if (!await title.isVisible().catch(() => false)) return null;
  let source, handle;
  try {
    source = await title.elementHandle({ timeout: 1500 });
    handle = await source.evaluateHandle(element => {
      const semantic = element.closest('[role="dialog"], [aria-modal="true"]');
      if (semantic && !['BODY', 'HTML'].includes(semantic.tagName)) return semantic;
      for (let node = element.parentElement; node && !['BODY', 'HTML'].includes(node.tagName); node = node.parentElement) {
        const style = getComputedStyle(node), box = node.getBoundingClientRect();
        if (style.position === 'fixed' && box.width >= 160 && box.height >= 100) return node;
      }
      return null;
    });
    const root = handle.asElement();
    if (root) return root;
  } catch { /* An unidentifiable root remains latched and cannot prove exit. */ }
  finally { await source?.dispose().catch(() => {}); }
  await handle?.dispose().catch(() => {});
  return null;
}
async function verificationBlocker(page, minimumLayer = 1, background = null) {
  if (await visibleVerification(page)) return true;
  // These are specific MFA words observed on the real SMS step, used only
  // after MFA was entered. Ordinary "验证码登录" is deliberately excluded.
  if (await page.getByText(/短信验证|接收短信验证码|发送短信验证/, { exact: false }).first().isVisible().catch(() => false)) return true;
  try {
    return await page.evaluate(({ minimum, backgroundNodes }) => {
      const nodes = document.querySelectorAll('[role="dialog"], [aria-modal="true"], body *');
      if (nodes.length > 20000) return true;
      for (const node of nodes) {
        const style = getComputedStyle(node), box = node.getBoundingClientRect();
        if (box.width < 160 || box.height < 100 || style.visibility === 'hidden' || style.display === 'none') continue;
        const semantic = node.matches('[role="dialog"], [aria-modal="true"]');
        if (!semantic && style.position !== 'fixed') continue;
        if (!semantic) {
          let layer = 0;
          for (let parent = node; parent && parent.nodeType === 1; parent = parent.parentElement) {
            const z = Number.parseInt(getComputedStyle(parent).zIndex, 10);
            if (Number.isFinite(z)) layer = Math.max(layer, z);
          }
          if (layer < minimum) continue;
        }
        // Only the exact pre-existing background elements may be ignored.
        // A replacement dialog that happens to contain the QR stays blocking.
        if (backgroundNodes?.includes(node)) continue;
        if (['BODY', 'HTML'].includes(node.tagName)) continue;
        return true;
      }
      return false;
    }, { minimum: minimumLayer, backgroundNodes: background });
  } catch { return true; }
}
async function probeChallenge(page) {
  // about:blank before a real navigation commits is waiting, not evidence of
  // a new challenge. A final non-official page still cannot prove login.
  if (!officialPage(page.url())) return null;
  try {
    if (await page.getByText(/身份验证|为保障账号安全，请先完成身份验证|请完成(?:安全)?验证|拖动滑块|按住滑块|完成拼图/, { exact: false }).first().isVisible()) return true;
    return await page.getByText(/短信验证|接收短信验证码|发送短信验证/, { exact: false }).first().isVisible();
  } catch { return null; } // Unknown DOM is neither a challenge nor permission to save.
}
export function verificationTracker(page, { clock = Date.now, locate = verificationRoot, blocked = verificationBlocker } = {}) {
  let latched = false, root = null, rootLayer = 1, background = null, absentSince = null, proofReady = false, epoch = 0, inspection = null;
  return {
    async inspect() {
      if (inspection) return inspection;
      inspection = (async () => {
      if (!officialPage(page.url())) return { latched, active: false, canProve: false, epoch };
      if (!latched && await visibleVerification(page)) {
        latched = true; epoch += 1;
        try {
          background = await page.evaluateHandle(() => {
            const ancestors = [];
            for (let node = document.querySelector('#douyin_login_comp_scan_code'); node && ancestors.length < 64 && !['BODY', 'HTML'].includes(node.tagName); node = node.parentElement) ancestors.push(node);
            return ancestors;
          });
        } catch { /* Missing background identity does not authorize an exit. */ }
      }
      if (!latched) return { latched: false, active: false, canProve: false, epoch };
      // Only bind the initially identified modal, never an arbitrary later
      // dialog or the background QR element.
      if (!root && await visibleVerification(page)) {
        root = await locate(page);
        if (root) {
          try {
            const layer = await root.evaluate(node => {
              let maximum = 0;
              for (let parent = node; parent && parent.nodeType === 1; parent = parent.parentElement) {
                const z = Number.parseInt(getComputedStyle(parent).zIndex, 10);
                if (Number.isFinite(z)) maximum = Math.max(maximum, z);
              }
              return maximum;
            });
            if (typeof layer === 'number' && Number.isFinite(layer)) rootLayer = Math.max(1, layer);
          } catch { /* Conservative default still treats raised fixed layers as blockers. */ }
        }
      }
      let active = false;
      try { active = Boolean(root && await root.isVisible() && await root.evaluate(node => {
        if (!node.isConnected) return false;
        for (let parent = node; parent && parent.nodeType === 1; parent = parent.parentElement) {
          if (getComputedStyle(parent).opacity === '0') return false;
        }
        return true;
      })); } catch { /* detached */ }
      if (active || !root || await blocked(page, rootLayer, background)) {
        absentSince = null;
        if (proofReady) { epoch += 1; proofReady = false; }
        return { latched: true, active, canProve: false, epoch };
      }
      absentSince ??= clock();
      if (!proofReady && clock() - absentSince >= 500) { proofReady = true; epoch += 1; }
      return { latched: true, active: false, canProve: proofReady, epoch };
      })();
      try { return await inspection; } finally { inspection = null; }
    },
    async visible() { return (await this.inspect()).active; },
  };
}
const VIEW_KEYS = new Set(['Backspace', 'Delete', 'Tab', 'Enter', 'Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'Space']);
export async function focusedEditable(page) {
  let frame = page.mainFrame();
  for (let depth = 0; frame && depth < 8; depth += 1) {
    try {
      const state = await frame.evaluate(() => {
        const element = document.activeElement;
        if (!element || element.disabled || element.readOnly) return 'none';
        if (element.tagName === 'TEXTAREA' || (element.tagName === 'INPUT' && ['text', 'tel', 'password', 'number'].includes(element.type))) return 'editable';
        return element.tagName === 'IFRAME' ? 'frame' : 'none';
      });
      if (state === 'editable') return true;
      if (state !== 'frame') return false;
      const handle = await frame.evaluateHandle(() => document.activeElement);
      try { frame = await handle.asElement()?.contentFrame(); } finally { await handle.dispose(); }
    } catch { return false; }
  }
  return false;
}
export function verificationView(send, { clock = Date.now, frameId = crypto.randomUUID, visible = visibleVerification } = {}) {
  let current = null, enabled = false, dirty = true, busy = false, lastRead = -Infinity, lastHash = '', sent = 0, actions = 0, budgetWarned = false;
  const error = code => send({ type: 'verification_error', code });
  const invalidate = () => { if (current || enabled) send({ type: 'verification_clear' }); current = null; };
  return {
    clear() { invalidate(); enabled = false; dirty = true; lastHash = ''; },
    force() { dirty = true; },
    async tick(page, active) {
      if (!active || !officialPage(page.url()) || !await visible(page)) { this.clear(); return; }
      enabled = true;
      if (busy || (!dirty && clock() - lastRead < 5000)) return;
      if (sent >= 48) { if (!budgetWarned) { budgetWarned = true; invalidate(); error('budget'); } return; }
      busy = true; const forced = dirty; dirty = false; lastRead = clock();
      try {
        const size = page.viewportSize();
        if (!size || !Number.isInteger(size.width) || !Number.isInteger(size.height) || size.width < 1 || size.height < 1 || size.width > 2048 || size.height > 2048) throw new Error('budget');
        const jpeg = await page.screenshot({ type: 'jpeg', quality: 80, fullPage: false, scale: 'css', timeout: 1500 });
        if (!officialPage(page.url()) || !await visible(page)) { this.clear(); return; }
        if (!Buffer.isBuffer(jpeg) || jpeg.length > 256 * 1024 || jpeg[0] !== 255 || jpeg[1] !== 216 || jpeg.at(-2) !== 255 || jpeg.at(-1) !== 217) throw new Error('budget');
        const hash = crypto.createHash('sha256').update(jpeg).digest('hex');
        if (!forced && hash === lastHash) return;
        lastHash = hash; sent += 1;
        current = { frame_id: frameId(), width: size.width, height: size.height };
        send({ type: 'verification_frame', image_base64: jpeg.toString('base64'), ...current });
      } catch (failure) { invalidate(); error(failure?.message === 'budget' ? 'budget' : 'action_failed'); }
      finally { busy = false; }
    },
    async act(page, message) {
      const frame = current;
      dirty = true;
      if (actions >= 128) { invalidate(); error('budget'); return false; }
      if (!frame || message.frame_id !== frame.frame_id) { error('stale_frame'); return false; }
      current = null; send({ type: 'verification_clear' }); actions += 1;
      if (!officialPage(page.url()) || !await visible(page)) { error('stale_frame'); return false; }
      try {
        const allowed = new Set(['type', 'frame_id', 'kind', 'x', 'y', 'text', 'key']);
        if (Object.keys(message).some(key => !allowed.has(key))) throw new Error('action');
        if (message.kind === 'click') {
          if (!Number.isInteger(message.x) || !Number.isInteger(message.y) || message.x < 0 || message.y < 0 || message.x >= frame.width || message.y >= frame.height) throw new Error('action');
          await page.mouse.click(message.x, message.y);
        } else if (message.kind === 'text') {
          if (typeof message.text !== 'string' || [...message.text].length > 128 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(message.text)) throw new Error('action');
          if (!await focusedEditable(page)) { error('input_focus'); return false; }
          await page.keyboard.insertText(message.text);
        } else if (message.kind === 'key') {
          if (!VIEW_KEYS.has(message.key)) throw new Error('action');
          await page.keyboard.press(message.key);
        } else if (message.kind !== 'refresh') throw new Error('action');
        return true; // refresh only re-captures; it never navigates or reloads.
      } catch { error('action_failed'); return false; }
    },
  };
}
async function currentQrImage(page) {
  const image = page.locator(QR_IMAGE);
  return await image.count() === 1 && await image.isVisible() ? image : null;
}
export function qrPng(value) {
  if (typeof value !== 'string' || !value.startsWith('data:image/png;base64,')) return null;
  const encoded = value.slice(22);
  if (!encoded || encoded.length > 87384 || encoded.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return null;
  const png = Buffer.from(encoded, 'base64');
  if (png.toString('base64') !== encoded || png.length < 45 || png.length > 65536 ||
    !png.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) ||
    png.readUInt32BE(8) !== 13 || png.toString('ascii', 12, 16) !== 'IHDR' ||
    png.readUInt32BE(16) < 1 || png.readUInt32BE(16) > 2048 || png.readUInt32BE(20) < 1 || png.readUInt32BE(20) > 2048 ||
    png.toString('ascii', png.length - 8, png.length - 4) !== 'IEND') return null;
  return png;
}
async function unobscuredQr(image) {
  return image.evaluate(element => {
    const container = element.closest('#animate_qrcode_container');
    if (!container) return false;
    const box = element.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return false;
    return [[.1,.1],[.9,.1],[.5,.5],[.1,.9],[.9,.9]].every(([x,y]) => {
      const hit = document.elementFromPoint(box.left + box.width*x, box.top + box.height*y);
      return hit === element || Boolean(hit && container.contains(hit));
    });
  }).catch(() => false);
}
export async function visibleScanConfirmation(page) {
  if (!officialPage(page.url())) return false;
  return page.locator(QR_PANEL).getByText(/扫码成功|请在手机(?:抖音)?(?:上)?确认/, { exact: false }).first().isVisible().catch(() => false);
}
export function qrRelay(send, { clock = Date.now } = {}) {
  const seen = new Set(); let lastCheck = -Infinity, sent = 0, refreshes = 0, reading = false, cleared = false;
  return {
    reset() { seen.clear(); cleared = false; },
    clear() { if (!cleared) { send({ type: 'qr_clear' }); cleared = true; } },
    async tick(page) {
      if (reading || clock() - lastCheck < 5000) return;
      lastCheck = clock(); reading = true;
      try {
        if (!officialPage(page.url())) { this.clear(); return; }
        const panel = page.locator(QR_PANEL);
        if (await panel.count() !== 1 || !await panel.isVisible()) { this.clear(); return; }
        if (await panel.getByText('二维码失效', { exact: true }).first().isVisible()) {
          if (!cleared) { send({ type: 'qr_clear' }); cleared = true; }
          seen.clear();
          const refresh = panel.getByText('点击刷新', { exact: true }).first();
          if (refreshes < 20 && await refresh.isVisible()) {
            refreshes += 1;
            await refresh.click({ timeout: 1500 });
          }
          return; // Never forward the expired element; inspect fresh UI next tick.
        }
        if (sent >= 20) return;
        const image = await currentQrImage(page);
        if (!image || !await unobscuredQr(image)) { this.clear(); return; }
        // Copy only the site's actual PNG bytes. Element screenshots can
        // capture an overlaid SMS input and must never feed the QR slot.
        const png = qrPng(await image.getAttribute('src', { timeout: 1500 }));
        if (!png || !officialPage(page.url()) || !await unobscuredQr(image)) { this.clear(); return; }
        const hash = crypto.createHash('sha256').update(png).digest('hex');
        if (seen.has(hash)) return;
        seen.add(hash); sent += 1; cleared = false;
        send({ type: 'qr', image_base64: png.toString('base64') });
      } catch { this.clear(); /* No raw page, image or exception is logged. */ }
      finally { reading = false; }
    },
  };
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function settleWithin(value, milliseconds) {
  let timer;
  try { await Promise.race([value, new Promise(resolve => { timer = setTimeout(resolve, milliseconds); })]); }
  finally { clearTimeout(timer); }
}

export async function main(runtime = {}) {
  let browser, cancelled = false, checking = false, startResolve, malformed = false;
  let options, proof = false, denied = false, verification = false, result, waitingConfirmation = false, proofNavigationUsed = false;
  let tracker, trackerState = { latched: false, active: false, canProve: false, epoch: 0 };
  let probePage = null, probeEpoch = -1, proofEpoch = -1, probeFinished = false, probeChallenged = false, probePostMfa = false, probeFailure = null;
  let responses = 0, bytes = 0, phase = '', navigation = null, qrReady = false, lastUiCheck = -Infinity;
  let authOrdinal = 0, lastAuthOrdinal = 0;
  const actions = [];
  const pending = new Set();
  const input = readline.createInterface({ input: runtime.input || process.stdin, crlfDelay: Infinity });
  const send = value => process.stdout.write(JSON.stringify(value) + '\n');
  const relay = qrRelay(send);
  const view = verificationView(send, { visible: async () => Boolean(tracker && await tracker.visible()) });
  const progress = value => { if (phase !== value) { phase = value; send({ type: 'progress', phase }); } };
  const start = new Promise(resolve => { startResolve = resolve; });
  input.on('line', line => {
    if (Buffer.byteLength(line) > 512 * 1024) { malformed = true; cancelled = true; startResolve(null); return; }
    try {
      const message = JSON.parse(line);
      if (message.type === 'start' && !options) { options = message; startResolve(message); }
      else if (message.type === 'cancel') { cancelled = true; startResolve(null); }
      else if (message.type === 'check') checking = true;
      else if (message.type === 'verification_action') {
        if (actions.length >= 16) { send({ type: 'verification_error', code: 'budget' }); view.force(); }
        else actions.push(message);
      }
    } catch { malformed = true; cancelled = true; startResolve(null); }
  });
  input.on('close', () => { cancelled = true; startResolve(null); });
  const failure = code => ({ type: 'result', ok: false, authenticated: false, error: { code } });
  try {
    options = await start;
    if (cancelled) { result = failure(malformed ? 'douyin_session_protocol' : 'douyin_session_cancelled'); return; }
    if (!options || !['login', 'check'].includes(options.mode) || !proxyAllowed(options.guard_proxy)) throw new Error('protocol');
    const state = options.storage_state ? minimalStorageState(options.storage_state) : null;
    const requested = Number(options.timeout_ms);
    const maximum = options.mode === 'login' ? 600000 : 45000;
    if (!Number.isFinite(requested) || requested < 1) throw new Error('protocol');
    const deadline = Date.now() + Math.min(requested, maximum);
    // Injected only by explicit offline module tests, never by JSONL input.
    const { chromium } = runtime.chromium ? runtime : await import('@playwright/test');
    browser = await chromium.launch({ headless: options.mode === 'check', chromiumSandbox: true,
      timeout: Math.min(30000, Math.max(1, deadline - Date.now())),
      proxy: { server: options.guard_proxy, bypass: '<-loopback>' },
      args: ['--disable-background-networking', '--disable-quic', '--disable-extensions', '--disable-component-update',
        '--disable-features=MediaRouter', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1'] });
    const context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false,
      viewport: { width: 1280, height: 800 },
      ...(state ? { storageState: state } : {}) });
    await context.routeWebSocket('**/*', socket => socket.close());
    await context.route('**/*', route => {
      const request = route.request();
      if (cancelled || Date.now() >= deadline || responses > 700 || bytes > 100 * 1024 * 1024 || !request.url().startsWith('https://')) return route.abort('blockedbyclient');
      if (request.isNavigationRequest() && !request.frame().parentFrame() && !officialPage(request.url())) return route.abort('blockedbyclient');
      return route.continue(); // Every outbound TCP connection remains pinned by the mandatory proxy.
    });
    const page = await context.newPage();
    tracker = verificationTracker(page);
    progress('opening');
    page.on('close', () => { cancelled = true; });
    const network = await context.newCDPSession(page);
    await network.send('Network.enable');
    network.on('Network.dataReceived', event => { bytes += event.dataLength; if (bytes > 100 * 1024 * 1024) cancelled = true; });
    context.on('response', response => {
      responses += 1;
      if (!authEndpoint(response.url())) return;
      const ordinal = ++authOrdinal;
      let owner = null;
      try { owner = response.request().frame().page(); } catch { /* No owning page cannot be post-MFA proof. */ }
      const fromProbe = owner === probePage && probePage !== null;
      const eventEpoch = fromProbe ? probeEpoch : trackerState.epoch;
      const postMfa = fromProbe ? probePostMfa : trackerState.latched;
      const task = (async () => {
        if (Date.now() >= deadline) return;
        const headers = await response.allHeaders();
        const type = (headers['content-type'] || '').split(';')[0].toLowerCase();
        if (!type.endsWith('json') || Number(headers['content-length'] || 0) > 1024 * 1024) return;
        const body = await response.body();
        if (body.length > 1024 * 1024) return;
        let payload; try { payload = JSON.parse(body.toString('utf8')); } catch { return; }
        const stateNow = await tracker.inspect();
        if (ordinal < lastAuthOrdinal || (stateNow.latched && (!postMfa || !fromProbe || !stateNow.canProve || stateNow.epoch !== eventEpoch))) return;
        if (fromProbe && await probeChallenge(probePage) === true) { probeChallenged = true; return; }
        if (officialAuthProof(response.url(), response.status(), payload)) { proof = true; denied = false; proofEpoch = eventEpoch; lastAuthOrdinal = ordinal; }
        else if (officialAuthDenial(response.url(), response.status(), payload)) { denied = true; proof = false; lastAuthOrdinal = ordinal; }
      })().catch(() => {});
      pending.add(task); task.finally(() => pending.delete(task));
    });
    const initialUrl = options.mode === 'login' ? 'https://www.douyin.com/' : 'https://www.douyin.com/user/self';
    const visit = () => page.goto(initialUrl, { waitUntil: 'domcontentloaded', timeout: Math.max(1, Math.min(30000, deadline - Date.now())) }).catch(() => {});
    navigation = visit().then(async () => {
      // One normal interaction with the official visible login control. The
      // check command below preserves this page and its current challenge.
      if (options.mode === 'login' && !proof && !cancelled && !waitingConfirmation && !verification && Date.now() < deadline) {
        try {
          const scanVisible = async () => {
            return Boolean(await currentQrImage(page));
          };
          const waitUntil = Math.min(deadline, Date.now() + (runtime.uiReadyWaitMs ?? 5000));
          do {
            if (proof || cancelled || waitingConfirmation || (await tracker.inspect()).latched || await scanVisible()) return;
            if (Date.now() >= waitUntil) break;
            await sleep(200);
          } while (Date.now() < waitUntil);
          if (proof || cancelled || waitingConfirmation || (await tracker.inspect()).latched || await scanVisible()) return;
          const button = page.getByRole('button', { name: '登录', exact: true });
          if (await button.isVisible()) await button.click({ timeout: Math.min(1500, Math.max(1, deadline - Date.now())) });
        } catch { /* The user can still use the actual official window. */ }
      }
    }).finally(() => { qrReady = true; });
    progress(options.mode === 'check' ? 'checking' : 'waiting_scan');
    while (!cancelled && Date.now() < deadline) {
      const previous = trackerState;
      trackerState = await tracker.inspect();
      verification = trackerState.latched;
      if (trackerState.epoch !== previous.epoch) {
        proof = false; denied = false; proofEpoch = -1; lastAuthOrdinal = ++authOrdinal;
        proofNavigationUsed = false; probeFinished = false;
        if (probePage) { const old = probePage; probePage = null; await old.close?.().catch(() => {}); }
      }
      if (checking) {
        checking = false; relay.reset();
        if (verification) { relay.clear(); view.force(); progress('verification_required'); }
        else if (waitingConfirmation) { relay.clear(); progress('waiting_confirmation'); }
        else progress(options.mode === 'check' ? 'checking' : 'waiting_scan');
      }
      if (verification) { relay.clear(); progress('verification_required'); }
      while (actions.length) await view.act(page, actions.shift());
      await view.tick(page, verification);
      const authCookie = hasAuthCookie(await context.cookies());
      const finalState = await tracker.inspect();
      verification = finalState.latched;
      if (verification) { relay.clear(); progress('verification_required'); }
      const probeObservation = probePage ? await probeChallenge(probePage) : false;
      if (probeObservation === true) probeChallenged = true;
      if (probePage && probeFinished && (probeFailure || !officialPage(probePage.url()))) {
        result = failure(probeFailure || 'douyin_session_runtime');
        break;
      }
      if (probeChallenged) {
        // This bounded proof-only page is not the user's input surface. Do
        // not leave an invisible new challenge waiting or claim success.
        result = failure('douyin_session_verification_required');
        break;
      }
      const freshProof = !finalState.latched || (finalState.canProve && proofEpoch === finalState.epoch && probeFinished && probeEpoch === finalState.epoch);
      if (proof && authCookie && freshProof && !probeChallenged && probeObservation === false && (!probePage || probeFinished)) {
        const storage = minimalStorageState(await context.storageState());
        const current = await tracker.inspect();
        if (hasAuthCookie(storage.cookies) && (!current.latched || (current.canProve && current.epoch === proofEpoch && current.epoch === probeEpoch)) &&
          (!probePage || await probeChallenge(probePage) === false)) {
          result = { type: 'result', ok: true, authenticated: true,
            verification_method: 'official_profile_response', storage_state: storage };
          break;
        }
      }
      // Never navigate the user's QR/SMS page. A separate page in the same
      // context may seek a fresh official proof only after the known original
      // verification root has ended with no visible replacement challenge.
      if (authCookie && !proof && qrReady && !proofNavigationUsed && (!finalState.latched || finalState.canProve)) {
        // The gate may have opened during view/final inspection. Adopt that
        // same epoch before its new page can emit any response.
        trackerState = finalState;
        proofNavigationUsed = true; probeEpoch = finalState.epoch; probePostMfa = finalState.latched;
        probeFinished = false; probeChallenged = false; probeFailure = null;
        const candidate = await context.newPage();
        if (candidate === page) throw new Error('protocol');
        probePage = candidate;
        const probeNetwork = await context.newCDPSession(candidate);
        await probeNetwork.send('Network.enable');
        probeNetwork.on('Network.dataReceived', event => { bytes += event.dataLength; if (bytes > 100 * 1024 * 1024) cancelled = true; });
        const currentProbe = candidate, currentEpoch = probeEpoch;
        navigation = candidate.goto('https://www.douyin.com/user/self', {
          waitUntil: 'domcontentloaded', timeout: Math.max(1, Math.min(30000, deadline - Date.now())),
        }).catch(error => {
          if (probePage === currentProbe && probeEpoch === currentEpoch) probeFailure = error?.name === 'TimeoutError' ? 'douyin_session_timeout' : 'douyin_session_runtime';
        }).finally(() => {
          if (probePage === currentProbe && probeEpoch === currentEpoch) probeFinished = true;
        });
      }
      if (Date.now() - lastUiCheck >= 1000) {
        lastUiCheck = Date.now();
        if (!verification && options.mode === 'login' && await visibleScanConfirmation(page)) { waitingConfirmation = true; relay.clear(); }
        if (verification) progress('verification_required');
        else if (waitingConfirmation) progress('waiting_confirmation');
      }
      if (options.mode === 'login' && qrReady && !waitingConfirmation && !verification) await relay.tick(page);
      if (responses > 700 || bytes > 100 * 1024 * 1024) { result = failure('douyin_session_budget'); break; }
      await sleep(200);
    }
    result ||= failure(responses > 700 || bytes > 100 * 1024 * 1024 ? 'douyin_session_budget' : cancelled ? malformed ? 'douyin_session_protocol' : 'douyin_session_cancelled' :
      verification && !trackerState.canProve ? 'douyin_session_verification_required' : options.mode === 'check' && denied ? 'douyin_session_not_authenticated' : 'douyin_session_timeout');
  } catch (error) {
    result = failure(error?.message === 'protocol' ? 'douyin_session_protocol' : error?.message === 'budget' ? 'douyin_session_budget' : 'douyin_session_runtime');
  } finally {
    view.clear();
    input.close();
    if (browser) await settleWithin(browser.close().catch(() => {}), 3000);
    await settleWithin(Promise.allSettled([...pending]), 100);
    process.stdout.write(JSON.stringify(result || failure('douyin_session_runtime')) + '\n');
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
  process.exit(0);
}
