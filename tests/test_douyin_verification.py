"""Official-view contracts with memory-only JPEG and user-action browser doubles.

No real browser, verification provider, SMS, account, or model is contacted.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = """import vm from 'node:vm'; import {verificationView,focusedEditable,visibleVerification} from './frontend/scripts/douyin-login.mjs';
let active=true,now=0,sequence=0,screenshots=0,pixels=1;
const emitted=[],typed=[],keys=[],clicks=[];
let element={tagName:'INPUT',type:'text',disabled:false,readOnly:false};
const focusedFrame={evaluate:async fn=>vm.runInNewContext('('+fn.toString()+')()',{document:{activeElement:element,hasFocus:()=>false}})};
const page={url:()=> 'https://www.douyin.com/user/self',viewportSize:()=>({width:1280,height:800}),
getByText:pattern=>({first:()=>({isVisible:async()=>active&&pattern.test('为保障账号安全，请先完成身份验证')})}),
mainFrame:()=>focusedFrame,
screenshot:async options=>{if(options.type!=='jpeg'||options.quality!==80||options.scale!=='css'||options.fullPage!==false||'path'in options)throw Error('unsafe screenshot');screenshots++;return Buffer.from([255,216,pixels,255,217]);},
mouse:{click:async(x,y)=>clicks.push([x,y])},keyboard:{insertText:async text=>typed.push(text),press:async key=>keys.push(key)}};
const view=verificationView(message=>emitted.push(message),{clock:()=>now,frameId:()=> 'synthetic-frame-'+(++sequence)});
"""


def node(script, *args):
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', PREFIX + script, *map(str, args)],
        cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    return json.loads(result.stdout)


def test_verification_view_is_viewport_only_bounded_low_frequency_and_force_refreshable():
    result = node("""await view.tick(page,true);now=1000;await view.tick(page,true);const early=screenshots;
now=5000;await view.tick(page,true);const dedup=emitted.filter(x=>x.type==='verification_frame').length;
view.force();await view.tick(page,true);const frames=emitted.filter(x=>x.type==='verification_frame');
active=false;await view.tick(page,false);
console.log(JSON.stringify({early,dedup,frames:frames.length,distinct:frames[0].frame_id!==frames[1].frame_id,
shape:frames.every(x=>Object.keys(x).sort().join(',')==='frame_id,height,image_base64,type,width'&&x.width===1280&&x.height===800),
cleared:emitted.some(x=>x.type==='verification_clear')}));""")
    assert result == {'early': 1, 'dedup': 1, 'frames': 2, 'distinct': True, 'shape': True, 'cleared': True}


@pytest.mark.parametrize('action,element_change,accepted,error', [
    ({'kind': 'click', 'x': 10, 'y': 20}, {}, True, None),
    ({'kind': 'click', 'x': -1, 'y': 20}, {}, False, 'action_failed'),
    ({'kind': 'click', 'x': 1280, 'y': 20}, {}, False, 'action_failed'),
    ({'kind': 'click', 'x': 10.5, 'y': 20}, {}, False, 'action_failed'),
    ({'kind': 'click', 'x': 10, 'y': 20, 'selector': '#synthetic-send-sms'}, {}, False, 'action_failed'),
    ({'kind': 'text', 'text': 'synthetic-manual-text'}, {}, True, None),
    ({'kind': 'text', 'text': 'synthetic\ntext'}, {}, False, 'action_failed'),
    ({'kind': 'text', 'text': 'x' * 129}, {}, False, 'action_failed'),
    ({'kind': 'text', 'text': 'synthetic-manual-text'}, {'type': 'email'}, False, 'input_focus'),
    ({'kind': 'text', 'text': 'synthetic-manual-text'}, {'readOnly': True}, False, 'input_focus'),
    ({'kind': 'text', 'text': 'synthetic-manual-text'}, {'disabled': True}, False, 'input_focus'),
    ({'kind': 'key', 'key': 'Enter'}, {}, True, None),
    ({'kind': 'key', 'key': 'Tab'}, {}, True, None),
    ({'kind': 'key', 'key': 'Space'}, {}, True, None),
    ({'kind': 'key', 'key': 'Control+L'}, {}, False, 'action_failed'),
    ({'kind': 'key', 'key': 'Meta+L'}, {}, False, 'action_failed'),
    ({'kind': 'key', 'key': 'F5'}, {}, False, 'action_failed'),
    ({'kind': 'refresh'}, {}, True, None),
])
def test_only_current_frame_user_actions_are_dispatched_without_text_echo(action, element_change, accepted, error):
    result = node("""const config=JSON.parse(process.argv[1]);Object.assign(element,config.element);
await view.tick(page,true);const id=emitted.find(x=>x.type==='verification_frame').frame_id;
const ok=await view.act(page,{type:'verification_action',frame_id:id,...config.action});
await view.tick(page,true);const frames=emitted.filter(x=>x.type==='verification_frame');
const before=typed.length+keys.length+clicks.length;
await view.act(page,{type:'verification_action',frame_id:id,kind:'click',x:1,y:1});
console.log(JSON.stringify({ok,frames:frames.length,new_id:frames[1].frame_id!==id,
first_error:emitted.find(x=>x.type==='verification_error')?.code||null,
replay_no_action:before===typed.length+keys.length+clicks.length,
text_not_echoed:!JSON.stringify(emitted).includes('synthetic-manual-text'),calls:before}));""",
        json.dumps({'action': action, 'element': element_change}))
    assert result['ok'] is accepted and result['frames'] == 2 and result['new_id']
    # A successful action's first later error is the deliberately replayed id.
    assert result['first_error'] == (error or 'stale_frame')
    assert result['replay_no_action'] and result['text_not_echoed']
    assert result['calls'] == (1 if accepted and action['kind'] != 'refresh' else 0)


def test_verification_exit_or_foreign_page_never_accepts_a_stale_user_action():
    result = node("""await view.tick(page,true);const id=emitted[0].frame_id;active=false;
const ok=await view.act(page,{frame_id:id,kind:'click',x:1,y:1});await view.tick(page,false);
active=true;page.url=()=> 'https://contract.invalid/';await view.tick(page,true);
console.log(JSON.stringify({ok,clicks:clicks.length,frames:emitted.filter(x=>x.type==='verification_frame').length,
error:emitted.find(x=>x.type==='verification_error')?.code}));""")
    assert result == {'ok': False, 'clicks': 0, 'frames': 1, 'error': 'stale_frame'}


def test_verification_frame_budget_and_oversize_jpeg_are_enforced_without_repeated_errors():
    result = node("""for(let i=0;i<51;i++){now+=5000;pixels=i;await view.tick(page,true);}
const frames=emitted.filter(x=>x.type==='verification_frame').length;
const budget=emitted.filter(x=>x.type==='verification_error'&&x.code==='budget').length;
const other=[];const large=verificationView(x=>other.push(x));page.screenshot=async()=>Buffer.alloc(256*1024+1);await large.tick(page,true);
console.log(JSON.stringify({frames,budget,large_frames:other.filter(x=>x.type==='verification_frame').length,
large_error:other.find(x=>x.type==='verification_error')?.code}));""")
    assert result == {'frames': 48, 'budget': 1, 'large_frames': 0, 'large_error': 'budget'}


@pytest.mark.parametrize('tag,input_type,expected', [
    ('INPUT', 'text', True), ('INPUT', 'tel', True), ('INPUT', 'password', True), ('INPUT', 'number', True),
    ('TEXTAREA', '', True), ('INPUT', 'email', False), ('INPUT', 'button', False), ('DIV', '', False),
])
def test_text_requires_current_dom_editable_focus_even_when_native_window_is_not_os_foreground(tag, input_type, expected):
    result = node("""const c=JSON.parse(process.argv[1]);element.tagName=c.tag;element.type=c.input_type;
console.log(JSON.stringify({editable:await focusedEditable(page)}));""", json.dumps({'tag': tag, 'input_type': input_type}))
    assert result == {'editable': expected}


@pytest.mark.parametrize('phone_ack', [False, True])
def test_cookie_and_profile_never_finish_or_navigate_away_while_official_identity_verification_is_visible(phone_ack):
    script = """import {PassThrough} from 'node:stream';import {EventEmitter} from 'node:events';import {main} from './frontend/scripts/douyin-login.mjs';
const input=new PassThrough(),context=new EventEmitter();let gotos=0,loginClicks=0;
const cookies=[{name:'sessionid',value:'synthetic-cookie-only',domain:'.douyin.com',path:'/',expires:-1,secure:true,httpOnly:true,sameSite:'Lax'}];
context.routeWebSocket=async()=>{};context.route=async()=>{};context.cookies=async()=>cookies;context.storageState=async()=>({cookies,origins:[]});
context.newCDPSession=async()=>({send:async()=>{},on:()=>{}});
const image={count:async()=>0,isVisible:async()=>false,screenshot:async()=>{throw Error('must not forward QR during verification');}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:()=>({first:()=>({isVisible:async()=>process.argv[1]==='true'})})};
const page=new EventEmitter();page.url=()=> 'https://www.douyin.com/user/self';page.locator=s=>s==='#douyin_login_comp_scan_code'?panel:image;
const root={isVisible:async()=>true,evaluate:async()=>true};
page.getByText=()=>({first:()=>({isVisible:async()=>true,elementHandle:async()=>({evaluateHandle:async()=>({asElement:()=>root}),dispose:async()=>{}})})});page.viewportSize=()=>({width:1280,height:800});
page.getByRole=()=>({isVisible:async()=>true,click:async()=>{loginClicks++;}});
page.screenshot=async()=>Buffer.from([255,216,1,255,217]);
page.goto=async()=>{gotos++;queueMicrotask(()=>context.emit('response',{
url:()=> 'https://www.douyin.com/aweme/v1/web/user/profile/self/',status:()=>200,
allHeaders:async()=>({'content-type':'application/json'}),body:async()=>Buffer.from(JSON.stringify({status_code:0,user:{uid:'123456'}}))}));};
context.newPage=async()=>page;const chromium={launch:async()=>({newContext:async()=>context,close:async()=>{}})};
const execution=main({chromium,input,uiReadyWaitMs:0});input.write(JSON.stringify({type:'start',mode:'login',guard_proxy:'http://127.0.0.1:12345',timeout_ms:500})+'\\n');
await execution;console.log(JSON.stringify({gotos,loginClicks}));
"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, str(phone_ack).lower()],
        cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    outcome = next(line for line in lines if line.get('type') == 'result')
    assert not outcome['authenticated'] and outcome['error']['code'] == 'douyin_session_verification_required'
    assert any(line.get('type') == 'verification_frame' for line in lines)
    assert not any(line.get('type') == 'qr' or line.get('phase') == 'waiting_confirmation' for line in lines)
    assert lines[-1] == {'gotos': 1, 'loginClicks': 0}


def test_post_verification_rechecks_once_even_when_earlier_proof_request_was_used_and_race_response_arrived_early():
    script = """import {PassThrough} from 'node:stream';import {EventEmitter} from 'node:events';import {main} from './frontend/scripts/douyin-login.mjs';
const input=new PassThrough(),context=new EventEmitter();let gotos=0,active=false,navigatedDuringChallenge=false;
const cookies=[{name:'sessionid',value:'synthetic-only-cookie',domain:'.douyin.com',path:'/',expires:-1,secure:true,httpOnly:true,sameSite:'Lax'}];
context.routeWebSocket=async()=>{};context.route=async()=>{};context.cookies=async()=>cookies;context.storageState=async()=>({cookies,origins:[]});
context.newCDPSession=async()=>({send:async()=>{},on:()=>{}});
const proof=(owner=page)=>context.emit('response',{request:()=>({frame:()=>({page:()=>owner})}),url:()=> 'https://www.douyin.com/aweme/v1/web/user/profile/self/',status:()=>200,
allHeaders:async()=>({'content-type':'application/json'}),body:async()=>Buffer.from(JSON.stringify({status_code:0,user:{uid:'123456'}}))});
const image={count:async()=>1,isVisible:async()=>true,screenshot:async()=>Buffer.from([137,80,78,71,13,10,26,10,1])};
const panel={count:async()=>1,isVisible:async()=>true,getByText:()=>({first:()=>({isVisible:async()=>false})})};
const page=new EventEmitter();page.url=()=> 'https://www.douyin.com/user/self';page.locator=s=>s==='#douyin_login_comp_scan_code'?panel:image;
const root={isVisible:async()=>active,evaluate:async()=>true};page.evaluate=async()=>false;
page.getByText=()=>({first:()=>({isVisible:async()=>active,elementHandle:async()=>({evaluateHandle:async()=>({asElement:()=>root}),dispose:async()=>{}})})});page.viewportSize=()=>({width:1280,height:800});page.screenshot=async()=>Buffer.from([255,216,1,255,217]);
page.goto=async()=>{gotos++;if(active)navigatedDuringChallenge=true;if(gotos>=3)queueMicrotask(proof);};
context.newPage=async()=>{if(!context.primary){context.primary=page;return page;}const probe=Object.assign(new EventEmitter(),page);probe.goto=async()=>{gotos++;if(active)navigatedDuringChallenge=true;if(gotos>=3)queueMicrotask(()=>proof(probe));};return probe;};const chromium={launch:async()=>({newContext:async()=>context,close:async()=>{}})};
const execution=main({chromium,input,uiReadyWaitMs:0});input.write(JSON.stringify({type:'start',mode:'login',guard_proxy:'http://127.0.0.1:12345',timeout_ms:3000})+'\\n');
setTimeout(()=>{active=true;},250);setTimeout(()=>{active=false;proof();},600);
await execution;console.log(JSON.stringify({gotos,navigatedDuringChallenge}));
"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script],
        cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    outcome = next(line for line in lines if line.get('type') == 'result')
    assert outcome['authenticated'] and outcome['verification_method'] == 'official_profile_response'
    assert lines[-1] == {'gotos': 3, 'navigatedDuringChallenge': False}


@pytest.mark.parametrize('parent_tag,editable', [('IFRAME', True), ('BODY', False)])
def test_text_follows_only_the_actual_dom_focus_frame_chain(parent_tag, editable):
    result = node("""let disposed=0;
const child={evaluate:async fn=>vm.runInNewContext('('+fn.toString()+')()',{document:{activeElement:{tagName:'INPUT',type:'tel'},hasFocus:()=>false}})};
const parent={evaluate:async fn=>vm.runInNewContext('('+fn.toString()+')()',{document:{activeElement:{tagName:process.argv[1]}}}),
evaluateHandle:async()=>({asElement:()=>({contentFrame:async()=>child}),dispose:async()=>disposed++})};
page.mainFrame=()=>parent;page.frames=()=>[parent,child];
console.log(JSON.stringify({editable:await focusedEditable(page),disposed}));""", parent_tag)
    assert result == {'editable': editable, 'disposed': 1 if editable else 0}


@pytest.mark.parametrize('transition', ['verification_left', 'foreign_page'])
def test_capture_race_never_forwards_the_post_verification_or_nonofficial_page(transition):
    result = node("""page.screenshot=async()=>{
if(process.argv[1]==='verification_left')active=false;else page.url=()=> 'https://contract.invalid/';
return Buffer.from([255,216,1,255,217]);};
await view.tick(page,true);
console.log(JSON.stringify({frames:emitted.filter(x=>x.type==='verification_frame').length,
cleared:emitted.some(x=>x.type==='verification_clear'),errors:emitted.filter(x=>x.type==='verification_error').length}));""", transition)
    assert result == {'frames': 0, 'cleared': True, 'errors': 0}
