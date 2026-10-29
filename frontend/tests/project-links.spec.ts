import { test, expect } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const artifacts = process.env.UI_ARTIFACT_DIR || '/tmp/skill-collect-ui-project-links';
const quote = '[契约] 画面显示 https://github.com/review-lab/evidence-workflow。';
const audioQuote = '[契约] 这里介绍了 Evidence Helper 这个项目。';
const provider = 'https://model.contract.invalid/v1';

function fixture() {
  const source = {platform: 'douyin', basis: 'transcript_and_frames', snapshot_hash: 'ui-contract-source',
    url: 'https://www.douyin.com/video/contract-fixture',
    original_input: '[契约] https://github.com/review-lab/user-supplied',
    evidence: [
      {id: 'frames-observation', kind: 'model_frame_observation', time: '18.4s, 36.7s', excerpt: quote},
      {id: 'audio-0', kind: 'transcript', time: '0—120s', excerpt: audioQuote},
    ]};
  const candidate = {repository: 'review-lab/evidence-helper', url: 'https://github.com/review-lab/evidence-helper',
    description: '[契约] 一个名称相似的搜索结果，尚未确认与视频中的项目相同。', match: 'search_candidate',
    verification_status: 'search_result', same_project_confirmed: false};
  const reference = {id: 'audio-0', kind: 'transcript', time: '0—120s', quote: audioQuote};
  return {id: 'ui-project-links-video', title: '[契约样例] 视频项目关联', input: 'https://v.douyin.com/contract-fixture/',
    status: 'succeeded', stage: 'done', stages: [{stage: 'project_links', status: 'succeeded', message: '[契约] 关联阶段已完成'}],
    source, analysis: {overview: '[契约样例] 转写和画面分析，非真实供应商结果。', basis: source.basis, model: 'ui-contract-text'},
    project_links: {schema_version: 1, basis: source.basis, source_snapshot_hash: source.snapshot_hash, status: 'partial',
      links: [
        {id: 'direct', name: '[契约] 明确链接项目', status: 'repository_verified',
          evidence: [{id: 'frames-observation', kind: 'model_frame_observation', time: '18.4s, 36.7s', quote}],
          explicit_urls: ['https://github.com/review-lab/evidence-workflow'], candidates: [
            {repository: 'review-lab/evidence-workflow', url: 'https://github.com/review-lab/evidence-workflow',
              description: '[契约] 仅核验仓库存在，未运行代码。', version: 'a'.repeat(40), match: 'explicit_url', verification_status: 'repository_exists', same_project_confirmed: false}]},
        {id: 'single', name: '[契约] Evidence Helper 单一候选', status: 'candidates', query: '"Evidence Helper" in:name,description', evidence: [reference], candidates: [candidate]},
        {id: 'ambiguous', name: '[契约] Evidence Helper 同名歧义', status: 'ambiguous', evidence: [reference], candidates: [candidate,
          {...candidate, repository: 'another-lab/evidence-helper', url: 'https://github.com/another-lab/evidence-helper'}]},
        {id: 'missing', name: '[契约] 暂无候选', status: 'no_match', evidence: [reference], candidates: []},
        {id: 'limited', name: '[契约] GitHub 限流', status: 'unavailable', evidence: [reference], candidates: [],
          error: {code: 'rate_limit', message: '[契约] GitHub 本次限流，尚未完成检索。', retryable: true}},
        {id: 'invalid', name: '[契约] 非法候选地址', status: 'invalid', evidence: [reference], candidates: [
          {...candidate, url: 'https://github.com.evil.invalid/review-lab/evidence-helper'}]},
        {id: 'unlocated', name: '[契约] 无法定位的引用', status: 'candidates', evidence: [{id: 'not-in-source', kind: 'transcript', time: '99s', quote: '[契约] 不存在的时间证据'}], candidates: []},
        {id: 'user-input', name: '[契约] 用户补充链接', status: 'repository_verified', evidence: [
          {id: 'source-original_input', kind: 'user_input', quote: source.original_input}], candidates: [
          {...candidate, repository: 'review-lab/user-supplied', url: 'https://github.com/review-lab/user-supplied', match: 'explicit_url', verification_status: 'repository_exists'}]},
      ], errors: [], warnings: [], limits: {truncated: true}},
  };
}

for (const viewport of [{width: 1440, height: 1100, label: '桌面'}, {width: 390, height: 844, label: '窄屏'}]) {
  test(`GitHub项目关联界面契约：${viewport.label}证据、候选边界、单独检索与手动分析`, async ({page, context}) => {
    await page.setViewportSize(viewport);
    mkdirSync(artifacts, {recursive: true});
    let current: any = fixture(), repositoryTask: any = null;
    const taskCreates: any[] = [], modelCalls: string[] = [], unexpected: string[] = [], pageErrors: string[] = [];
    let lookups = 0, externalOpens = 0;
    page.on('pageerror', error => pageErrors.push(error.message));
    await context.route('https://**/*', route => {
      if (route.request().url() === 'https://github.com/review-lab/evidence-workflow') {
        externalOpens++; return route.fulfill({contentType: 'text/html', body: '<title>显式 GitHub 打开契约</title>'});
      }
      unexpected.push('unexpected external request'); return route.abort();
    });
    await page.route('**/api/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
      if (path === '/api/status') return route.fulfill({json: {counts: {tasks: 1, knowledge: 0, skills: 0}, diagnostics: {}}});
      if (path === '/api/settings') return route.fulfill({json: {models: {text: {base_url: provider, model: 'ui-contract-text'}, vision: {}, transcription: {}}, github: {}, feishu: {}}});
      if (path === '/api/knowledge' || path === '/api/skills') return route.fulfill({json: []});
      if (path.startsWith('/api/models/')) {modelCalls.push(path); return route.fulfill({status: 400, json: {error: {message: '本契约禁止模型调用'}}});}
      if (path === '/api/tasks' && method === 'GET') return route.fulfill({json: [current]});
      if (path === '/api/tasks' && method === 'POST') {
        taskCreates.push(request.postDataJSON());
        repositoryTask = {id: 'ui-created-repository', title: '[契约] 手动创建的 GitHub 任务', input: request.postDataJSON().input,
          status: 'queued', stage: 'queued', stages: [], source: {platform: 'github', basis: 'source_files'}};
        return route.fulfill({json: repositoryTask});
      }
      if (path === `/api/tasks/${current.id}/project-links` && method === 'POST') {
        lookups++; current = {...current, project_links: {...fixture().project_links, basis: current.source.basis, source_snapshot_hash: current.source.snapshot_hash}};
        return route.fulfill({json: current});
      }
      if (path === `/api/tasks/${current.id}`) return route.fulfill({json: current});
      if (repositoryTask && path === `/api/tasks/${repositoryTask.id}`) return route.fulfill({json: repositoryTask});
      unexpected.push(`${method} ${path}`); return route.fulfill({status: 404, json: {error: {message: '未定义的界面契约请求'}}});
    });
    const open = async () => {await page.goto('/#tasks'); await page.locator('.task-row').filter({hasText: current.title}).click();};
    await page.goto('/');
    await expect(page.locator('.composer-note')).toContainText('由本工作台自动尝试获取原片');
    await expect(page.locator('.composer-note')).toContainText('转写音轨 → 分析抽样画面 → 内容分析 → 关联 GitHub 项目');
    await expect(page.locator('.composer-note')).toContainText('无需 Codex');
    await open();
    const section = page.getByRole('region', {name: 'GitHub 项目关联', exact: true});
    await expect(section.getByRole('heading', {name: '转写与抽样画面提到的 GitHub 项目', exact: true})).toBeVisible();
    await expect(section).toContainText('部分项目尚未匹配');
    const direct = section.locator('.project-link-item').filter({hasText: '[契约] 明确链接项目'});
    await expect(direct.locator('.project-candidate-status')).toHaveText('明确链接 · 仓库存在已核验');
    await expect(direct).toContainText('来源时间范围 / 取样点：18.4s, 36.7s');
    await expect(direct.getByText(quote, {exact: true})).toBeVisible();
    const single = section.locator('.project-link-item').filter({hasText: '[契约] Evidence Helper 单一候选'});
    await expect(single.locator('.project-candidate-status')).toHaveText('名称检索候选 · 尚未确认');
    await expect(single).toContainText('来源时间范围 / 取样点：0—120s');
    const ambiguous = section.locator('.project-link-item').filter({hasText: '[契约] Evidence Helper 同名歧义'});
    await expect(ambiguous.locator('.project-candidate')).toHaveCount(2);
    await expect(ambiguous).toContainText('没有自动选定仓库');
    await expect(section.locator('.project-link-item').filter({hasText: '[契约] 暂无候选'})).toContainText('此次检索未找到候选，不代表项目不存在');
    await expect(section.locator('.project-link-item').filter({hasText: '[契约] GitHub 限流'})).toContainText('rate_limit');
    await expect(section.locator('.project-link-item').filter({hasText: '[契约] GitHub 限流'})).toContainText('[契约] GitHub 本次限流，尚未完成检索。');
    const invalid = section.locator('.project-link-item').filter({hasText: '[契约] 非法候选地址'});
    await expect(invalid.getByRole('link', {name: '打开 GitHub', exact: true})).toHaveCount(0);
    await expect(invalid.getByRole('button', {name: '分析此候选仓库', exact: true})).toBeDisabled();
    const unlocated = section.locator('.project-link-item').filter({hasText: '[契约] 无法定位的引用'});
    await expect(unlocated).toContainText('无法在当前来源中定位'); await expect(unlocated).not.toContainText('99s');
    const supplied = section.locator('.project-link-item').filter({hasText: '[契约] 用户补充链接'});
    await expect(supplied).toContainText('用户输入线索（非原片证据）');
    await expect(supplied).toContainText('没有原片时间定位');
    await expect(section).toContainText('已达到本次扫描或检索上限');
    await expect(section).toContainText(provider); expect(taskCreates).toHaveLength(0); expect(lookups).toBe(0);
    await expect(page.getByText('关联GitHub项目', {exact: true})).toBeVisible();
    await direct.getByRole('button', {name: '查看此处来源证据', exact: true}).click();
    await expect(page.locator('#task-evidence-frames-observation')).toBeFocused();
    await expect(page.locator('#task-evidence-frames-observation')).toContainText(quote);
    await page.getByRole('button', {name: '分析结果', exact: true}).click();
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', viewport.width);
    const popupPromise = page.waitForEvent('popup');
    await direct.getByRole('link', {name: '打开 GitHub', exact: true}).click();
    const popup = await popupPromise; await popup.waitForLoadState(); await expect(popup).toHaveURL('https://github.com/review-lab/evidence-workflow'); await popup.close();
    expect(externalOpens).toBe(1); expect(taskCreates).toHaveLength(0);
    await section.getByRole('button', {name: '重新检索项目', exact: true}).click();
    await expect.poll(() => lookups).toBe(1); expect(taskCreates).toHaveLength(0); expect(modelCalls).toHaveLength(0);
    await section.screenshot({path: resolve(artifacts, `project-links-${viewport.width}-contract.png`), animations: 'disabled'});

    // Platform summaries and uploaded subtitles retain their own evidence boundary.
    current = {...fixture(), source: {...fixture().source, basis: 'text_only', evidence: [{id: 'text-1', kind: 'text', time: '00:12', excerpt: audioQuote}]},
      project_links: {...fixture().project_links, basis: 'text_only', links: [{...fixture().project_links.links[1], evidence: [{id: 'text-1', kind: 'text', time: '00:12', quote: audioQuote}]}]}};
    await page.reload(); await page.locator('.task-row').click();
    await expect(section.getByRole('heading', {name: '相关文本中的 GitHub 项目', exact: true})).toBeVisible();
    await expect(section).toContainText('平台 AI 摘要只能作为文本线索');
    await expect(section).toContainText('文本所载时间（未经原片核验）：00:12');
    await expect(section.getByRole('heading', {name: '转写与抽样画面提到的 GitHub 项目', exact: true})).toHaveCount(0);
    current = {...current, source: {...current.source, basis: 'subtitles'}, project_links: {...current.project_links, basis: 'subtitles'}};
    await page.reload(); await page.locator('.task-row').click();
    await expect(section.getByRole('heading', {name: '字幕中提到的 GitHub 项目', exact: true})).toBeVisible();
    await expect(section).toContainText('时间来自字幕文件');

    current = {...fixture(), source: {...fixture().source, basis: 'frames_only', evidence: [fixture().source.evidence[0]]},
      analysis: {...fixture().analysis, basis: 'frames_only'}, project_links: {...fixture().project_links, basis: 'frames_only', links: [fixture().project_links.links[0]]}};
    await page.reload(); await page.locator('.task-row').click();
    await expect(page.locator('.page-heading .badge')).toHaveText('仅画面分析完成');
    await expect(page.locator('.analysis-meta')).toContainText('仅抽样画面（无音频转写）');
    await expect(section.getByRole('heading', {name: '抽样画面中的 GitHub 项目', exact: true})).toBeVisible();
    await expect(section).toContainText('没有音频转写');

    // Customer-side acquisition is the main path; missing browser is not success.
    current = {...fixture(), status: 'running', stage: 'fetch', source: undefined, analysis: undefined, project_links: undefined,
      stages: [{stage: 'fetch', status: 'running', message: '[契约] 工作台正在自动获取原片'}]};
    await page.reload(); await page.locator('.task-row').click();
    await expect(page.locator('.page-heading .badge')).toHaveText('进行中');
    await expect(page.locator('.stage-panel')).toContainText('当前阶段：自动获取原片');
    await expect(page.getByRole('button', {name: '备用：补传视频或字幕', exact: true})).toBeVisible();
    await expect(page.getByRole('button', {name: '生成 Skill 草稿', exact: true})).toHaveCount(0);
    current = {...current, status: 'waiting_material', error: {code: 'douyin_browser_missing', stage: 'fetch',
      message: '[契约] 缺少 Chromium 浏览器组件，请运行项目安装程序后重试。'}};
    await page.reload(); await expect(page.locator('.task-row')).toContainText('自动获取未完成'); await page.locator('.task-row').click();
    await expect(page.locator('.page-heading .badge')).toHaveText('自动获取未完成');
    await expect(page.getByText('自动获取尚未完成，任务已保留', {exact: true})).toBeVisible();
    await expect(page.getByText(current.error.message, {exact: true})).toBeVisible();
    await expect(page.getByText('当前阶段：自动获取原片', {exact: true})).toBeVisible();
    await expect(page.getByText('本次尚未完成自动原片分析。请先根据原因处理后重试；补传仅作备用降级，不代表自动获取已成功。', {exact: true})).toBeVisible();
    await expect(page.getByRole('button', {name: '查看本机模型诊断', exact: true})).toHaveCount(0);
    await expect(section.getByRole('button', {name: '检索项目', exact: true})).toBeDisabled();
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', viewport.width);
    await page.screenshot({path: resolve(artifacts, `project-acquisition-${viewport.width}-contract.png`), fullPage: true, animations: 'disabled'});
    expect(taskCreates).toHaveLength(0); expect(modelCalls).toHaveLength(0);

    // Historical tasks, genuine empty reports and stale snapshots are distinct.
    current = {...fixture(), project_links: undefined};
    await page.reload(); await page.locator('.task-row').click();
    await expect(section).toContainText('此任务尚无项目关联记录'); await expect(section).not.toContainText('本次未提取到可关联');
    await expect(section.getByRole('button', {name: '检索项目', exact: true})).toBeEnabled();
    current = {...fixture(), project_links: {...fixture().project_links, status: 'none', links: [], limits: {}}};
    await page.reload(); await page.locator('.task-row').click();
    await expect(section).toContainText('本次未提取到可关联的 GitHub 项目线索，不代表材料中一定没有项目');
    current = {...fixture(), project_links: {...fixture().project_links, status: 'none', links: [fixture().project_links.links[3]], limits: {}}};
    await page.reload(); await page.locator('.task-row').click();
    await expect(section).toContainText('已检索，未匹配仓库');
    await expect(section).toContainText('此次检索未找到候选，不代表项目不存在');
    await expect(section).not.toContainText('本次未提取到可关联的 GitHub 项目线索');
    current = {...fixture(), project_links: {...fixture().project_links, source_snapshot_hash: 'outdated-contract-snapshot'}};
    await page.reload(); await page.locator('.task-row').click();
    await expect(section).toContainText('关联记录与当前来源快照不一致');
    await expect(section.locator('.project-candidate')).toHaveCount(0);
    await expect(section.getByRole('button', {name: '分析此仓库', exact: true})).toHaveCount(0);

    current = fixture(); await page.reload(); await page.locator('.task-row').click();
    await direct.getByRole('button', {name: '分析此仓库', exact: true}).click();
    await expect.poll(() => taskCreates.length).toBe(1);
    expect(taskCreates[0]).toEqual({input: 'https://github.com/review-lab/evidence-workflow', depth: 'quick'});
    await expect(page.locator('.page-heading h1')).toHaveText('[契约] 手动创建的 GitHub 任务');
    await expect(section).toHaveCount(0); // Repository sources do not acquire video cards.
    expect(modelCalls).toHaveLength(0); expect(unexpected).toHaveLength(0); expect(pageErrors).toHaveLength(0);
    writeFileSync(resolve(artifacts, `project-links-${viewport.width}-checks.json`), JSON.stringify({
      status: 'passed', viewport, evidence_kind: 'explicit_ui_contract_only',
      checks: ['actual evidence excerpts and recorded time ranges', 'explicit repository existence distinct from identity and execution verification',
        'single and ambiguous name candidates remain unconfirmed', 'no-match distinct from lookup error', 'user input is not original-video evidence',
        'invalid URLs and unknown evidence not promoted', 'source evidence jump', 'manual open intercepted locally', 'single lookup without model calls',
        'text-summary/subtitle/frames-only/legacy/empty/stale states distinct', 'automatic acquisition is the primary customer path',
        'missing Chromium names the stage and cause without claiming success or requiring upload as the primary path',
        'no horizontal overflow', 'one manual repository task uses existing API', 'repository task hides media card'],
      api_calls: {manual_project_lookups: lookups, explicit_task_creates: taskCreates.length, model_calls: modelCalls.length},
      real_github_calls: false, real_model_calls: false, user_configuration_modified: false,
    }, null, 2));
  });
}
