import {test, expect} from '@playwright/test';
import type {Page, Route} from '@playwright/test';
import {mkdirSync} from 'node:fs';
import {resolve} from 'node:path';

const output = process.env.UI_ARTIFACT_DIR || '/tmp/skill-collect-ui-douyin-session-contract-v030';
type Item = Record<string, any>;
const signedOut = (): Item => ({status: 'signed_out', session_saved: false, browser_open: false, qr_available: false});
const loggedIn = (): Item => ({status: 'logged_in', session_saved: true, browser_open: false, qr_available: false, verified_at: '2026-11-01T00:00:00Z'});

async function markerPng(page: Page): Promise<Buffer> {
  // Deliberately not a QR pattern: this local PNG visibly marks the UI contract.
  const data = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 240; canvas.height = 240;
    const context = canvas.getContext('2d')!;
    context.fillStyle = '#f3f7ec'; context.fillRect(0, 0, 240, 240);
    context.fillStyle = '#536d42'; context.fillRect(35, 40, 170, 35);
    context.textAlign = 'center'; context.font = 'bold 22px sans-serif'; context.fillText('CONTRACT', 120, 125);
    context.font = '17px sans-serif'; context.fillText('NOT A QR CODE', 120, 165);
    return canvas.toDataURL('image/png');
  });
  return Buffer.from(data.split(',')[1], 'base64');
}

async function workspace(page: Page, session: (route: Route, action: string) => Promise<void>, tasks: Item[] = [], qr?: (route: Route) => Promise<void>) {
  const png = await markerPng(page);
  await page.context().route('https://**/*', route => route.abort());
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/douyin/session/qr') return qr ? qr(route) : route.fulfill({contentType: 'image/png', headers: {'Cache-Control': 'no-store'}, body: png});
    if (path.startsWith('/api/douyin/session')) return session(route, path.slice('/api/douyin/session'.length));
    if (route.request().method() !== 'GET') throw new Error('本界面契约禁止真实任务与模型写入：' + path);
    if (path === '/api/status') return route.fulfill({json: {counts: {tasks: tasks.length, skills: 0, knowledge: 0}, diagnostics: {}}});
    if (path === '/api/settings') return route.fulfill({json: {models: {text: {}, vision: {}, transcription: {}}, github: {}, feishu: {}}});
    if (path === '/api/skills' || path === '/api/knowledge') return route.fulfill({json: []});
    if (path === '/api/tasks') return route.fulfill({json: tasks});
    if (path.startsWith('/api/tasks/')) return route.fulfill({json: tasks.find(task => task.id === path.slice('/api/tasks/'.length))});
    return route.fulfill({status: 404, json: {error: {message: '未定义的界面契约读取'}}});
  });
}

for (const viewport of [{width: 1440, height: 1100}, {width: 390, height: 844}]) {
  test(`抖音会话界面契约：${viewport.width}px入口、扫码检查、取消与清除`, async ({page}) => {
    await page.setViewportSize(viewport);
    let current = signedOut(), gets = 0;
    const actions: string[] = [], errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await workspace(page, async (route, action) => {
      if (route.request().method() === 'GET') {gets++; return route.fulfill({json: current});}
      expect(route.request().postDataJSON()).toEqual({}); actions.push(action);
      if (action === '/login') current = {status: 'waiting_scan', session_saved: false, browser_open: true, qr_available: false};
      else if (action === '/check') current = loggedIn();
      else if (action === '/cancel') current = {status: 'cancelled', session_saved: false, browser_open: false};
      else if (action === '/clear') current = signedOut();
      else throw new Error('未定义的抖音会话操作');
      await route.fulfill({json: current});
    });
    await page.goto('/');
    await expect(page.locator('.douyin-connect-entry')).toContainText('默认匿名读取');
    expect(gets).toBe(0);
    await page.getByRole('button', {name: '连接抖音', exact: true}).click();
    const card = page.getByRole('region', {name: '抖音登录与连接', exact: true});
    await expect(card).toHaveCount(1); await expect(card).toBeFocused();
    await expect(card).toContainText('未连接抖音');
    const box = await card.boundingBox(); expect(box!.y).toBeGreaterThanOrEqual(0); expect(box!.y).toBeLessThan(100);
    await expect(card).toContainText('不读取其他浏览器登录'); expect(actions).toEqual([]);
    await card.getByRole('button', {name: '扫码登录抖音', exact: true}).click();
    await expect(card).toContainText('等待手机扫码');
    await expect(card).toContainText('正在等待抖音官网生成二维码');
    await expect(card.locator('img')).toHaveCount(0);
    current = {...current, qr_available: true, qr_updated_at: 'contract-qr-1'};
    const firstQr = page.waitForResponse(response => new URL(response.url()).pathname === '/api/douyin/session/qr');
    await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
    expect((await firstQr).headers()['cache-control']).toBe('no-store');
    const qrImage = card.getByRole('img', {name: '抖音官方登录二维码', exact: true});
    await expect(qrImage).toBeVisible(); await expect(qrImage).toHaveJSProperty('naturalWidth', 240);
    await expect(qrImage).toHaveAttribute('src', '/api/douyin/session/qr?v=contract-qr-1');
    await expect(card).toContainText('用手机抖音扫描下方官方二维码并确认');
    await expect(card.locator('canvas, input')).toHaveCount(0);
    expect(actions).toEqual(['/login']);
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', viewport.width);
    mkdirSync(output, {recursive: true});
    await card.evaluate(element => {const label = document.createElement('p'); label.dataset.contractEvidence = 'true'; label.textContent = '[显式界面契约样例] 模拟登录状态，未连接真实抖音'; label.className = 'douyin-session-eyebrow'; element.prepend(label);});
    await card.screenshot({path: resolve(output, `douyin-session-${viewport.width}-contract.png`), animations: 'disabled'});
    await card.evaluate(element => element.querySelector('[data-contract-evidence]')?.remove());
    current = {...current, status: 'waiting_confirmation', browser_open: false};
    await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
    await expect(card).toContainText('已扫码，等待手机确认');
    await expect(card).toContainText('请在手机抖音确认登录，工作台会自动核验，无需再次扫码');
    await expect(card).toContainText('不会刷新登录挑战');
    await expect(qrImage).toHaveCount(0);
    await expect(card.getByRole('button', {name: '重新获取二维码', exact: true})).toHaveCount(0);
    await expect(card.getByRole('button', {name: '取消本次登录', exact: true})).toBeEnabled();
    await expect(card.getByRole('button', {name: '检查登录状态', exact: true})).toBeEnabled();
    current = {...current, qr_available: false};
    await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
    await expect(qrImage).toHaveCount(0); expect(actions).toEqual(['/login']);
    await card.evaluate(element => {const label = document.createElement('p'); label.textContent = '[显式界面契约样例] 已扫码等待确认，未连接真实抖音'; label.className = 'douyin-session-eyebrow'; label.dataset.contractEvidence = 'true'; element.prepend(label);});
    await card.screenshot({path: resolve(output, `douyin-confirmation-${viewport.width}-contract.png`), animations: 'disabled'});
    await card.evaluate(element => element.querySelector('[data-contract-evidence]')?.remove());
    await card.getByRole('button', {name: '检查登录状态', exact: true}).click();
    await expect(card).toContainText('抖音会话已验证'); await expect(card).toContainText('最近验证');
    await expect(qrImage).toHaveCount(0);
    await expect(card).toContainText('原片获取和分析仍需由任务实际完成');
    await expect(card).toContainText('正在采集的抖音任务会停止该次采集；已保存内容保留');
    await expect(card.getByRole('button', {name: '重新扫码登录', exact: true})).toBeEnabled();
    await card.getByRole('button', {name: '退出并清除本机会话', exact: true}).click();
    await expect(card).toContainText('未连接抖音'); expect(actions).toEqual(['/login', '/check', '/clear']);
    await card.getByRole('button', {name: '扫码登录抖音', exact: true}).click();
    current = {...current, qr_available: true, qr_updated_at: 'contract-qr-2'};
    await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
    await expect(qrImage).toBeVisible();
    await expect(qrImage).toHaveAttribute('src', '/api/douyin/session/qr?v=contract-qr-2');
    await card.getByRole('button', {name: '取消本次登录', exact: true}).click();
    await expect(card).toContainText('本次登录已取消');
    await expect(qrImage).toHaveCount(0);
    await page.waitForTimeout(2700);
    expect(actions).toEqual(['/login', '/check', '/clear', '/login', '/cancel']);
    expect(errors).toEqual([]);
  });
}

test('抖音会话界面契约：轮询串行、断连保留、离开停止读取且不重复登录', async ({page}) => {
  let current = loggedIn(), mode = 'good', reads = 0, active = 0, maxActive = 0;
  let release: () => void = () => {}; const held = new Promise<void>(resolve => {release = resolve;});
  const actions: string[] = [];
  await workspace(page, async (route, action) => {
    if (route.request().method() !== 'GET') {actions.push(action); return route.fulfill({json: current});}
    reads++; active++; maxActive = Math.max(active, maxActive);
    try {
      if (mode === 'hold') await held;
      if (mode === 'fail') await route.fulfill({status: 503, json: {error: {message: '[契约] 本地状态读取失败'}}});
      else await route.fulfill({json: current}).catch(() => {});
    } finally {active--;}
  });
  await page.goto('/#settings');
  const card = page.getByRole('region', {name: '抖音登录与连接', exact: true});
  await expect(card).toContainText('抖音会话已验证');
  mode = 'fail'; await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card.getByRole('alert')).toContainText('保留上次状态');
  await expect(card).toContainText('上次验证不代表当前仍在线');
  await expect(card).toContainText('抖音会话已验证'); expect(actions).toEqual([]);
  mode = 'hold'; await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect.poll(() => active).toBe(1); const before = reads;
  await page.waitForTimeout(3200); expect(reads).toBe(before); expect(maxActive).toBe(1);
  await page.locator('.sidebar .nav-item').filter({hasText: '工作台'}).click();
  await expect(card).toHaveCount(0); release();
  await page.waitForTimeout(2800); expect(reads).toBe(before);
  current = {status: 'expired', session_saved: true, browser_open: false}; mode = 'good';
  await page.getByRole('button', {name: '连接抖音', exact: true}).click();
  await expect(card).toContainText('抖音会话已过期');
  await expect(card.getByRole('alert')).toHaveCount(0); expect(actions).toEqual([]);
  await card.getByRole('button', {name: '检查已保存会话', exact: true}).click();
  await expect.poll(() => actions).toEqual(['/check']);
  await expect(card).toContainText('抖音会话已过期');
  current = loggedIn();
  await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card).toContainText('抖音会话已验证');
  await card.getByRole('button', {name: '检查已保存会话', exact: true}).click();
  await expect.poll(() => actions).toEqual(['/check', '/check']);
  current = {...loggedIn(), status: 'checking'};
  await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card).toContainText('正在核验本项目已保存的抖音会话');
  await expect(card).not.toContainText('扫描下方官方二维码');
  await expect(card.getByRole('button', {name: '重新获取二维码', exact: true})).toHaveCount(0);
});

test('抖音会话界面契约：失败任务入口、验证码与网络错误不冒充登录成功', async ({page}) => {
  let current: Item = {status: 'opening', browser_open: true, session_saved: false};
  const task = {id: 'douyin-login-contract', title: '[契约样例] 抖音原片获取失败', input: 'https://v.douyin.com/login-contract/', status: 'waiting_material', stage: 'fetch', stages: [], error: {code: 'login_required', message: '[契约] 来源需要登录'}, source: {platform: 'douyin'}};
  const actions: string[] = []; let qrFails = true, allowRestart = true;
  const png = await markerPng(page);
  await workspace(page, async (route, action) => {
    if (route.request().method() === 'GET') return route.fulfill({json: current});
    actions.push(action);
    if (action === '/login' && allowRestart) {allowRestart = false; current = {status: 'opening', browser_open: true, session_saved: false, qr_available: false}; return route.fulfill({json: current});}
    return route.fulfill({status: 503, json: {error: {code: 'network', message: '[契约] 专用窗口暂时无法打开'}}});
  }, [task], route => qrFails ? route.fulfill({status: 503, body: 'contract image unavailable'}) : route.fulfill({contentType: 'image/png', headers: {'Cache-Control': 'no-store'}, body: png}));
  await page.goto('/#tasks'); await page.locator('.task-row').click();
  await page.getByRole('button', {name: '连接抖音并检查登录', exact: true}).click();
  const card = page.getByRole('region', {name: '抖音登录与连接', exact: true});
  await expect(card).toBeFocused(); await expect(card).toContainText('正在准备官方二维码');
  current = {...current, status: 'checking'};
  await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card).toContainText('正在核验登录状态');
  current = {...current, status: 'verification_required'};
  await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card).toContainText('需要本人完成官方验证');
  await expect(card).toContainText('请勿把验证内容发到聊天');
  current = {...current, status: 'waiting_scan', qr_available: true, qr_updated_at: 'contract-image-failure'};
  await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card.getByRole('alert')).toContainText('二维码暂时无法显示');
  await expect(card).not.toContainText('抖音会话已验证');
  qrFails = false;
  await card.getByRole('button', {name: '重新加载二维码', exact: true}).click();
  await expect(card.getByRole('img', {name: '抖音官方登录二维码', exact: true})).toHaveJSProperty('naturalWidth', 240);
  await card.getByRole('button', {name: '重新获取二维码', exact: true}).click();
  await expect(card.getByRole('img', {name: '抖音官方登录二维码', exact: true})).toHaveCount(0);
  await expect(card).toContainText('正在等待抖音官网生成二维码');
  expect(actions).toEqual(['/login']);
  current = {...current, status: 'waiting_scan', qr_available: true, qr_updated_at: 'contract-restarted'};
  await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card.getByRole('img', {name: '抖音官方登录二维码', exact: true})).toHaveAttribute('src', '/api/douyin/session/qr?v=contract-restarted');
  current = {...current, status: 'verification_required', browser_open: false, qr_available: false};
  await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card.getByRole('button', {name: '放弃验证并重新扫码', exact: true})).toBeEnabled();
  await expect(card.getByRole('img', {name: '抖音官方登录二维码', exact: true})).toHaveCount(0);
  current = {status: 'failed', browser_open: false, session_saved: false, error: {code: 'network', message: '[契约] 官方页面连接失败'}};
  await card.getByRole('button', {name: '刷新登录状态', exact: true}).click();
  await expect(card).toContainText('官方页面连接失败');
  await expect(card.getByRole('button', {name: '扫码登录抖音', exact: true})).toBeEnabled();
  await card.getByRole('button', {name: '扫码登录抖音', exact: true}).click();
  await expect(card.getByRole('alert')).toContainText('操作结果未确认');
  await expect(card.getByRole('alert')).toContainText('不会自动重复登录操作');
  await expect(card).not.toContainText('抖音会话已验证');
  await page.waitForTimeout(2800); expect(actions).toEqual(['/login', '/login']);
});
