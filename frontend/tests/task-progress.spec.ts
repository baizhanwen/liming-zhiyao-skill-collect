import {test, expect} from '@playwright/test';
import type {Page, Route} from '@playwright/test';
import {mkdirSync} from 'node:fs';
import {resolve} from 'node:path';

const output=process.env.UI_ARTIFACT_DIR||'/tmp/skill-collect-ui-progress-v021';
type Item=Record<string,any>;
const ago=(seconds:number)=>new Date(Date.now()-seconds*1000).toISOString();
function running(id='progress-a'):Item{return {id,title:'[契约样例] 链接分析与进度等待',input:'https://v.douyin.com/progress-contract/',status:'running',stage:'analysis',attempts:4,started_at:ago(75),created_at:ago(80),stages:[{stage:'analysis',status:'running',message:'[契约] 请求已发出，等待服务响应',at:ago(65)}],live:{observed_at:new Date().toISOString(),worker_active:true,last_activity_at:ago(65),stage_started_at:ago(65),elapsed_seconds:75,stage_elapsed_seconds:65,activity_age_seconds:65,phase:'model_analysis',waiting_on:'text_service',detail:'[契约] 已发出文字分析请求，等待服务响应',metrics:{proxy_bytes:2097152}}};}

async function mockWorkspace(page:Page, list:()=>Item[], detail:(route:Route,id:string)=>Promise<void>, writes:Item[]){
  await page.context().route('https://**/*',route=>route.abort());
  await page.route('**/api/**',async route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    if(request.method()!=='GET'){
      writes.push({path,method:request.method()});return route.fulfill({status:400,json:{error:{message:'本契约禁止提交任务或调用模型'}}});
    }
    if(path==='/api/status')return route.fulfill({json:{counts:{tasks:list().length,skills:0,knowledge:0},diagnostics:{}}});
    if(path==='/api/settings')return route.fulfill({json:{models:{text:{},vision:{},transcription:{}},github:{},feishu:{}}});
    if(path==='/api/skills'||path==='/api/knowledge')return route.fulfill({json:[]});
    if(path==='/api/tasks')return route.fulfill({json:list()});
    if(path.startsWith('/api/tasks/'))return detail(route,path.slice('/api/tasks/'.length));
    return route.fulfill({status:404,json:{error:{message:'未定义的界面契约读取'}}});
  });
}

for(const viewport of [{width:1440,height:1100},{width:390,height:844}]){
  test(`进度契约：${viewport.width}px顶部进度、真实计数、排队与取消范围`,async({page})=>{
    await page.setViewportSize(viewport);mkdirSync(output,{recursive:true});
    let current=running(),queued:Item={...running('progress-queue'),title:'[契约样例] 下一条排队任务',status:'queued',stage:'queued',live:{worker_active:false,queue_position:2,elapsed_seconds:20,stage_elapsed_seconds:0,activity_age_seconds:20,waiting_on:'queue',detail:'[契约] 等待工作线程接手',metrics:{}}};
    const writes:Item[]=[];
    await mockWorkspace(page,()=>[current,queued],async(route,id)=>route.fulfill({json:id===current.id?current:queued}),writes);
    await page.goto('/');
    const home=page.getByRole('region',{name:'正在进行的任务进度',exact:true});
    await expect(home).toContainText('等待文字服务');
    await expect(home.getByRole('button',{name:'查看任务进度',exact:true})).toBeVisible();
    await expect(page.locator('.task-list-live-clock').first()).toContainText('历时');
    await page.evaluate(()=>window.scrollTo(0,document.body.scrollHeight));
    await home.getByRole('button',{name:'查看任务进度',exact:true}).click();
    const progress=page.getByRole('region',{name:'当前任务进度',exact:true});
    await expect(progress).toContainText('状态连接正常');
    await expect(progress).toContainText('当前步骤已等待较久');
    await expect(progress).toContainText('不能据此判断已卡死');
    await expect(progress).toContainText('网页代理总流量：2.0 MB');
    await expect(progress.locator('.task-progress-clock')).toContainText('已尝试 4 次');
    await expect(progress).not.toContainText('实际取回数据');
    await expect(progress).not.toContainText('模型仍在生成');
    await expect(progress.getByRole('progressbar')).toHaveCount(0);
    expect(await progress.innerText()).not.toMatch(/\d+\s*%/);
    const banner=await progress.boundingBox(),heading=await page.locator('.page-heading').boundingBox();
    expect(banner!.y).toBeLessThan(heading!.y);expect(banner!.y).toBeGreaterThanOrEqual(0);expect(banner!.y).toBeLessThan(280);
    await expect.poll(()=>page.evaluate(()=>window.scrollY)).toBe(0);
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth',viewport.width);
    await page.screenshot({path:resolve(output,`task-progress-${viewport.width}-contract.png`),fullPage:true,animations:'disabled'});
    current={...current,stage:'fetch',live:{...current.live,phase:'browser',detail:'[契约] 已观察到媒体响应，继续获取原片',waiting_on:'source',stage_elapsed_seconds:2,activity_age_seconds:0,metrics:{requests:3,downloaded_bytes:1048576,proxy_bytes:2097152,media_responses:1,complete_bound_media:1}}};
    await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(progress).toContainText('浏览器读取媒体');
    await expect(progress).toContainText('实际取回数据：1.0 MB');
    await expect(progress).toContainText('已收到来源响应：3 次');
    await expect(progress).toContainText('完整视频候选：1');
    await expect(progress).not.toContainText('视频下载量');
    await expect(progress).not.toContainText('生成百分比');
    current={...current,stage:'frames',live:{...current.live,phase:'frames',waiting_on:'local_media',metrics:{completed:2,total:4,sampled_frames:2,total_frames:4}}};
    await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(progress).toContainText('准备抽样画面：2 / 4');
    await expect(progress).toContainText('已准备抽样画面：2 / 4');
    current={...current,live:{...current.live,phase:'retry_wait',metrics:{retry_attempt:2,max_attempts:3}}};
    await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(progress).toContainText('本轮尝试：2 / 3');
    await expect(progress.locator('.task-progress-clock')).toContainText('已尝试 4 次');
    current={...current,stage:'fetch',live:{...current.live,phase:'browser'}};
    current={...current,status:'waiting_material',ended_at:new Date().toISOString(),error:{code:'douyin_fetch',message:'[契约] 公开来源暂未提供可用原片'},live:{...current.live,worker_active:false,waiting_on:'none',detail:'[契约] 本次原片获取未完成'}};
    await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(progress.getByRole('heading',{name:'来源获取未完成',exact:true})).toBeVisible();
    await expect(progress).toContainText('所在步骤：自动获取原片');
    await expect(progress.getByRole('heading',{name:'当前阶段：自动获取原片',exact:true})).toHaveCount(0);
    await page.getByRole('button',{name:'返回任务记录',exact:true}).click();
    await page.locator('.task-row').filter({hasText:queued.title}).click();
    await expect(progress).toContainText('任务正在排队');await expect(progress).toContainText('队列第');await expect(progress).toContainText('2');
    queued={...queued,status:'cancelled',ended_at:new Date().toISOString(),live:{...queued.live,worker_active:true,queue_position:undefined,elapsed_seconds:20,stage_elapsed_seconds:4,cancellation_wait_seconds:3,detail:'[契约] 取消已提交，等待在途请求结束',waiting_on:'none'}};
    await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(progress).toContainText('取消已请求，等待当前请求结束');
    const total=progress.locator('.task-progress-clock>span').filter({hasText:'任务历时'}).locator('strong');
    await expect(total).toHaveText('20秒');await expect(progress).toContainText('等待请求结束');
    await page.waitForTimeout(1100);await expect(total).toHaveText('20秒');
    queued={...queued,live:{...queued.live,worker_active:false,cancellation_wait_seconds:undefined}};
    await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(progress).toContainText('后台已停止处理本任务');await expect(progress).not.toContainText('等待请求结束');
    queued={...queued,status:'running',stage:'analysis',ended_at:undefined,live:undefined};
    await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
    await expect(progress).toContainText('暂未确认后台执行状态');
    expect(writes).toHaveLength(0);
  });
}

test('进度契约：列表失败不阻塞详情、读取不重叠、断连保留并恢复',async({page})=>{
  let current=running(),mode='good',listFails=false,reads=0,active=0,maxActive=0,failures=0;
  let release:()=>void=()=>{};let held:Promise<void>=Promise.resolve();const writes:Item[]=[];
  await mockWorkspace(page,()=>[current],async(route)=>{
    reads++;active++;maxActive=Math.max(maxActive,active);
    try{if(mode==='hold')await held;if(mode==='fail'){failures++;await route.fulfill({status:503,json:{error:{message:'[契约] 当前任务状态暂时不可读'}}});}else await route.fulfill({json:current});}finally{active--;}
  },writes);
  // Override only list reads; detail remains independently available.
  await page.route('**/api/tasks',route=>listFails?route.fulfill({status:503,json:{error:{message:'[契约] 任务列表刷新失败'}}}):route.fulfill({json:[current]}));
  await page.goto('/#tasks');await page.locator('.task-row').click();
  const progress=page.getByRole('region',{name:'当前任务进度',exact:true});await expect(progress).toContainText('最近成功刷新');
  mode='fail';listFails=true;await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
  await expect(progress.getByRole('alert')).toContainText('任务状态刷新失败');
  await expect(page.locator('.page-heading .badge')).toHaveText('进行中');
  await expect(progress).toContainText('保留上次结果');await expect(progress).toContainText('最近成功刷新');
  held=new Promise<void>(resolve=>{release=resolve;});mode='hold';
  await progress.getByRole('button',{name:'刷新状态',exact:true}).click();await expect.poll(()=>active).toBe(1);
  const heldReads=reads;await page.waitForTimeout(3200);expect(reads).toBe(heldReads);expect(maxActive).toBe(1);
  current={...current,stage:'vision',live:{...current.live,phase:'model_vision',detail:'[契约] 详情仍可刷新，等待视觉响应',activity_age_seconds:0}};mode='good';release();
  await expect(progress).toContainText('[契约] 详情仍可刷新，等待视觉响应');await expect(progress.getByRole('alert')).toHaveCount(0);
  await expect(progress).toContainText('状态连接正常');expect(failures).toBeGreaterThan(0);
  await page.getByRole('button',{name:'返回任务记录',exact:true}).click();
  await expect(page.locator('.task-list-connection')).toContainText('任务列表刷新失败');await expect(page.locator('.task-row')).toHaveCount(1);
  expect(writes).toHaveLength(0);
});

test('进度契约：换任务与同ID重开不会被迟到响应覆盖或停止轮询',async({page})=>{
  const a=running('late-a'),b={...running('late-b'),title:'[契约样例] 新选择的任务'};const writes:Item[]=[];
  let release:()=>void=()=>{};const held=new Promise<void>(resolve=>{release=resolve;});let bReads=0;
  await page.addInitScript(()=>{const original=window.fetch.bind(window);window.fetch=(input,init)=>String(input).endsWith('/api/tasks/late-a')?original(input,{...init,signal:undefined}):original(input,init);});
  await mockWorkspace(page,()=>[a,b],async(route,id)=>{if(id===a.id){await held;await route.fulfill({json:{...a,status:'succeeded',title:'[契约样例] 迟到的旧响应'}}).catch(()=>{});}else{bReads++;await route.fulfill({json:b});}},writes);
  await page.goto('/#tasks');await page.locator('.task-row').filter({hasText:a.title}).click();
  await expect(page.locator('.page-heading h1')).toHaveText(a.title);
  await page.getByRole('button',{name:'返回任务记录',exact:true}).click();await page.locator('.task-row').filter({hasText:b.title}).click();
  await expect(page.locator('.page-heading h1')).toHaveText(b.title);release();await page.waitForTimeout(300);
  await expect(page.locator('.page-heading h1')).toHaveText(b.title);
  const before=bReads;await page.getByRole('button',{name:'返回任务记录',exact:true}).click();await page.locator('.task-row').filter({hasText:b.title}).click();
  await expect.poll(()=>bReads).toBeGreaterThan(before);const reopened=bReads;
  await expect.poll(()=>bReads,{timeout:6000}).toBeGreaterThan(reopened);
  await expect(page.locator('.page-heading h1')).toHaveText(b.title);expect(writes).toHaveLength(0);
});

for(const withLive of [true,false])test(`进度契约：${withLive?'live':'旧接口无live'}状态读取超时可见，计时在断连时不倒退`,async({page})=>{
  let hold=false;let release:()=>void=()=>{};const held=new Promise<void>(resolve=>{release=resolve;});const current=running(),writes:Item[]=[];
  if(!withLive)delete current.live;
  let firstRelease:()=>void=()=>{};const firstRead=withLive?Promise.resolve():new Promise<void>(resolve=>{firstRelease=resolve;});
  await mockWorkspace(page,()=>[current],async(route)=>{await firstRead;if(hold)await held;await route.fulfill({json:current}).catch(()=>{});},writes);
  await page.goto('/#tasks');await page.locator('.task-row').click();
  const progress=page.getByRole('region',{name:'当前任务进度',exact:true});
  const total=progress.locator('.task-progress-clock>span').filter({hasText:'任务历时'}).locator('strong');
  const stage=progress.locator('.task-progress-clock>span').filter({hasText:'本步骤已用时'}).locator('strong');
  if(!withLive){await expect(total).toHaveText('—');await expect(stage).toHaveText('—');firstRelease();}
  await expect(progress).toContainText('状态连接正常');
  hold=true;await progress.getByRole('button',{name:'刷新状态',exact:true}).click();
  await page.waitForTimeout(8500);const before=await total.innerText();
  await expect(progress.getByRole('alert')).toContainText('读取任务状态超时',{timeout:5000});
  const seconds=(text:string)=>{const match=text.match(/(?:(\d+)分)?(\d+)秒/);return match?Number(match[1]||0)*60+Number(match[2]):0;};
  expect(seconds(await total.innerText())).toBeGreaterThanOrEqual(seconds(before));
  const frozen=await total.innerText(),stageFrozen=await stage.innerText();await page.waitForTimeout(1100);await expect(total).toHaveText(frozen);await expect(stage).toHaveText(stageFrozen);
  await expect(progress).toContainText('刷新失败不等于任务处理失败');
  await expect(page.locator('.page-heading .badge')).toHaveText('进行中');release();expect(writes).toHaveLength(0);
});
