import {test, expect} from '@playwright/test';
import type {Page, Route} from '@playwright/test';
import {mkdirSync} from 'node:fs';
import {resolve} from 'node:path';

type Item = Record<string, any>;
const output = process.env.UI_ARTIFACT_DIR || '/tmp/skill-collect-ui-douyin-verification-contract-v030';
const frameId = (n: number) => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
const frame = (n: number): Item => ({status: 'verification_required', session_saved: false, browser_open: true, qr_available: false,
  verification_available: true, verification_frame_id: frameId(n), verification_width: 1280, verification_height: 800});
const consume = (value: Item): Item => ({...value, verification_available: false, verification_frame_id: null});

async function jpeg(page: Page): Promise<Buffer> {
  const data = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 1280; canvas.height = 800;
    const context = canvas.getContext('2d')!;
    context.fillStyle = '#f3f7ec'; context.fillRect(0, 0, 1280, 800);
    context.fillStyle = '#536d42'; context.font = 'bold 44px sans-serif';
    context.fillText('CONTRACT / NOT OFFICIAL', 40, 65);
    context.textAlign = 'center'; context.font = 'bold 68px sans-serif'; context.fillText('CONTRACT', 640, 380);
    context.font = '48px sans-serif'; context.fillText('NOT OFFICIAL', 640, 455);
    return canvas.toDataURL('image/jpeg');
  });
  return Buffer.from(data.split(',')[1], 'base64');
}
async function workspace(page: Page, get: (route: Route) => Promise<void>, action: (route: Route) => Promise<void>, image?: (route: Route) => Promise<void>) {
  const picture = await jpeg(page);
  await page.context().route('https://**/*', route => route.abort());
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/douyin/session/verification/action') return action(route);
    if (path === '/api/douyin/session/verification') return image ? image(route) : route.fulfill({contentType: 'image/jpeg', headers: {'Cache-Control': 'no-store'}, body: picture});
    if (path === '/api/douyin/session') return get(route);
    if (route.request().method() !== 'GET') throw new Error('Official-view contract blocks all other writes: ' + path);
    if (path === '/api/status') return route.fulfill({json: {counts: {}, diagnostics: {}}});
    if (path === '/api/settings') return route.fulfill({json: {models: {text: {}, vision: {}, transcription: {}}, github: {}, feishu: {}}});
    if (['/api/tasks', '/api/skills', '/api/knowledge'].includes(path)) return route.fulfill({json: []});
    return route.fulfill({status: 404, json: {error: {message: 'Undefined explicit UI contract read'}}});
  });
}
function metadata(payload: Item): Item {
  const {text, ...safe} = payload;
  return {...safe, ...(typeof text === 'string' ? {text_length: text.length} : {})};
}

for (const size of [{width: 1440, height: 1100}, {width: 390, height: 844}]) {
  test(`官方验证视图契约：${size.width}px等比及放大坐标、旧帧锁定、临时输入清空`, async ({page}) => {
    await page.setViewportSize(size);
    let current = frame(1); const actions: Item[] = [];
    const transient = 'contract-transient-input'; let leaked = false;
    page.on('console', message => {if (message.text().includes(transient)) leaked = true;});
    await workspace(page, route => route.fulfill({json: current}), async route => {
      actions.push(metadata(route.request().postDataJSON())); current = consume(current);
      await route.fulfill({json: current});
    });
    await page.goto('/'); await page.getByRole('button', {name: '连接抖音', exact: true}).click();
    const card = page.getByRole('region', {name: '抖音登录与连接', exact: true});
    const view = page.getByRole('region', {name: '抖音官方验证视图', exact: true});
    const image = view.getByRole('img', {name: '抖音官网当前验证画面', exact: true});
    const control = view.getByRole('button', {name: '点击官方验证画面', exact: true});
    const refreshState = card.getByRole('button', {name: '刷新登录状态', exact: true});
    await expect(control).toBeEnabled(); await expect(image).toHaveJSProperty('naturalWidth', 1280);
    await view.locator('.douyin-verification-viewport').scrollIntoViewIfNeeded();
    let box = (await image.boundingBox())!;
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await expect.poll(() => actions.length).toBe(1);
    expect(actions[0].kind).toBe('click'); expect(actions[0].frame_id).toBe(frameId(1));
    expect(Number.isInteger(actions[0].x) && Number.isInteger(actions[0].y)).toBe(true);
    expect(Math.abs(actions[0].x - 640)).toBeLessThanOrEqual(2); expect(Math.abs(actions[0].y - 400)).toBeLessThanOrEqual(2);
    await expect(view.getByRole('button', {name: '刷新官方画面', exact: true})).toBeDisabled();
    current = frame(1); // Explicit late old snapshot must not unlock a consumed UUID.
    await refreshState.click(); await expect(control).toBeDisabled();
    await expect(view.getByRole('button', {name: 'Enter', exact: true})).toBeDisabled(); expect(actions).toHaveLength(1);
    current = frame(2); await refreshState.click(); await expect(control).toBeEnabled();
    await view.getByRole('button', {name: '150%', exact: true}).click();
    const viewport = view.locator('.douyin-verification-viewport'); await viewport.scrollIntoViewIfNeeded();
    await viewport.evaluate(element => {element.scrollLeft = 0; element.scrollTop = 0;});
    box = (await image.boundingBox())!; expect(box.width).toBeGreaterThan(1800);
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', size.width);
    expect(await viewport.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
    await page.mouse.click(box.x + 80 * box.width / 1280, box.y + 60 * box.height / 800);
    await expect.poll(() => actions.length).toBe(2);
    expect(actions[1].frame_id).toBe(frameId(2)); expect(Math.abs(actions[1].x - 80)).toBeLessThanOrEqual(1); expect(Math.abs(actions[1].y - 60)).toBeLessThanOrEqual(1);
    current = frame(3); await refreshState.click(); await expect(control).toBeEnabled();
    const input = view.getByLabel('临时输入（不保存）', {exact: true});
    await expect(input).toHaveAttribute('type', 'password'); await expect(input).toHaveAttribute('maxlength', '128');
    await input.fill(transient); await view.getByRole('button', {name: '输入到官网', exact: true}).click();
    await expect.poll(async () => (await input.inputValue()).length).toBe(0);
    await expect.poll(() => actions.length).toBe(3); expect(actions[2].kind).toBe('text'); expect(actions[2].text_length).toBe(transient.length);
    expect(actions[2]).not.toHaveProperty('text'); expect((await view.innerText()).includes(transient)).toBe(false);
    for (const [i, key] of ['Tab', 'Enter', 'Backspace'].entries()) {
      current = frame(4 + i); await refreshState.click();
      const keyButton = view.getByRole('button', {name: key, exact: true}); await expect(keyButton).toBeEnabled(); await keyButton.click();
      await expect.poll(() => actions.length).toBe(4 + i); expect(actions.at(-1)?.kind).toBe('key'); expect(actions.at(-1)?.key).toBe(key);
    }
    current = frame(7); await refreshState.click(); await expect(control).toBeEnabled();
    await view.getByRole('button', {name: '适应宽度', exact: true}).click();
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', size.width);
    mkdirSync(output, {recursive: true});
    await view.evaluate(element => {const label = document.createElement('p'); label.textContent = '[显式界面契约样例] CONTRACT / NOT OFFICIAL；未连接官网或发送短信'; label.className = 'douyin-session-eyebrow'; element.prepend(label);});
    await view.screenshot({path: resolve(output, `douyin-verification-${size.width}-contract.png`), animations: 'disabled'});
    await input.fill(transient); current = {...current, status: 'cancelled', browser_open: false}; await refreshState.click();
    await expect(view).toHaveCount(0); await expect(page.locator('#douyin-official-transient')).toHaveCount(0);
    current = frame(8); await refreshState.click(); await expect(control).toBeEnabled();
    await expect.poll(async () => (await view.getByLabel('临时输入（不保存）', {exact: true}).inputValue()).length).toBe(0);
    expect(leaked).toBe(false);
  });
}

test('官方验证视图契约：缺帧/图片错误/断连/不确定动作只等待新帧且不重发', async ({page}) => {
  let current = consume(frame(1)), imageFails = true, readFails = false;
  let releaseNewImage: (() => void) | undefined;
  const actions: Item[] = [], transient = 'contract-private-transient'; let leaked = false;
  const picture = await jpeg(page);
  page.on('console', message => {if (message.text().includes(transient)) leaked = true;});
  await workspace(page, route => readFails ? route.fulfill({status: 503, json: {error: {message: 'Contract status offline'}}}) : route.fulfill({json: current}), async route => {
    const payload = route.request().postDataJSON(); actions.push(metadata(payload));
    if (payload.kind === 'text') return route.fulfill({status: 503, json: {error: {message: transient}}});
    current = consume(current); await route.fulfill({json: current});
  }, async route => {
    if (imageFails) return route.fulfill({status: 503, body: 'Contract image unavailable'});
    if (new URL(route.request().url()).searchParams.get('v') === frameId(3)) {
      await new Promise<void>(resolve => {releaseNewImage = resolve;});
    }
    await route.fulfill({contentType: 'image/jpeg', headers: {'Cache-Control': 'no-store'}, body: picture});
  });
  await page.goto('/#settings');
  const card = page.getByRole('region', {name: '抖音登录与连接', exact: true});
  const view = page.getByRole('region', {name: '抖音官方验证视图', exact: true});
  const refreshState = card.getByRole('button', {name: '刷新登录状态', exact: true});
  await expect(view).toContainText('正在等待官网验证画面');
  await expect(view.getByRole('button', {name: '刷新官方画面', exact: true})).toBeDisabled();
  await page.waitForTimeout(2700); expect(actions).toHaveLength(0);
  current = frame(1); await refreshState.click();
  await expect(view.getByRole('alert')).toContainText('官网画面暂时无法显示');
  await expect(view.getByRole('button', {name: '刷新官方画面', exact: true})).toBeEnabled();
  await view.getByRole('button', {name: '刷新官方画面', exact: true}).click();
  await expect.poll(() => actions.length).toBe(1); expect(actions[0].kind).toBe('refresh');
  await expect(view.getByRole('button', {name: '刷新官方画面', exact: true})).toBeDisabled();
  current = frame(2); imageFails = false; await refreshState.click();
  const input = view.getByLabel('临时输入（不保存）', {exact: true}); await expect(input).toBeEnabled();
  await input.fill(transient); await expect(input).toBeFocused(); current = frame(3);
  await expect(view.getByRole('img', {name: '抖音官网当前验证画面', exact: true})).toHaveAttribute('src', '/api/douyin/session/verification?v=' + frameId(3), {timeout: 7000});
  await expect.poll(() => !!releaseNewImage).toBe(true);
  await expect(input).toBeEnabled(); await expect(input).toBeFocused();
  await expect.poll(async () => (await input.inputValue()).length).toBe(transient.length);
  const submitText = view.getByRole('button', {name: '输入到官网', exact: true});
  await expect(submitText).toBeDisabled();
  await expect(view.getByRole('button', {name: '点击官方验证画面', exact: true})).toBeDisabled();
  await expect(view.getByRole('button', {name: 'Enter', exact: true})).toBeDisabled();
  expect(actions).toHaveLength(1);
  releaseNewImage!();
  await expect(submitText).toBeEnabled(); await expect(input).toBeFocused();
  await submitText.click();
  await expect.poll(async () => (await input.inputValue()).length).toBe(0);
  await expect(card).toContainText('官网操作结果尚未确认'); expect((await card.innerText()).includes(transient)).toBe(false);
  await page.waitForTimeout(2700); expect(actions).toHaveLength(2);
  await expect(view.getByRole('button', {name: '刷新官方画面', exact: true})).toBeDisabled();
  readFails = true; await refreshState.click();
  await expect(view.getByRole('alert')).toContainText('状态连接有问题');
  await expect(input).toBeDisabled();
  readFails = false; current = {...frame(4), verification_error: {code: 'not_editable', message: '请先点击当前官网输入框。'}};
  await refreshState.click(); await expect(input).toBeEnabled();
  await expect(view.getByRole('alert')).toContainText('请先点击当前官网输入框');
  await input.fill(transient); current = {status: 'logged_in', session_saved: true, browser_open: false}; await refreshState.click();
  await expect(view).toHaveCount(0); await expect(page.locator('#douyin-official-transient')).toHaveCount(0); expect(leaked).toBe(false);
  current = frame(5); await refreshState.click(); await expect(input).toBeEnabled();
  await expect.poll(async () => (await input.inputValue()).length).toBe(0);
});
