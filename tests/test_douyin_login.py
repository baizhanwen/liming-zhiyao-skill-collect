"""Pure Node helper/IPC fixtures. Never open a browser or contact a website."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'frontend/scripts/douyin-login.mjs'
SELF = 'https://www.douyin.com/aweme/v1/web/user/profile/self/'


def helper(name, *args):
    command = ("import fs from 'node:fs'; import * as login from './frontend/scripts/douyin-login.mjs';"
               "const v=JSON.parse(fs.readFileSync(0,'utf8'));"
               "try { console.log(JSON.stringify({value:login[v.name](...v.args)})); }"
               "catch { console.log(JSON.stringify({error:'fixed'})); }")
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', command],
        input=json.dumps({'name': name, 'args': args}), text=True, capture_output=True, cwd=ROOT, timeout=10)
    assert result.returncode == 0 and not result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('url,status,payload,accepted', [
    (SELF, 200, {'status_code': 0, 'user': {'uid': '123456'}}, True),
    (SELF, 200, {'status_code': 0, 'user': {'uid': '0'}}, False),
    (SELF, 200, {'status_code': 0}, False),
    (SELF, 200, {'status_code': 0, 'user': {'uid': '123456', 'is_login': False}}, False),
    (SELF, 403, {'status_code': 0, 'user': {'uid': '123456'}}, False),
    (SELF.replace('https:', 'http:'), 200, {'status_code': 0, 'user': {'uid': '123456'}}, False),
    (SELF.replace('www.douyin.com', 'douyin.com.contract.invalid'), 200, {'status_code': 0, 'user': {'uid': '123456'}}, False),
    (SELF.replace('/self/', '/other/'), 200, {'status_code': 0, 'user': {'uid': '123456'}}, False),
    ('https://www.douyin.com/passport/web/user/info/', 200, {'message': 'success', 'data': {'user_id': '123456'}}, True),
    ('https://www.douyin.com/passport/web/user/info/', 200, {'message': 'success', 'data': {'user_id': '0'}}, False),
    ('https://www.douyin.com/passport/web/user/info/', 200, {'message': 'error', 'data': {'user_id': '123456'}}, False),
])
def test_authentication_requires_successful_official_self_response(url, status, payload, accepted):
    assert helper('officialAuthProof', url, status, payload) == {'value': accepted}


@pytest.mark.parametrize('name,domain,expires,accepted', [
    ('sessionid', '.douyin.com', -1, True), ('sid_tt', '.iesdouyin.com', 2000, True),
    ('sessionid_ss', 'www.douyin.com', 1, False), ('passport_csrf_token', '.douyin.com', -1, False),
    ('ttwid', '.douyin.com', -1, False), ('sessionid', '.contract.invalid', -1, False),
])
def test_auth_cookie_is_required_separately_and_visitor_cookie_is_insufficient(name, domain, expires, accepted):
    value = {'name': name, 'value': 'synthetic-session-only', 'domain': domain, 'expires': expires}
    assert helper('hasAuthCookie', [value], 1000) == {'value': accepted}


def test_exported_state_excludes_account_cache_foreign_sites_and_unknown_cookie_fields():
    state = {'cookies': [{'name': 'sessionid', 'value': 'synthetic-auth', 'domain': '.douyin.com', 'path': '/',
        'expires': -1, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax', 'account_cache': 'synthetic-private'},
        {'name': 'sessionid', 'value': 'synthetic-foreign', 'domain': '.contract.invalid'}],
        'origins': [{'origin': 'https://www.douyin.com', 'localStorage': [
            {'name': 'passport_csrf_token', 'value': 'synthetic-csrf'},
            {'name': 'userInfo', 'value': 'synthetic-private-account-cache'}]},
            {'origin': 'https://contract.invalid', 'localStorage': [{'name': 'ttwid', 'value': 'synthetic-foreign'}]}]}
    result = helper('minimalStorageState', state)['value']
    assert len(result['cookies']) == 1 and result['cookies'][0]['secure'] is True
    assert result['origins'] == [{'origin': 'https://www.douyin.com', 'localStorage': [{'name': 'passport_csrf_token', 'value': 'synthetic-csrf'}]}]
    assert 'synthetic-private' not in json.dumps(result) and 'synthetic-foreign' not in json.dumps(result)


def test_state_budget_is_rejected_without_echoing_values():
    state = {'cookies': [{'name': 'sessionid', 'value': 'synthetic-' + 'x' * 270000, 'domain': '.douyin.com'}], 'origins': []}
    assert helper('minimalStorageState', state) == {'error': 'fixed'}


@pytest.mark.parametrize('proxy', ['http://contract.invalid:443', 'https://127.0.0.1:443', 'http://user:synthetic@127.0.0.1:1'])
def test_invalid_proxy_protocol_fails_before_browser_launch(proxy):
    result = subprocess.run([shutil.which('node'), str(SCRIPT)], input=json.dumps({'type': 'start', 'mode': 'login',
        'guard_proxy': proxy, 'timeout_ms': 10}) + '\n', text=True, capture_output=True, timeout=10)
    assert not result.stderr
    assert json.loads(result.stdout) == {'type': 'result', 'ok': False, 'authenticated': False,
        'error': {'code': 'douyin_session_protocol'}}


def test_cancel_before_start_does_not_launch_a_browser_or_echo_input():
    result = subprocess.run([shutil.which('node'), str(SCRIPT)], input='{"type":"cancel"}\n',
        text=True, capture_output=True, timeout=10)
    assert not result.stderr
    assert json.loads(result.stdout) == {'type': 'result', 'ok': False, 'authenticated': False,
        'error': {'code': 'douyin_session_cancelled'}}


@pytest.mark.parametrize('mode,proof,cookie,authenticated,qr,denial,confirmation', [
    ('login', True, True, True, False, False, False), ('check', True, True, True, False, False, False),
    ('check', False, True, False, False, False, False), ('check', True, False, False, False, False, False),
    ('login', False, False, False, False, False, False), ('login', False, False, False, True, False, False),
    ('check', False, True, False, False, True, False), ('check', False, True, False, True, False, False),
    ('login', False, False, False, True, False, True),
])
def test_actual_login_main_with_explicit_mock_browser_requires_both_evidence_and_cookie(mode, proof, cookie, authenticated, qr, denial, confirmation):
    script = """import {PassThrough} from 'node:stream'; import {EventEmitter} from 'node:events';
import {main} from './frontend/scripts/douyin-login.mjs';
const config=JSON.parse(process.argv[1]); const input=new PassThrough(); const context=new EventEmitter();
let launchOptions,contextOptions,loginClicks=0,backgroundReads=0;const navigations=[];
const cookies=config.cookie?[{name:'sessionid',value:'synthetic-auth-cookie',domain:'.douyin.com',path:'/',expires:-1,httpOnly:true,secure:true,sameSite:'Lax'}]:[];
context.routeWebSocket=async()=>{}; context.route=async()=>{}; context.cookies=async()=>cookies;
context.storageState=async()=>({cookies,origins:[{origin:'https://www.douyin.com',localStorage:[{name:'userInfo',value:'synthetic-private-cache'}]}]});
context.newCDPSession=async()=>({send:async()=>{},on:()=>{}});
const page=new EventEmitter();page.currentURL='about:blank';page.url=function(){return this.currentURL;};page.goto=async function(url){
navigations.push({owner:this===page?'primary':'probe',url});this.currentURL=url==='https://www.douyin.com/'?'https://www.douyin.com/jingxuan':url;
queueMicrotask(()=>context.emit('response',{url:()=> 'https://www.douyin.com/synthetic/config',status:()=>200,
allHeaders:async()=>({'content-type':'application/json'}),body:async()=>{backgroundReads++;return Buffer.from(JSON.stringify({data:{captcha:{enabled:true},verify_data:{preload:true}}}));}}));
if(config.proof||config.denial)queueMicrotask(()=>context.emit('response',{
url:()=> 'https://www.douyin.com/aweme/v1/web/user/profile/self/', status:()=>200,
allHeaders:async()=>({'content-type':'application/json'}),body:async()=>Buffer.from(JSON.stringify(config.denial?{status_code:0,user:{uid:'0',is_login:false}}:{status_code:0,user:{uid:'123456'}}))}));};
page.getByText=label=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>typeof label==='string'&&label==='扫码登录'&&config.qr})});
const image={count:async()=>config.qr?1:0,isVisible:async()=>config.qr,evaluate:async()=>true,getAttribute:async()=> 'data:image/png;base64,'+Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==','base64').toString('base64'),screenshot:async()=>{throw Error('QR must not screenshot');}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:label=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>label instanceof RegExp&&config.confirmation})})};
page.locator=selector=>selector==='#douyin_login_comp_scan_code'?panel:image;
page.getByRole=(role,options)=>({all:async function(){return [this.first()]},first:()=>image,isVisible:async()=>role==='button'&&options.name==='登录',click:async()=>{loginClicks++;}});
context.newPage=async()=>{if(!context.primary){context.primary=page;return page;}const probe=Object.assign(new EventEmitter(),page);probe.currentURL='about:blank';return probe;};
const chromium={launch:async options=>{launchOptions=options;return {newContext:async options=>{contextOptions=options;return context;},close:async()=>{}};}};
const execution=main({chromium,input,uiReadyWaitMs:0});input.write(JSON.stringify({type:'start',mode:config.mode,guard_proxy:'http://127.0.0.1:12345',timeout_ms:500})+'\\n');
await execution;console.log(JSON.stringify({mock_launch:launchOptions,mock_context:contextOptions,login_clicks:loginClicks,background_reads:backgroundReads,navigations}));
"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script,
        json.dumps({'mode': mode, 'proof': proof, 'cookie': cookie, 'qr': qr, 'denial': denial, 'confirmation': confirmation})], cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    outcome = next(line for line in lines if line.get('type') == 'result')
    assert outcome['ok'] is authenticated and outcome['authenticated'] is authenticated
    if authenticated:
        assert outcome['verification_method'] == 'official_profile_response'
        assert 'synthetic-private-cache' not in json.dumps(outcome)
    else:
        assert outcome['error']['code'] == ('douyin_session_not_authenticated' if mode == 'check' and denial else 'douyin_session_timeout') and 'storage_state' not in outcome
    launch = lines[-1]['mock_launch']
    assert launch['headless'] is (mode == 'check') and launch['chromiumSandbox'] is True
    assert launch['proxy'] == {'server': 'http://127.0.0.1:12345', 'bypass': '<-loopback>'}
    assert 'storageState' not in lines[-1]['mock_context']
    assert lines[-1]['login_clicks'] == (1 if mode == 'login' and not proof and not qr else 0)
    assert lines[-1]['background_reads'] == 0
    assert [v['url'] for v in lines[-1]['navigations'] if v['owner'] == 'primary'] == [
        'https://www.douyin.com/' if mode == 'login' else 'https://www.douyin.com/user/self']
    assert all(v['url'] == 'https://www.douyin.com/user/self' for v in lines[-1]['navigations'] if v['owner'] == 'probe')
    assert not any(line.get('phase') == 'verification_required' for line in lines)
    qr_messages = [line for line in lines if line.get('type') == 'qr']
    assert bool(qr_messages) is (mode == 'login' and qr and not authenticated and not confirmation)
    assert any(line.get('phase') == 'waiting_confirmation' for line in lines) is confirmation
    if confirmation:
        assert any(line.get('type') == 'qr_clear' for line in lines) and not outcome['authenticated']


@pytest.mark.parametrize('url,status,payload,denied', [
    (SELF, 200, {'status_code': 0, 'user': {'is_login': False}}, True),
    (SELF, 401, {'message': 'not authenticated'}, True),
    (SELF, 403, {'data': {'captcha': {'preloaded': True}}}, False),
    (SELF, 200, {'data': {'verify_data': {'preloaded': True}}}, False),
    (SELF.replace('/self/', '/other/'), 401, {'message': 'not authenticated'}, False),
    (SELF, 200, {'status_code': 0, 'user': {'uid': '0'}}, False),
])
def test_only_explicit_official_denial_can_mark_session_not_authenticated(url, status, payload, denied):
    assert helper('officialAuthDenial', url, status, payload) == {'value': denied}


def test_qr_relay_keeps_one_original_png_per_login_generation_and_never_resets_periodically():
    script = """import {qrRelay} from './frontend/scripts/douyin-login.mjs';
let now=0,reads=0,exact=true;
let png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==','base64'); const sent=[];
const image={count:async()=>1,isVisible:async()=>true,evaluate:async()=>true,getAttribute:async name=>{reads++;exact&&=name==='src';return 'data:image/png;base64,'+png.toString('base64');},screenshot:async()=>{throw Error('no screenshot');}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})})};
const page={url:()=> 'https://www.douyin.com/jingxuan',locator:selector=>{
if(selector==='#douyin_login_comp_scan_code')return panel;
exact&&=selector==='#douyin_login_comp_scan_code #animate_qrcode_container img[aria-label="二维码"]';return image;}};
const relay=qrRelay(message=>sent.push(message),{clock:()=>now});
await relay.tick(page);now=4999;await relay.tick(page);const beforeInterval=reads;
for(let i=0;i<30;i++){now+=5000;await relay.tick(page);}
const stable=sent.filter(x=>x.type==='qr').length;
png=Buffer.from(png);png[29]=1;now+=5000;await relay.tick(page);
const changed=sent.filter(x=>x.type==='qr').length;
const expired=sent.filter(x=>x.type==='qr_expired').length;
const cleared=sent.filter(x=>x.type==='qr_clear').length;
// A new relay exists only in a new user-started login process/generation.
const newGeneration=[];await qrRelay(x=>newGeneration.push(x),{clock:()=>now}).tick(page);
const bad=[];png=Buffer.alloc(65537);await qrRelay(x=>bad.push(x),{clock:()=>now}).tick(page);
console.log(JSON.stringify({beforeInterval,stable,changed,expired,cleared,has_reset:'reset'in relay,exact,
newGeneration:newGeneration.filter(x=>x.type==='qr').length,badQr:bad.filter(x=>x.type==='qr').length,
safe_fields:sent.filter(x=>x.type==='qr').every(v=>Object.keys(v).sort().join(',')==='image_base64,type')}));
"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script],
        cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout) == {'beforeInterval': 1, 'stable': 1, 'changed': 1,
        'expired': 1, 'cleared': 1, 'has_reset': False, 'exact': True,
        'newGeneration': 1, 'badQr': 0, 'safe_fields': True}


def test_expired_official_qr_is_cleared_and_waits_for_user_restart_with_zero_refresh_clicks():
    script = """import {qrRelay} from './frontend/scripts/douyin-login.mjs';
let now=0,expired=false,refreshes=0,reads=0;const messages=[];
const image={count:async()=>1,isVisible:async()=>true,evaluate:async()=>true,getAttribute:async()=>{reads++;if(expired)throw Error('must not read expired QR');return 'data:image/png;base64,'+Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==','base64').toString('base64');},screenshot:async()=>{throw Error('no screenshot');}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:label=>({all:async function(){return [this.first()]},first:()=>({
isVisible:async()=>expired&&label==='二维码失效',evaluate:async()=>expired,
click:async()=>{refreshes++;throw Error('must never click an official refresh control');}})})};
const page={url:()=> 'https://www.douyin.com/jingxuan',locator:selector=>selector==='#douyin_login_comp_scan_code'?panel:image};
const relay=qrRelay(message=>messages.push(message),{clock:()=>now});
await relay.tick(page);expired=true;
for(let i=0;i<25;i++){now+=5000;await relay.tick(page);}
expired=false;now+=5000;await relay.tick(page);
console.log(JSON.stringify({refreshes,reads,qr:messages.filter(x=>x.type==='qr').length,
clear:messages.filter(x=>x.type==='qr_clear').length,expired:messages.filter(x=>x.type==='qr_expired').length,
metadataOnly:messages.filter(x=>x.type!=='qr').every(x=>Object.keys(x).join(',')==='type')}));
"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script],
        cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout) == {'refreshes': 0, 'reads': 1,
        'qr': 1, 'clear': 1, 'expired': 1, 'metadataOnly': True}


@pytest.mark.parametrize('scenario,gotos,authenticated', [
    ('no_auth', 1, False), ('retain_proof', 1, True), ('cookie_then_proof', 2, True),
])
def test_repeated_active_checks_preserve_one_challenge_and_only_cookie_can_trigger_one_proof_navigation(scenario, gotos, authenticated):
    script = """import {PassThrough} from 'node:stream';import {EventEmitter} from 'node:events';import {main} from './frontend/scripts/douyin-login.mjs';
const scenario=process.argv[1],input=new PassThrough(),context=new EventEmitter();let gotos=0,cookieReady=false;
const cookies=()=>cookieReady?[{name:'sessionid',value:'synthetic-confirmed-cookie',domain:'.douyin.com',path:'/',expires:-1,secure:true,httpOnly:true,sameSite:'Lax'}]:[];
context.routeWebSocket=async()=>{};context.route=async()=>{};context.cookies=async()=>cookies();context.storageState=async()=>({cookies:cookies(),origins:[]});
context.newCDPSession=async()=>({send:async()=>{},on:()=>{}});
const image={count:async()=>1,isVisible:async()=>true,evaluate:async()=>true,getAttribute:async()=> 'data:image/png;base64,'+Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==','base64').toString('base64'),screenshot:async()=>{throw Error('QR must not screenshot');}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})})};
const page=new EventEmitter();page.url=()=> 'https://www.douyin.com/user/self';page.locator=selector=>selector==='#douyin_login_comp_scan_code'?panel:image;
page.getByText=()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})});page.getByRole=()=>{throw Error('must not touch global tutorial image or reopen login');};
page.goto=async()=>{gotos++;if(scenario==='retain_proof'||(scenario==='cookie_then_proof'&&gotos===2))queueMicrotask(()=>context.emit('response',{
url:()=> 'https://www.douyin.com/aweme/v1/web/user/profile/self/',status:()=>200,allHeaders:async()=>({'content-type':'application/json'}),
body:async()=>Buffer.from(JSON.stringify({status_code:0,user:{uid:'123456'}}))}));};
context.newPage=async()=>{if(!context.primary){context.primary=page;return page;}const probe=Object.assign(new EventEmitter(),page);probe.currentURL='about:blank';return probe;};const chromium={launch:async()=>({newContext:async()=>context,close:async()=>{}})};
const execution=main({chromium,input,uiReadyWaitMs:0});input.write(JSON.stringify({type:'start',mode:'login',guard_proxy:'http://127.0.0.1:12345',timeout_ms:1100})+'\\n');
for(const at of [50,250,450])setTimeout(()=>input.write('{"type":"check"}\\n'),at);
if(scenario!=='no_auth')setTimeout(()=>{cookieReady=true;},500);
await execution;console.log(JSON.stringify({gotos}));
"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, scenario],
        cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[-1] == {'gotos': gotos}
    outcome = next(line for line in lines if line.get('type') == 'result')
    assert outcome['authenticated'] is authenticated
    if authenticated:
        assert outcome['verification_method'] == 'official_profile_response'
    else:
        assert outcome['error']['code'] == 'douyin_session_timeout'


@pytest.mark.parametrize('candidate_count', [0, 2])
def test_qr_selection_fails_closed_when_actual_challenge_image_is_missing_or_ambiguous(candidate_count):
    script = """import {qrRelay} from './frontend/scripts/douyin-login.mjs';
let screenshots=0,messages=0;
const image={count:async()=>Number(process.argv[1]),isVisible:async()=>true,screenshot:async()=>{screenshots++;}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})})};
const page={url:()=> 'https://www.douyin.com/user/self',locator:selector=>selector==='#douyin_login_comp_scan_code'?panel:image,
getByRole:()=>{throw Error('must not fall back to first tutorial image');}};
await qrRelay(()=>messages++).tick(page);console.log(JSON.stringify({screenshots,messages}));
"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, str(candidate_count)],
        cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout) == {'screenshots': 0, 'messages': 1}
