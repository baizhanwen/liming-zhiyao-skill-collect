"""Full-controller primary DOM failures with explicit in-memory browser mocks.

No real browser, account, Cookie, website, SMS, or model is used.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]

SCRIPT = """import {PassThrough} from 'node:stream';
import {EventEmitter} from 'node:events';
import {main} from './frontend/scripts/douyin-login.mjs';
const scenario=process.argv[1],input=new PassThrough(),context=new EventEmitter();
let primaryURL='about:blank',pages=0,proofResponses=0,proofBodyReads=0;
const navigations=[];
const cookies=[{name:'sessionid',value:'EXPLICIT_SYNTHETIC_COOKIE',domain:'.douyin.com',
path:'/',expires:-1,secure:true,httpOnly:true,sameSite:'Lax'}];
const primary=new EventEmitter();primary.url=()=>primaryURL;
primary.getByText=()=>({all:async()=>{
  if(scenario==='all_error')throw Error('EXPLICIT_PRIVATE_DOM_SENTINEL');
  return scenario==='item_error'?[{isVisible:async()=>{throw Error('EXPLICIT_PRIVATE_DOM_SENTINEL');}}]:[];
}});
primary.getByRole=()=>({isVisible:async()=>false});
primary.locator=()=>({count:async()=>0,isVisible:async()=>false,
getByText:()=>({all:async()=>[],first:()=>({isVisible:async()=>false})})});
primary.goto=async url=>{
  navigations.push(url);primaryURL='https://www.douyin.com/jingxuan';
  queueMicrotask(()=>{
    proofResponses++;
    context.emit('response',{
      request:()=>({frame:()=>({page:()=>primary})}),
      url:()=> 'https://www.douyin.com/aweme/v1/web/user/profile/self/',status:()=>200,
      allHeaders:async()=>({'content-type':'application/json'}),
      body:async()=>{proofBodyReads++;return Buffer.from(JSON.stringify({status_code:0,user:{uid:'123456'}}));}
    });
  });
};
context.route=async()=>{};context.routeWebSocket=async()=>{};
context.newCDPSession=async()=>({send:async()=>{},on:()=>{}});
context.cookies=async()=>{await new Promise(resolve=>setImmediate(resolve));return cookies;};
context.storageState=async()=>({cookies,origins:[]});
context.newPage=async()=>{pages++;if(pages!==1)throw Error('Unexpected proof page in this same-page fixture');return primary;};
const chromium={launch:async()=>({newContext:async()=>context,close:async()=>{}})};
const execution=main({chromium,input,uiReadyWaitMs:0});
input.write(JSON.stringify({type:'start',mode:'login',guard_proxy:'http://127.0.0.1:12345',timeout_ms:300})+'\\n');
await execution;
console.log(JSON.stringify({explicit_mock:true,pages,proofResponses,proofBodyReads,navigations}));
"""


def run_controller(scenario):
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', SCRIPT, scenario],
        cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and not result.stderr
    assert 'EXPLICIT_PRIVATE_DOM_SENTINEL' not in result.stdout
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert messages[-1] == {'explicit_mock': True, 'pages': 1, 'proofResponses': 1,
        'proofBodyReads': 1, 'navigations': ['https://www.douyin.com/']}
    return next(message for message in messages if message.get('type') == 'result')


@pytest.mark.parametrize('scenario', ['all_error', 'item_error'])
def test_unknown_primary_dom_cannot_save_same_page_official_proof_and_cookie(scenario):
    outcome = run_controller(scenario)
    assert outcome['ok'] is False and outcome['authenticated'] is False
    assert outcome['error']['code'] == 'douyin_session_timeout'
    assert 'storage_state' not in outcome


def test_same_fixture_can_save_when_primary_dom_is_reliably_without_challenge():
    outcome = run_controller('reliable_clear')
    assert outcome['ok'] is True and outcome['authenticated'] is True
    assert outcome['verification_method'] == 'official_profile_response'
    assert outcome['storage_state']['cookies'][0]['value'] == 'EXPLICIT_SYNTHETIC_COOKIE'
