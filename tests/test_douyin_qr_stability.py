"""One-challenge QR contracts using in-memory, non-QR PNG and browser doubles."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def node(script, *args):
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and not result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


@pytest.mark.parametrize('case,expected', [
    ('visible', True), ('transparent', False), ('transparent_parent', False),
    ('hidden', False), ('display_none', False), ('covered', False),
    ('zero_size', False), ('child_hit', True),
])
def test_expiration_requires_real_opaque_unobscured_text_not_merely_is_visible(case, expected):
    report = node("""import vm from 'node:vm';import {visibleQrExpiry} from './frontend/scripts/douyin-login.mjs';
const c=process.argv[1],child={},foreign={};
const parent={nodeType:1,parentElement:null,style:{opacity:c==='transparent_parent'?'0':'1',visibility:'visible',display:'block'}};
const element={nodeType:1,parentElement:parent,style:{opacity:c==='transparent'?'0':'1',visibility:c==='hidden'?'hidden':'visible',display:c==='display_none'?'none':'block'},
getBoundingClientRect:()=>({left:10,top:10,width:c==='zero_size'?0:100,height:24}),contains:x=>x===child};
const text={isVisible:async()=>true,evaluate:async fn=>vm.runInNewContext('('+fn.toString()+')(element)',
{element,getComputedStyle:x=>x.style,document:{elementFromPoint:()=>c==='covered'?foreign:c==='child_hit'?child:element}})};
const panel={getByText:(label,opts)=>{if(label!=='二维码失效'||opts.exact!==true)throw Error('only exact official expiry label');return {all:async function(){return [this.first()]},first:()=>text};}};
console.log(JSON.stringify({expired:await visibleQrExpiry(panel)}));""", case)[0]
    assert report == {'expired': expected}


BROWSER = """import {PassThrough} from 'node:stream';import {EventEmitter} from 'node:events';import {main} from './frontend/scripts/douyin-login.mjs';
let generations=0,clicks=0;const navigations=[];
const chromium={launch:async()=>{const generation=++generations,context=new EventEmitter();
context.route=async()=>{};context.routeWebSocket=async()=>{};context.cookies=async()=>[];
context.newCDPSession=async()=>({send:async()=>{},on:()=>{}});
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==','base64');png[29]=generation;
const image={count:async()=>1,isVisible:async()=>true,evaluate:async()=>true,getAttribute:async()=> 'data:image/png;base64,'+png.toString('base64'),screenshot:async()=>{throw Error('QR is never cropped');}};
const panel={count:async()=>1,isVisible:async()=>true,getByText:()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})})};
const page=new EventEmitter();let url='about:blank';page.url=()=>url;
page.goto=async target=>{navigations.push({generation,target});url='https://www.douyin.com/jingxuan';};
page.getByText=()=>({all:async function(){return [this.first()]},first:()=>({isVisible:async()=>false})});page.getByRole=()=>({isVisible:async()=>true,click:async()=>clicks++});
page.locator=selector=>selector==='#douyin_login_comp_scan_code'?panel:image;context.newPage=async()=>page;
return {newContext:async()=>context,close:async()=>{}};}};
async function login(timeout,checks=[]){const input=new PassThrough();const execution=main({chromium,input,uiReadyWaitMs:0});
input.write(JSON.stringify({type:'start',mode:'login',guard_proxy:'http://127.0.0.1:12345',timeout_ms:timeout})+'\\n');
const timers=checks.map(ms=>setTimeout(()=>input.write('{"type":"check"}\\n'),ms));await execution;timers.forEach(clearTimeout);}
"""


def test_multiple_ticks_and_active_checks_never_relay_same_code_again_or_reopen_challenge():
    messages = node(BROWSER + """await login(6200,[100,800,5100,5500]);
console.log(JSON.stringify({generations,clicks,navigations}));""")
    assert len([x for x in messages if x.get('type') == 'qr']) == 1
    assert not any(x.get('type') == 'qr_expired' for x in messages)
    assert messages[-1] == {'generations': 1, 'clicks': 0,
                           'navigations': [{'generation': 1, 'target': 'https://www.douyin.com/'}]}


def test_only_user_started_new_login_generation_can_relay_another_code():
    messages = node(BROWSER + """await login(300);await login(300);
console.log(JSON.stringify({generations,clicks,navigations}));""")
    images = [x['image_base64'] for x in messages if x.get('type') == 'qr']
    assert len(images) == 2 and images[0] != images[1]
    assert messages[-1] == {'generations': 2, 'clicks': 0, 'navigations': [
        {'generation': 1, 'target': 'https://www.douyin.com/'},
        {'generation': 2, 'target': 'https://www.douyin.com/'}]}
