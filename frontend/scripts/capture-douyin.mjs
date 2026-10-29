// Product renderer: fresh anonymous Chromium, with every network request brokered.
// Never imports a user profile, evaluates hidden application state, or solves a challenge.
import fs from 'node:fs';
import readline from 'node:readline';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
let chromium;
let version;
try {
  ({ chromium } = await import('@playwright/test'));
  version = require('@playwright/test/package.json').version;
} catch {
  process.stdout.write(JSON.stringify({ ok: false, browser_installed: false,
    error: { code: 'browser_package_missing', message: '请重新运行项目安装脚本' } }) + '\n');
  process.exit(1);
}

if (process.argv.includes('--check-runtime')) {
  const installed = fs.existsSync(chromium.executablePath());
  process.stdout.write(JSON.stringify({ ok: installed, browser_installed: installed,
    playwright_version: version, executable_path: chromium.executablePath(),
    ...(!installed ? { error: { code: 'browser_missing', message: '请运行项目安装脚本安装 Chromium' } } : {}) }) + '\n');
  process.exit(installed ? 0 : 1);
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const pending = new Map();
let nextId = 0;
let startResolve;
let cancelled = false;
let budgetExceeded = false;
let activeRequests = 0;
let requestCount = 0;
const waitingRequests = [];
const start = new Promise(resolve => { startResolve = resolve; });
input.on('line', line => {
  try {
    const message = JSON.parse(line);
    if (message.type === 'start') startResolve(message);
    else if (message.type === 'cancel') cancelled = true;
    else if (message.type === 'reply' && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  } catch { cancelled = true; }
});
input.on('close', () => { cancelled = true; });
const send = message => process.stdout.write(JSON.stringify(message) + '\n');
function rpc(message) {
  const id = ++nextId;
  return new Promise(resolve => {
    pending.set(id, resolve);
    send({ type: 'fetch', id, ...message });
  });
}
async function acquireRequest(countRequest = true) {
  if ((countRequest && ++requestCount > 700) || waitingRequests.length >= 256) return false;
  if (activeRequests >= 16) await new Promise(resolve => waitingRequests.push(resolve));
  activeRequests += 1;
  return true;
}
function releaseRequest() {
  activeRequests -= 1;
  waitingRequests.shift()?.();
}
function videoId(url) {
  try {
    const parsed = new URL(url);
    return parsed.pathname.match(/\/(?:share\/)?video\/(\d+)(?:\/|$)/)?.[1]
      || parsed.searchParams.get('modal_id') || null;
  } catch { return null; }
}
function platformPage(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'https:' && !u.username && !u.password && (!u.port || u.port === '443')
      && ['douyin.com', 'iesdouyin.com'].some(domain => u.hostname === domain || u.hostname.endsWith('.' + domain));
  } catch { return false; }
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let browser;
let result = { type: 'result', ok: false, code: 'douyin_browser_material' };
try {
  const options = await start;
  const target = String(options.video_id);
  const deadline = Date.now() + options.timeout_ms;
  let ready = false;
  let identityChanged = false;
  let redirects = 0;
  browser = await chromium.launch({ headless: true, chromiumSandbox: true,
    proxy: { server: options.guard_proxy, bypass: '<-loopback>' },
    args: ['--disable-background-networking', '--disable-quic', '--disable-extensions',
      '--disable-component-update', '--disable-features=MediaRouter',
      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1'] });
  const context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false,
    viewport: { width: 1280, height: 800 } });
  // WebSocket traffic is never allowed to escape the HTTP broker.
  await context.routeWebSocket('**/*', socket => socket.close());
  const page = await context.newPage();
  const network = await context.newCDPSession(page);
  await network.send('Network.enable');
  const resourceBytes = new Map();
  let decodedBytes = 0;
  network.on('Network.responseReceived', event => {
    const media = event.type === 'Media' || /^(?:video|audio)\//.test(event.response.mimeType || '');
    resourceBytes.set(event.requestId, { size: 0, limit: media ? 100 * 1024 * 1024 : 16 * 1024 * 1024 });
  });
  network.on('Network.dataReceived', event => {
    decodedBytes += event.dataLength;
    const entry = resourceBytes.get(event.requestId);
    if (entry) entry.size += event.dataLength;
    if (decodedBytes > 500 * 1024 * 1024 || (entry && entry.size > entry.limit)) {
      budgetExceeded = true;
      page.close().catch(() => {});
    }
  });
  network.on('Network.loadingFinished', event => { resourceBytes.delete(event.requestId); });
  network.on('Network.loadingFailed', event => { resourceBytes.delete(event.requestId); });
  let pageErrors = 0;
  let failedRequests = 0;
  const failedReasons = {};
  let lastProgress = 0;
  const responseStatuses = {};
  const responseTypes = {};
  let metadataBodies = 0;
  let metadataBodyErrors = 0;
  let observedMedia = 0;
  page.on('pageerror', () => { pageErrors += 1; });
  page.on('requestfailed', request => {
    failedRequests += 1;
    const code = request.failure()?.errorText?.match(/net::[A-Z_]+/)?.[0] || 'request_failed';
    failedReasons[code] = (failedReasons[code] || 0) + 1;
  });
  context.on('page', popup => { if (popup !== page) popup.close().catch(() => {}); });
  if (options.native_proxy) {
    page.on('response', async response => {
      let acquired = false;
      try {
        responseStatuses[response.status()] = (responseStatuses[response.status()] || 0) + 1;
        const resourceType = response.request().resourceType();
        responseTypes[resourceType] = (responseTypes[resourceType] || 0) + 1;
        acquired = await acquireRequest(false);
        if (!acquired || cancelled || Date.now() > deadline) return;
        const headers = await response.allHeaders();
        const type = (headers['content-type'] || '').split(';')[0].toLowerCase();
        const request = response.request();
        const host = new URL(response.url()).hostname;
        const official = ['douyin.com', 'iesdouyin.com'].some(domain => host === domain || host.endsWith('.' + domain));
        let reply;
        if (response.status() >= 300 && response.status() < 400 && headers.location) {
          reply = await rpc({ type: 'observed_redirect', url: response.url(), location: headers.location });
        } else if (official && (type === 'text/html' || type.endsWith('json'))) {
          if (Number(headers['content-length'] || 0) > 16 * 1024 * 1024) return;
          const body = await response.body();
          if (body.length > 16 * 1024 * 1024) return;
          const cache = path.join(options.work_dir, 'metadata-' + crypto.randomUUID() + '.bin');
          fs.writeFileSync(cache, body, { flag: 'wx', mode: 0o600 });
          metadataBodies += 1;
          reply = await rpc({ type: 'observed_metadata', url: response.url(), content_type: type, path: cache });
        } else if (response.status() >= 200 && response.status() < 300 &&
          (type.startsWith('video/') || type.startsWith('audio/') || type === 'application/octet-stream'
            || request.resourceType() === 'media')) {
          observedMedia += 1;
          reply = await rpc({ type: 'observed_media', url: response.url(), headers: await request.allHeaders() });
        }
        if (reply?.capture_ready) ready = true;
      } catch { metadataBodyErrors += 1; /* Body unavailable is not acquisition. */ }
      finally { if (acquired) releaseRequest(); }
    });
  }
  await context.route('**/*', async route => {
    const request = route.request();
    let acquired = false;
    try {
      acquired = await acquireRequest();
      if (!acquired) return await route.abort('blockedbyclient');
      if (cancelled || Date.now() > deadline) return await route.abort('timedout');
      if (!/^https?:\/\//i.test(request.url())) return await route.abort('blockedbyclient');
      const requestedId = videoId(request.url());
      const mainNavigation = request.isNavigationRequest() && request.frame() === page.mainFrame();
      if (mainNavigation && (!platformPage(request.url()) || (requestedId && requestedId !== target))) {
        identityChanged = true;
        return await route.abort('blockedbyclient');
      }
      if (options.native_proxy) {
        // All native egress is forced through our CONNECT proxy, which pins every
        // public destination. Chromium retains normal origin TLS verification.
        if (!request.url().startsWith('https://')) return await route.abort('blockedbyclient');
        return await route.continue();
      }
      const body = request.postDataBuffer();
      if (body && body.length > 1024 * 1024) return await route.abort('blockedbyclient');
      const reply = await rpc({ url: request.url(), method: request.method(),
        headers: await request.allHeaders(), body: body?.toString('base64') || '',
        resource_type: request.resourceType(), navigation: request.isNavigationRequest() });
      if (reply.abort || cancelled) return await route.abort('blockedbyclient');
      await route.fulfill({ status: reply.status, headers: reply.headers,
        ...(reply.path ? { path: reply.path } : { body: Buffer.from(reply.body || '', 'base64') }) });
      if (reply.redirect) {
        if (++redirects > 5 || !/^https?:\/\//i.test(reply.redirect)) { identityChanged = true; return; }
        // Native redirect follow-up is not reliably re-routed by Chromium. A new
        // driver navigation creates a fresh intercepted request for every hop.
        setTimeout(() => request.frame().goto(reply.redirect, { waitUntil: 'domcontentloaded',
          timeout: Math.max(1000, Math.min(45000, deadline - Date.now())) }).catch(() => {}), 0);
      }
      if (reply.capture_ready) ready = true;
    } catch { await route.abort('failed').catch(() => {}); }
    finally { if (acquired) releaseRequest(); }
  });
  try { await page.goto(options.url, { waitUntil: 'domcontentloaded', timeout: Math.min(45000, options.timeout_ms) }); }
  catch { /* A slow optional asset is not proof of a media failure or success. */ }
  let observations = [];
  while (!cancelled && !identityChanged && !budgetExceeded && Date.now() < deadline) {
    const current = videoId(page.url());
    if (Date.now() - lastProgress > 1000) {
      send({ type: 'progress', observed_video_id: current, page_errors: pageErrors,
        failed_requests: failedRequests, durations: observations.map(item => item.duration),
        response_status_counts: responseStatuses, response_types: responseTypes, failed_reasons: failedReasons,
        metadata_bodies: metadataBodies, metadata_body_errors: metadataBodyErrors, observed_media: observedMedia });
      lastProgress = Date.now();
    }
    if (current && current !== target) { identityChanged = true; break; }
    if (current === target) {
      // Ordinary media controls only; no hidden application state or injected challenge solver.
      observations = await page.locator('video').evaluateAll(videos => videos.map(video => {
        video.loop = true;
        video.muted = true;
        video.preload = 'auto';
        video.play().catch(() => {});
        return { src: video.currentSrc || video.src, duration: Number.isFinite(video.duration) ? video.duration : null,
          width: video.videoWidth, height: video.videoHeight, ready_state: video.readyState };
      })).catch(() => []);
      if (observations.length) {
        const reply = await rpc({ type: 'observe', video_id: current, observations });
        if (reply.capture_ready) ready = true;
      }
      if (ready) break;
    }
    await sleep(250);
  }
  result = { type: 'result', ok: ready && !cancelled && !identityChanged && !budgetExceeded, code: budgetExceeded
    ? 'douyin_browser_budget' : identityChanged
    ? 'douyin_browser_identity' : cancelled ? 'cancelled' : 'douyin_browser_material',
    page_url: page.url(), observed_video_id: videoId(page.url()), title: await page.title().catch(() => ''),
    observations, renderer_version: version };
} catch {
  result = { type: 'result', ok: false, code: 'douyin_browser_runtime' };
} finally {
  if (browser) await Promise.race([browser.close().catch(() => {}), sleep(5000)]);
  for (const resolve of pending.values()) resolve({ abort: true });
  pending.clear();
  input.close();
  await new Promise(resolve => process.stdout.write(JSON.stringify(result) + '\n', resolve));
  process.exit(0);
}
