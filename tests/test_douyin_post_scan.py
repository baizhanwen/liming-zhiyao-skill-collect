"""Explicit offline post-scan UI contracts. No account, browser or SMS calls."""
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


@pytest.mark.parametrize('label', ['短信验证', '接收短信验证码', '发送短信验证', '身份验证'])
def test_visible_later_sms_match_can_start_mfa_without_an_earlier_identity_heading(label):
    report = node("""import {visibleVerification,verificationTracker} from './frontend/scripts/douyin-login.mjs';
const label=process.argv[1],root={isVisible:async()=>true,evaluate:async()=>true};
const hidden={isVisible:async()=>false},shown={isVisible:async()=>true,
elementHandle:async()=>({evaluateHandle:async()=>({asElement:()=>root}),dispose:async()=>{}})};
const page={url:()=> 'https://www.douyin.com/jingxuan',getByText:pattern=>({
all:async()=>pattern.test(label)?[hidden,shown]:[]})};
const visible=await visibleVerification(page),state=await verificationTracker(page).inspect();
console.log(JSON.stringify({visible,state}));""", label)[0]
    assert report['visible'] and report['state']['active'] and report['state']['latched']
    assert not report['state']['canProve']


def test_hidden_scan_confirmation_copy_does_not_mask_visible_phone_confirmation():
    report = node("""import {visibleScanConfirmation} from './frontend/scripts/douyin-login.mjs';
const page={url:()=> 'https://www.douyin.com/jingxuan',locator:()=>({getByText:()=>({
all:async()=>[{isVisible:async()=>false},{isVisible:async()=>true}]})})};
console.log(JSON.stringify({confirmed:await visibleScanConfirmation(page)}));""")[0]
    assert report == {'confirmed': True}


@pytest.mark.parametrize('then_sms', [False, True], ids=['unknown-official-prompt', 'direct-sms-same-pixels'])
def test_check_acknowledges_unchanged_phase_and_qr_departure_shows_official_view_without_false_login(then_sms):
    reports = node("""import {PassThrough} from 'node:stream';import {EventEmitter} from 'node:events';
import {main} from './frontend/scripts/douyin-login.mjs';
const input=new PassThrough(),context=new EventEmitter();let qrVisible=true,sms=false,gotos=0,screenshots=0,typed=0;
const root={isVisible:async()=>true,evaluate:async()=>true};
const text=show=>({all:async()=>[{isVisible:async()=>show,
elementHandle:async()=>({evaluateHandle:async()=>({asElement:()=>root}),dispose:async()=>{}})}]});
const image={count:async()=>1,isVisible:async()=>qrVisible,evaluate:async()=>true,
getAttribute:async()=> 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=='};
const panel={count:async()=>1,isVisible:async()=>qrVisible,getByText:()=>({first:()=>({isVisible:async()=>false}),all:async()=>[]})};
const page=new EventEmitter();let url='about:blank';page.url=()=>url;
page.goto=async target=>{if(target!=='https://www.douyin.com/')throw Error('input page must never navigate');gotos++;url='https://www.douyin.com/jingxuan';};
page.getByText=pattern=>text(sms&&pattern.test('短信验证'));page.locator=s=>s==='#douyin_login_comp_scan_code'?panel:image;
page.getByRole=()=>({isVisible:async()=>false});page.viewportSize=()=>({width:1280,height:800});
page.screenshot=async()=>{screenshots++;if(screenshots===1&&process.argv[1]==='true')setTimeout(()=>{sms=true;},50);return Buffer.from([255,216,1,255,217]);};
page.mainFrame=()=>({evaluate:async()=> 'editable'});page.keyboard={insertText:async()=>typed++};
context.route=async()=>{};context.routeWebSocket=async()=>{};context.cookies=async()=>[];
context.newPage=async()=>page;context.newCDPSession=async()=>({send:async()=>{},on:()=>{}});
const chromium={launch:async()=>({newContext:async()=>context,close:async()=>{}})};
const execution=main({chromium,input,uiReadyWaitMs:0});
input.write(JSON.stringify({type:'start',mode:'login',guard_proxy:'http://127.0.0.1:12345',timeout_ms:5900})+'\\n');
const checks=[100,350,5300].map(at=>setTimeout(()=>input.write('{"type":"check"}\\n'),at));
setTimeout(()=>{qrVisible=false;},300);
await execution;checks.forEach(clearTimeout);console.log(JSON.stringify({gotos,screenshots,typed}));""", str(then_sms).lower())
    phases = [item.get('phase') for item in reports if item.get('type') == 'progress']
    assert phases.count('waiting_scan') >= 3  # even identical observations acknowledge active checks
    assert 'waiting_browser' in phases
    assert ('verification_required' in phases) is then_sms
    assert len([item for item in reports if item.get('type') == 'qr']) == 1
    assert any(item.get('type') == 'verification_frame' for item in reports)
    assert reports[-1]['gotos'] == 1 and reports[-1]['screenshots'] >= 1 and reports[-1]['typed'] == 0
    result = next(item for item in reports if item.get('type') == 'result')
    assert not result['authenticated'] and 'storage_state' not in result
    assert result['error']['code'] == ('douyin_session_verification_required' if then_sms else 'douyin_session_timeout')
    if then_sms:
        # The backend clears the old frame on a purpose change. Identical
        # pixels must still get a new frame so the official view is usable.
        transition = next(i for i, item in enumerate(reports) if item.get('phase') == 'verification_required')
        assert any(item.get('type') == 'verification_frame' for item in reports[transition + 1:])
