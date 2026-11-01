"""Offline modal lifecycle and original-PNG contracts; no website or SMS calls."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(script, *args):
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=12)
    assert result.returncode == 0 and not result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_modal_handle_survives_sms_title_and_all_view_input_guards_share_it():
    result = run("""import {verificationTracker,verificationView} from './frontend/scripts/douyin-login.mjs';
let title=true,rootVisible=true,now=0,typed=0,n=0;const messages=[];
const root={isVisible:async()=>rootVisible,evaluate:async()=>true};
const page={url:()=> 'https://www.douyin.com/user/self',getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>title})}),
viewportSize:()=>({width:1280,height:800}),screenshot:async()=>Buffer.from([255,216,1,255,217]),
mainFrame:()=>({evaluate:async()=> 'editable'}),keyboard:{insertText:async()=>typed++}};
const tracker=verificationTracker(page,{clock:()=>now,locate:async()=>root,blocked:async()=>false});
const view=verificationView(x=>messages.push(x),{visible:()=>tracker.visible(),clock:()=>now,frameId:()=>String(++n)});
const initial=await tracker.inspect();await view.tick(page,true);
title=false;now=1000;const sms=await tracker.inspect();
const ok=await view.act(page,{frame_id:'1',kind:'text',text:'synthetic-user-only'});await view.tick(page,true);
rootVisible=false;now=2000;const gone=await tracker.inspect();await view.tick(page,true);now=2500;const proofGate=await tracker.inspect();
console.log(JSON.stringify({initial,sms,ok,typed,gone,proofGate,frames:messages.filter(x=>x.type==='verification_frame').length,
clear:messages.some(x=>x.type==='verification_clear'),no_input_echo:!JSON.stringify(messages).includes('synthetic-user-only')}));""")[0]
    assert result['initial']['active'] and result['sms']['active'] and result['sms']['latched']
    assert not result['sms']['canProve'] and result['ok'] and result['typed'] == 1
    assert result['frames'] == 2 and result['clear'] and result['no_input_echo']
    assert not result['gone']['canProve'] and result['proofGate']['canProve']


@pytest.mark.parametrize('missing_root,replacement', [(True, False), (False, True)])
def test_unknown_or_replaced_modal_stays_latched_and_cannot_prove_exit(missing_root, replacement):
    result = run("""import {verificationTracker} from './frontend/scripts/douyin-login.mjs';
const c=JSON.parse(process.argv[1]);let title=true,visible=true,now=0;
const root={isVisible:async()=>visible,evaluate:async()=>true};
const page={url:()=> 'https://www.douyin.com/user/self',getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>title})})};
const tracker=verificationTracker(page,{clock:()=>now,locate:async()=>c.missing?null:root,blocked:async()=>c.replacement});
await tracker.inspect();title=false;visible=false;now=100000;
const a=await tracker.inspect();now+=100000;const b=await tracker.inspect();
console.log(JSON.stringify({a,b}));""", json.dumps({'missing': missing_root, 'replacement': replacement}))[0]
    assert all(result[key]['latched'] and not result[key]['active'] and not result[key]['canProve'] for key in ('a', 'b'))


@pytest.mark.parametrize('covered,bad_source', [(True, False), (False, True), (False, False)])
def test_qr_uses_original_data_png_never_overlay_screenshot_and_clears_if_covered(covered, bad_source):
    result = run("""import {qrRelay} from './frontend/scripts/douyin-login.mjs';
const c=JSON.parse(process.argv[1]);let now=0,covered=false,screenshots=0,reads=0;const messages=[];
const png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';
const image={count:async()=>1,isVisible:async()=>true,evaluate:async()=>!covered,
getAttribute:async()=>{reads++;return c.bad?'https://www.douyin.com/unknown.png':'data:image/png;base64,'+png;},
screenshot:async()=>{screenshots++;throw Error('SMS overlay must never be screenshotted as QR');}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})})};
const page={url:()=> 'https://www.douyin.com/user/self',locator:s=>s==='#douyin_login_comp_scan_code'?panel:image};
const relay=qrRelay(x=>messages.push(x),{clock:()=>now});await relay.tick(page);
covered=c.covered;now=5000;await relay.tick(page);
console.log(JSON.stringify({screenshots,reads,qr:messages.filter(x=>x.type==='qr').length,
original:messages.filter(x=>x.type==='qr').every(x=>x.image_base64===png),clear:messages.some(x=>x.type==='qr_clear')}));""", json.dumps({'covered': covered, 'bad': bad_source}))[0]
    assert result['screenshots'] == 0 and result['original']
    assert result['qr'] == (0 if bad_source else 1)
    assert result['clear'] is (covered or bad_source)
    if covered:
        assert result['reads'] == 1  # no read of the occluded image


@pytest.mark.parametrize('scenario', ['sms', 'replacement', 'complete', 'old_delayed', 'probe_challenge', 'slow_probe', 'probe_timeout', 'probe_network', 'probe_blank', 'probe_dom_retry', 'probe_dom_closed', 'probe_visibility_error'])
def test_main_preserves_input_page_and_requires_fresh_independent_page_proof(scenario):
    lines = run("""import {PassThrough} from 'node:stream';import {EventEmitter} from 'node:events';import {main} from './frontend/scripts/douyin-login.mjs';
const scenario=process.argv[1],input=new PassThrough(),context=new EventEmitter();
let title=true,modal=true,replace=false,primaryGotos=0,probeGotos=0,pages=0,lateResolve;const began=Date.now();
const cookies=[{name:'sessionid',value:'synthetic-cookie-only',domain:'.douyin.com',path:'/',expires:-1,secure:true,httpOnly:true,sameSite:'Lax'}];
context.route=async()=>{};context.routeWebSocket=async()=>{};context.cookies=async()=>cookies;context.storageState=async()=>({cookies,origins:[]});
context.newCDPSession=async()=>({send:async()=>{},on:()=>{}});
const root={isVisible:async()=>modal,evaluate:async()=>true};
const locator={isVisible:async()=>title,elementHandle:async()=>({evaluateHandle:async()=>({asElement:()=>root}),dispose:async()=>{}})};
const primary=new EventEmitter();let primaryURL='about:blank';primary.url=()=>primaryURL;primary.getByText=()=>({all:async function(){return [this.first()]},first:()=>locator});
primary.evaluate=async()=>replace;primary.viewportSize=()=>({width:1280,height:800});primary.screenshot=async()=>Buffer.from([255,216,1,255,217]);
primary.locator=()=>({count:async()=>0,isVisible:async()=>false,getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})})});
primary.getByRole=()=>({isVisible:async()=>false});
const emit=(owner,delayed=false)=>context.emit('response',{request:()=>({frame:()=>({page:()=>owner})}),
url:()=> 'https://www.douyin.com/aweme/v1/web/user/profile/self/',status:()=>200,
allHeaders:async()=>({'content-type':'application/json'}),body:async()=>{
if(delayed)await new Promise(resolve=>{lateResolve=resolve;});
return Buffer.from(JSON.stringify({status_code:0,user:{uid:'123456'}}));}});
primary.goto=async url=>{if(url!=='https://www.douyin.com/')throw Error('login must start on the ordinary homepage');primaryURL='https://www.douyin.com/jingxuan';primaryGotos++;if(scenario==='old_delayed')emit(primary,true);};
context.newPage=async()=>{pages++;if(pages===1)return primary;
let probeChecks=0;let committed=!['slow_probe','probe_blank','probe_timeout','probe_network'].includes(scenario);const p=new EventEmitter();p.url=()=>committed?'https://www.douyin.com/user/self':'about:blank';p.getByText=()=>({all:async function(){if(scenario==='probe_dom_closed')throw Error('synthetic-DOM-collection-unavailable');return [this.first()]},first:()=>({isVisible:async()=>{if(scenario==='probe_visibility_error'||(scenario==='probe_dom_retry'&&probeChecks++===0))throw Error('synthetic-DOM-visibility-unavailable');return scenario==='probe_challenge';}})});p.close=async()=>{};
p.goto=async url=>{if(url!=='https://www.douyin.com/user/self')throw Error('proof must use official self page');probeGotos++;if(['probe_timeout','probe_network'].includes(scenario)){const error=new Error('synthetic-private-url-not-for-output');error.name=scenario==='probe_timeout'?'TimeoutError':'Error';throw error;}if(scenario==='slow_probe'){await new Promise(resolve=>setTimeout(resolve,450));committed=true;}if(['complete','probe_challenge','slow_probe','probe_dom_retry','probe_dom_closed','probe_visibility_error'].includes(scenario))queueMicrotask(()=>emit(p));if(scenario==='old_delayed')lateResolve?.();};return p;};
const chromium={launch:async()=>({newContext:async()=>context,close:async()=>{}})};
const execution=main({chromium,input,uiReadyWaitMs:0});input.write(JSON.stringify({type:'start',mode:'login',guard_proxy:'http://127.0.0.1:12345',timeout_ms:scenario==='probe_challenge'?6000:scenario==='slow_probe'?3000:1600})+'\\n');
setTimeout(()=>{title=false;},250); // real SMS substep has no original heading
if(scenario!=='sms')setTimeout(()=>{modal=false;replace=scenario==='replacement';},500);
await execution;console.log(JSON.stringify({primaryGotos,probeGotos,pages,elapsed:Date.now()-began}));
""", scenario)
    outcome = next(x for x in lines if x.get('type') == 'result')
    assert lines[-1]['primaryGotos'] == 1
    assert not any(x.get('type') == 'qr' or x.get('phase') in ('waiting_confirmation', 'checking') for x in lines)
    assert any(x.get('type') == 'verification_frame' for x in lines)
    if scenario in ('complete', 'slow_probe', 'probe_dom_retry'):
        assert outcome['authenticated'] and lines[-1]['probeGotos'] == 1
    else:
        expected = 'douyin_session_timeout' if scenario in ('old_delayed', 'probe_timeout', 'probe_dom_closed', 'probe_visibility_error') else 'douyin_session_runtime' if scenario in ('probe_network', 'probe_blank') else 'douyin_session_verification_required'
        assert not outcome['authenticated'] and outcome['error']['code'] == expected
        assert 'storage_state' not in outcome and 'synthetic-private-url' not in json.dumps(lines)
        assert lines[-1]['probeGotos'] == (0 if scenario in ('sms', 'replacement') else 1)
        if scenario == 'probe_challenge':
            assert lines[-1]['elapsed'] < 3000  # fixed failure, not six-second invisible wait


def test_actual_ancestor_selection_holds_the_same_fixed_modal_after_heading_changes():
    result = run("""import vm from 'node:vm';import {verificationTracker} from './frontend/scripts/douyin-login.mjs';
let title=true,rootVisible=true,disposed=0;
const modal={tagName:'DIV',nodeType:1,isConnected:true,parentElement:null,getBoundingClientRect:()=>({width:400,height:500}),style:{position:'fixed',opacity:'1'}};
const node={tagName:'SPAN',parentElement:modal,closest:()=>null};
const root={isVisible:async()=>rootVisible,evaluate:async fn=>vm.runInNewContext('('+fn.toString()+')(node)',{node:modal,getComputedStyle:x=>x.style})};
const source={dispose:async()=>disposed++,evaluateHandle:async fn=>{const found=vm.runInNewContext('('+fn.toString()+')(element)',{element:node,getComputedStyle:x=>x.style});
return {asElement:()=>found===modal?root:null,dispose:async()=>{}};}};
const page={url:()=> 'https://www.douyin.com/user/self',getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>title,elementHandle:async()=>source})})};
const tracker=verificationTracker(page);const a=await tracker.inspect();title=false;const b=await tracker.inspect();
console.log(JSON.stringify({a,b,disposed}));""")[0]
    assert result['a']['active'] and result['b']['active'] and result['disposed'] == 1


@pytest.mark.parametrize('hit', ['image', 'logo', 'sms_overlay'])
def test_actual_element_from_point_allows_only_exact_qr_container_hits(hit):
    result = run("""import vm from 'node:vm';import {qrRelay} from './frontend/scripts/douyin-login.mjs';
const kind=process.argv[1];let reads=0,screenshots=0;const messages=[];
const logo={},overlay={},container={contains:node=>node===logo};
const node={closest:selector=>selector==='#animate_qrcode_container'?container:null,
getBoundingClientRect:()=>({left:10,top:10,width:200,height:200})};
const image={count:async()=>1,isVisible:async()=>true,
evaluate:async fn=>vm.runInNewContext('('+fn.toString()+')(element)',{element:node,document:{elementFromPoint:()=>kind==='image'?node:kind==='logo'?logo:overlay}}),
getAttribute:async()=>{reads++;return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';},
screenshot:async()=>{screenshots++;throw Error('no pixel crop');}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})})};
const page={url:()=> 'https://www.douyin.com/user/self',locator:s=>s==='#douyin_login_comp_scan_code'?panel:image};
await qrRelay(x=>messages.push(x)).tick(page);
console.log(JSON.stringify({reads,screenshots,qr:messages.filter(x=>x.type==='qr').length,clear:messages.some(x=>x.type==='qr_clear')}));""", hit)[0]
    assert result == {'reads': 0 if hit == 'sms_overlay' else 1, 'screenshots': 0,
                      'qr': 0 if hit == 'sms_overlay' else 1, 'clear': hit == 'sms_overlay'}


@pytest.mark.parametrize('suffix', ['=garbage', ' ', '\n', '!'])
def test_qr_data_png_rejects_noncanonical_base64_without_echo(suffix):
    result = run("""import {qrPng} from './frontend/scripts/douyin-login.mjs';
const encoded='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';
console.log(JSON.stringify({rejected:qrPng(encoded+process.argv[1])===null}));""", suffix)[0]
    assert result == {'rejected': True}


@pytest.mark.parametrize('replacement', [False, True])
def test_replacement_dialog_containing_qr_cannot_masquerade_as_the_known_background(replacement):
    result = run("""import vm from 'node:vm';import {verificationTracker} from './frontend/scripts/douyin-login.mjs';
let title=true,visible=true,now=0,replaced=false;
const make=(semantic,position='fixed',zIndex='1000')=>({tagName:'DIV',nodeType:1,isConnected:true,parentElement:null,
style:{position,zIndex,opacity:'1',visibility:'visible',display:'block'},getBoundingClientRect:()=>({width:400,height:500}),
matches:()=>semantic,querySelector:()=>qr});
const bg=make(true),qr=make(false,'static','0'),other=make(true);qr.parentElement=bg;
const modal=make(true);const root={isVisible:async()=>visible,evaluate:async fn=>vm.runInNewContext('('+fn.toString()+')(node)',{node:modal,getComputedStyle:n=>n.style})};
const document={querySelector:()=>qr,querySelectorAll:()=>replaced?[qr,bg,other]:[qr,bg]};
const page={url:()=> 'https://www.douyin.com/user/self',getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>title})}),
evaluateHandle:async fn=>vm.runInNewContext('('+fn.toString()+')()',{document}),
evaluate:async(fn,arg)=>vm.runInNewContext('('+fn.toString()+')(arg)',{document,getComputedStyle:n=>n.style,arg})};
const tracker=verificationTracker(page,{clock:()=>now,locate:async()=>root});await tracker.inspect();
title=false;visible=false;replaced=process.argv[1]==='true';await tracker.inspect();now=1000;const state=await tracker.inspect();
console.log(JSON.stringify(state));""", str(replacement).lower())[0]
    assert result['latched'] and not result['active'] and result['canProve'] is not replacement
