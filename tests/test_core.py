"""Explicit synthetic service contracts, local real SQLite/ZIP/FFmpeg/API flows."""
import base64
import io
import json
import shutil
import subprocess
import threading
import time
import zipfile
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.media import inspect_video, prepare_media, subtitle_source
from backend.models import ModelService,normalize_generated_skill
from backend.security import AppError, Vault, public_url
from backend.skills import SkillService, unpack
from backend.store import Store
from backend.tasks import TaskRunner


ANALYSIS={'overview':'[契约样例] 测试资料流程','purpose':'契约验证','audience':'测试维护者','steps':['读取材料','核对证据'],'dependencies':['Python'],'limitations':['测试服务，不代表真实模型理解'],'methods':['保留来源'],'claims':[{'text':'字幕提到核对证据','evidence_ids':['sub-1'],'kind':'source_claim'}]}
SUB=b'1\n00:00:00,000 --> 00:00:02,000\nRead the source and verify evidence.\n'


def mock_model(request):
    if request.url.path.endswith('/models'):
        return httpx.Response(200,json={'data':[{'id':'contract-model'}]})
    if request.url.path.endswith('/audio/transcriptions'):
        assert b'RIFF' in request.content
        return httpx.Response(200,json={'text':'[契约服务] audio received'})
    payload=json.loads(request.content)
    prompt=json.dumps(payload,ensure_ascii=False)
    if 'image_url' in prompt:
        content='[契约服务] 画面时间 0.25s 测试色块，未验证模型准确率'
    elif 'not_applicable' in prompt:
        content=json.dumps({'name':'contract-draft','description':'显式契约草稿','body':'---\nname: contract-draft\n---\n# 测试技能\n触发：测试。\n输入：资料。\n前置：授权。\n步骤：核对来源。\n输出：卡片。\n验收：人工核对。\n失败：保留材料。\n来源：测试。\n未执行。'},ensure_ascii=False)
    elif 'evidence_ids' in prompt and 'question' in prompt:
        content=json.dumps({'answer':'[契约回答] 请核对证据','evidence_ids':['sub-1']},ensure_ascii=False)
    elif 'output_schema' in prompt:
        output=dict(ANALYSIS)
        data=json.loads(payload['messages'][1]['content'])
        ev=data['source']['evidence']
        output['claims']=[{'text':'来源内容摘要（契约输出）','evidence_ids':[ev[0]['id']],'kind':'source_claim'}]
        content=json.dumps(output,ensure_ascii=False)
    else:
        content='OK'
    return httpx.Response(200,json={'model':'contract-model','choices':[{'message':{'content':content}}],'usage':{'total_tokens':42}})


def configured(tmp_path,handler=mock_model):
    store=Store(tmp_path);vault=Vault(tmp_path)
    for cap in ('text','vision','transcription'):
        store.put('model_configs',{'id':cap,'base_url':'https://contract.invalid/v1','model':'contract-model'})
    return store,vault,ModelService(store,vault,httpx.Client(transport=httpx.MockTransport(handler)))


def test_api_subtitle_to_knowledge_draft_zip_and_restart(tmp_path):
    app=create_app(tmp_path,start_worker=False,model_client=httpx.Client(transport=httpx.MockTransport(mock_model)))
    with TestClient(app) as c:
        assert c.get('/api/status').json()['counts']['skills']==0
        t=c.post('/api/uploads',files={'file':('fixture.srt',SUB,'text/plain')}).json()
        app.state.runner.run(t['id'])
        t=c.get('/api/tasks/'+t['id']).json()
        assert t['status']=='waiting_config' and t['source']['basis']=='subtitles' and not t.get('analysis')
        assert c.put('/api/settings',json={'models':{'text':{'base_url':'https://contract.invalid/v1','model':'contract-model','api_key':'synthetic-contract-secret'}}}).status_code==200
        assert 'synthetic-contract-secret' not in c.get('/api/settings').text
        c.post('/api/tasks/'+t['id']+'/retry');app.state.runner.run(t['id'])
        result=c.get('/api/tasks/'+t['id']).json()
        assert result['status']=='succeeded'
        assert result['analysis']['basis']=='subtitles' and result['analysis']['validation']=='not_executed'
        assert result['analysis']['usage']['total_tokens']==42
        k=c.get('/api/knowledge?q=核对').json()[0]
        assert k['source_id']==t['id'] and k['evidence'][0]['time'].startswith('00:00')
        c.put('/api/knowledge/'+k['id'],json={'body':'用户已经编辑'})
        c.post('/api/tasks/'+t['id']+'/retry');app.state.runner.run(t['id'])
        assert c.get('/api/knowledge').json()[0]['body']=='用户已经编辑'
        answer=c.post('/api/tasks/'+t['id']+'/ask',json={'question':'如何核对？'}).json()
        assert answer['evidence_ids']==['sub-1']
        skill=c.post('/api/tasks/'+t['id']+'/skill').json()
        assert skill['status']=='draft'
        assert skill['generation']['model']=='contract-model' and skill['generation']['prompt_version']
        assert c.post('/api/tasks/'+t['id']+'/skill').json()['id']==skill['id']
        exported=c.get('/api/skills/'+skill['id']+'/export').content
        imp=c.post('/api/skills/import',files={'file':('skill.zip',exported,'application/zip')})
        assert imp.status_code==200 and imp.json()['body']==skill['body']
        assert imp.json()['content_hash']==skill['content_hash']
        updated=c.put('/api/skills/'+skill['id'],json={'body':skill['body']+'\n补充验收条件'}).json()
        assert updated['version']==2 and updated['content_hash']!=skill['content_hash']
        assert len(c.get('/api/skills/'+skill['id']+'/versions').json())==2
    app2=create_app(tmp_path,start_worker=False)
    with TestClient(app2) as c:
        assert c.get('/api/skills/'+skill['id']).json()['version']==2
        assert c.get('/api/tasks/'+t['id']).json()['status']=='succeeded'


@pytest.mark.parametrize('status,body,code,retryable',[(401,{},'authentication',False),(403,{},'permission',False),(402,{},'quota',False),(429,{'error':'insufficient_quota'},'quota',False),(429,{},'rate_limit',True),(404,{'error':'model_not_found'},'model',False),(404,{},'endpoint',False),(400,{},'protocol',False),(503,{},'upstream',True)])
def test_model_errors_are_classified_without_response_leak(tmp_path,status,body,code,retryable):
    def handler(req):return httpx.Response(status,json={**body,'echo':'SYNTHETIC_PRIVATE_ECHO'})
    _,_,models=configured(tmp_path,handler)
    with pytest.raises(AppError) as e:models.probe('text')
    assert e.value.code==code and e.value.retryable==retryable
    assert 'SYNTHETIC_PRIVATE_ECHO' not in e.value.message


def test_model_timeout_and_malformed_output_bounded(tmp_path):
    def timeout(req):raise httpx.ReadTimeout('synthetic-secret-echo',request=req)
    _,_,models=configured(tmp_path,timeout)
    with pytest.raises(AppError,match='超时'):models.probe('text')
    calls=[]
    def malformed(req):
        calls.append(1)
        return httpx.Response(200,json={'choices':[{'message':{'content':'bad json'}}]})
    _,_,models=configured(tmp_path,malformed)
    with pytest.raises(AppError) as e:models.analyze({'text':'test','evidence':[]})
    assert e.value.code=='output_format' and len(calls)==2


def test_capabilities_probe_independent(tmp_path):
    app=create_app(tmp_path,start_worker=False,model_client=httpx.Client(transport=httpx.MockTransport(mock_model)))
    with TestClient(app) as c:
        c.put('/api/settings',json={'models':{cap:{'base_url':'https://contract.invalid/v1','model':'contract-model'} for cap in ('text','vision','transcription')}})
        assert c.post('/api/models/text/test').json()['status']=='passed'
        settings=c.get('/api/settings').json()
        assert 'last_test' not in settings['models']['vision'] and 'last_test' not in settings['models']['transcription']
        assert c.post('/api/models/vision/test').json()['capability']=='vision'
        assert '静音' in c.post('/api/models/transcription/test').json()['note']
        assert c.get('/api/models/text/list').json()['models']==['contract-model']


def test_task_retry_reuses_acquisition_and_analysis_checkpoint(tmp_path):
    store,vault,models=configured(tmp_path)
    counts={'fetch':0,'model':0}
    class Git:
        def snapshot(self,*args):
            counts['fetch']+=1
            return {'title':'contract','text':'source','evidence':[{'id':'G1','excerpt':'source'}]}
    original=models.analyze
    def transient(source):
        counts['model']+=1
        if counts['model']==1:raise AppError('timeout','测试临时故障',True)
        return original(source)
    models.analyze=transient
    runner=TaskRunner(store,vault,models,Git)
    task=runner.create('https://github.com/example/repo')
    assert runner.create('https://github.com/example/repo')['id']==task['id']
    runner.run(task['id'])
    assert store.get('tasks',task['id'])['status']=='succeeded'
    assert counts=={'fetch':1,'model':2}
    assert len(store.list('knowledge'))==1


def test_task_final_failure_and_recovery(tmp_path):
    store,vault,models=configured(tmp_path)
    class Failing:
        def snapshot(self,*args):raise AppError('network','契约网络故障',True)
    runner=TaskRunner(store,vault,models,Failing)
    task=runner.create('https://github.com/example/fail');runner.run(task['id'])
    assert store.get('tasks',task['id'])['status']=='failed'
    assert store.get('tasks',task['id'])['attempts']==3
    cancelled=runner.create('https://github.com/example/cancel');runner.cancel(cancelled['id']);runner.run(cancelled['id'])
    assert store.get('tasks',cancelled['id'])['status']=='cancelled'
    pending=runner.create('https://github.com/example/recover')
    runner.checkpoint(pending['id'],status='running',stage='fetch',source={'text':'cached'})
    runner.recover()
    assert store.get('tasks',pending['id'])['status']=='queued'
    runner.checkpoint(pending['id'],status='running',stage='analysis')
    runner.recover()
    assert store.get('tasks',pending['id'])['status']=='waiting_config'
    assert store.get('tasks',pending['id'])['source']['text']=='cached'


def test_complete_package_binary_license_and_safe_import(tmp_path):
    service=SkillService(Store(tmp_path))
    skill=service.create({'name':'测试技能','body':'---\nname: test\n---\n# Test','files':[{'path':'scripts/tool.py','content':'print("test fixture only")'},{'path':'templates/data.bin','content':base64.b64encode(b'\x00\xff\x11').decode(),'encoding':'base64'},{'path':'LICENSE','content':'Test fixture license'}]})
    exported=service.export_zip(skill['id'])
    imported=service.import_zip(exported)
    assert imported['content_hash']==skill['content_hash'] and imported['status']=='draft'
    assert service.import_zip(exported)['id']==imported['id']
    s2=service.create({'name':'another','body':'# another'})
    assert service.import_zip(service.export_zip(s2['id']))['id']!=imported['id']
    raw,manifest=unpack(exported)
    assert raw['templates/data.bin']==b'\x00\xff\x11' and 'LICENSE' in raw
    invalid=io.BytesIO()
    with zipfile.ZipFile(invalid,'w') as z:
        z.writestr('../SKILL.md','malicious')
    with pytest.raises(AppError):service.import_zip(invalid.getvalue())


def test_api_private_boundary_validation_and_archiving(tmp_path):
    app=create_app(tmp_path,start_worker=False)
    with TestClient(app) as c:
        assert c.get('/api/status',headers={'Host':'evil.example'}).status_code==403
        assert c.put('/api/settings',json={},headers={'Origin':'https://evil.example'}).status_code==403
        assert c.put('/api/settings',json={'models':{'text':{'max_tokens':'bad'}}}).status_code==400
        assert c.put('/api/settings',json={'models':{'text':{'base_url':'http://remote.example/v1'}}}).status_code==400
        assert c.post('/api/uploads',files={'file':('bad.exe',b'data')}).status_code==400
        assert c.post('/api/uploads',files={'file':('empty.srt',b'')}).status_code==400
        s=c.post('/api/skills',json={'name':'档案','body':'# 测试知识','tags':['中文']}).json()
        assert c.get('/api/skills?q=知识&tag=中文').json()[0]['id']==s['id']
        assert c.put('/api/skills/'+s['id'],json={'status':'verified'}).status_code==400
        assert c.post('/api/skills/'+s['id']+'/verification',json={'result':'passed','evidence':''}).status_code==400
        assert c.post('/api/skills/'+s['id']+'/verification',json={'result':'passed','evidence':'[契约] 人工验证记录','notes':'仅测试状态保存'}).json()['status']=='verified'
        assert c.delete('/api/skills/'+s['id']).json()['archived']
        assert c.get('/api/skills').json()==[]
        assert len(c.get('/api/skills?archived=true').json())==1
        assert c.post('/api/feishu/test',json={}).json()['error']['code']=='waiting_config'


@pytest.mark.parametrize('url',['http://127.0.0.1','http://[::1]','http://169.254.169.254','http://10.0.0.1','file:///etc/passwd','http://user:secret@example.com','http://example.com:bad'])
def test_source_urls_reject_private_targets(url):
    with pytest.raises(AppError):public_url(url)


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg external dependency absent')
def test_real_ffmpeg_pipeline_and_contract_audio_vision(tmp_path):
    path=tmp_path/'fixed-fixture.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=c=blue:s=320x240:r=10','-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(path)],check=True)
    store,vault,models=configured(tmp_path/'app')
    runner=TaskRunner(store,vault,models,lambda:None)
    task=runner.create('',uploads=[{'path':str(path),'name':path.name}])
    runner.run(task['id']);result=store.get('tasks',task['id'])
    assert result['status']=='succeeded',result.get('error')
    assert result['analysis']['basis']=='transcript_and_frames'
    assert result['source']['evidence'][0]['time'].startswith('0')
    assert len(result['media']['frames'])==4 and len(result['transcripts'])==1
    assert result['vision_result']['model']=='contract-model'
    assert all(Path(f['path']).exists() for f in result['media']['frames'])


def test_subtitle_time_and_text_basis(tmp_path):
    p=tmp_path/'sample.vtt';p.write_text('WEBVTT\n\n00:00.000 --> 00:01.500\nTest cue\n')
    assert subtitle_source(p)['evidence'][0]['time']=='00:00.000 --> 00:01.500'
    txt=tmp_path/'sample.txt';txt.write_text('没有时间信息的文本')
    assert subtitle_source(txt)['basis']=='text_only'
    p.write_text('1\nnot a time --> invalid\ntext\n')
    with pytest.raises(AppError):subtitle_source(p)


def test_douyin_failed_fetch_supplement_continues_same_task(tmp_path,monkeypatch):
    import backend.tasks as task_module
    def blocked(*args,**kwargs):raise AppError('douyin_material','[契约] 公开页不可用，请补材料')
    monkeypatch.setattr(task_module,'douyin_source',blocked)
    store,vault,models=configured(tmp_path/'app')
    runner=TaskRunner(store,vault,models,lambda:None)
    task=runner.create('测试分享 https://www.douyin.com/video/contract')
    runner.run(task['id'])
    assert store.get('tasks',task['id'])['status']=='waiting_material'
    p=tmp_path/'supplement.srt';p.write_bytes(SUB)
    runner.attach(task['id'],{'path':str(p),'name':'supplement.srt'})
    runner.run(task['id']);result=store.get('tasks',task['id'])
    assert result['status']=='succeeded' and result['id']==task['id']
    assert 'douyin.com' in result['source']['original_input']
    assert result['analysis']['basis']=='subtitles'


def test_skill_optimistic_version_prevents_lost_edit(tmp_path):
    s=SkillService(Store(tmp_path));a=s.create({'name':'version','body':'first'})
    s.update(a['id'],{'body':'second','expected_version':1})
    with pytest.raises(AppError) as e:s.update(a['id'],{'body':'stale third','expected_version':1})
    assert e.value.code=='version_conflict'
    assert s.get(a['id'])['body']=='second'


def test_sqlite_handles_close_at_context_exit(tmp_path):
    store=Store(tmp_path)
    with store.connect() as connection:
        assert connection.execute('SELECT 1').fetchone()==(1,)
    with pytest.raises(sqlite3.ProgrammingError):connection.execute('SELECT 1')


def test_deepseek_bounded_extraction_default_and_explicit_override(tmp_path):
    received=[]
    def handler(req):
        received.append(json.loads(req.content));return mock_model(req)
    store,vault,models=configured(tmp_path,handler)
    store.put('model_configs',{'id':'text','base_url':'https://api.deepseek.com','model':'deepseek-flash'})
    models.probe('text')
    assert received[-1]['thinking']=={'type':'disabled'}
    store.put('model_configs',{'id':'text','base_url':'https://api.deepseek.com','model':'deepseek-flash','thinking_mode':'provider_default'})
    models.probe('text')
    assert 'thinking' not in received[-1]
    store.put('model_configs',{'id':'text','base_url':'https://other-provider.invalid/v1','model':'manual'})
    models.probe('text');assert 'thinking' not in received[-1]


def test_truncated_model_reply_is_not_retried_and_has_private_diagnostic(tmp_path):
    calls=[]
    def handler(req):
        calls.append(1)
        return httpx.Response(200,json={'model':'contract','choices':[{'message':{'content':'{"overview":"unfinished'},'finish_reason':'length'}],'usage':{'total_tokens':128}})
    store,vault,models=configured(tmp_path,handler)
    with pytest.raises(AppError) as e:models.analyze({'text':'fixture','evidence':[],'snapshot_hash':'known-source'})
    assert e.value.code=='output_limit' and len(calls)==1
    run=store.get('model_runs',e.value.diagnostic_id)
    assert run['source_snapshot_hash']=='known-source' and run['finish_reason']=='length'
    assert run['response'].startswith('{"overview"')


def test_model_diagnostics_scoped_to_task_and_credentials_redacted(tmp_path):
    store,vault,models=configured(tmp_path)
    vault.set('model_text','synthetic-contract-secret')
    def handler(req):return httpx.Response(200,json={'choices':[{'message':{'content':'bad JSON synthetic-contract-secret'}}]})
    models.client=httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as e:models.analyze({'text':'fixture','evidence':[],'snapshot_hash':'source-one'},max_repairs=0)
    app=create_app(tmp_path,start_worker=False)
    app.state.store.put('tasks',{'id':'task-one','input':'fixture','status':'failed','source':{'snapshot_hash':'source-one'},'error':{'diagnostic_id':e.value.diagnostic_id}})
    app.state.store.put('model_runs',{'source_snapshot_hash':'another-source','response':'another task data'})
    with TestClient(app) as c:
        diagnostics=c.get('/api/tasks/task-one/diagnostics').json()
        assert len(diagnostics['runs'])==1
        assert 'synthetic-contract-secret' not in json.dumps(diagnostics)
        assert '[REDACTED]' in diagnostics['runs'][0]['response']


def test_enterprise_import_preserves_upstream_source(tmp_path):
    service=SkillService(Store(tmp_path))
    skill=service.create({'name':'source-chain','body':'# Source','source':{'platform':'github','repository':'org/repo','path':'skills/a/SKILL.md','version':'a'*40,'url':'https://github.com/org/repo'}})
    origin={'platform':'feishu','origin_key':'feishu:fixture:record-one','record_id':'record-one'}
    imported=service.import_zip(service.export_zip(skill['id']),origin=origin)
    assert imported['source']['platform']=='feishu'
    assert imported['source']['upstream_source']['repository']=='org/repo'


def test_generated_skill_metadata_is_safe_and_export_folder_matches_name(tmp_path):
    value=normalize_generated_skill({'name':'real-workflow','description':'用于“来源复核”的方法','body':'# 流程\n\n依据真实材料执行。'})
    assert value['name']=='流程'
    assert value['body'].startswith('---\nname: "real-workflow"\n')
    assert 'description:' in value['body']
    service=SkillService(Store(tmp_path));skill=service.create({**value,'name':'用户修改的展示名称'})
    with zipfile.ZipFile(io.BytesIO(service.export_zip(skill['id']))) as z:
        assert 'real-workflow/SKILL.md' in z.namelist()
    with pytest.raises(AppError):normalize_generated_skill({'name':'Bad--Name','description':'x','body':'test'})


@pytest.mark.parametrize('body',['没有一级标题。','## 这是二级标题','```sh\n# 脚本注释不是标题\n```','# ###'])
def test_generated_skill_without_h1_keeps_slug_as_display_name(body):
    value=normalize_generated_skill({'name':'source-review','description':'完整触发说明','body':body})
    assert value['name']=='source-review'
    assert value['body'].endswith(body)


@pytest.mark.parametrize('body',[
    '<!--\n# 注释里的占位标题\n-->\n# 真正标题',
    '<!-- # 注释里的占位标题 -->\n# 真正标题',
    '```html\n<!--\n# 围栏中的示例\n```\n# 真正标题',
])
def test_generated_display_title_ignores_common_html_comments_without_changing_body(body):
    value=normalize_generated_skill({'name':'actual-title','description':'来源用途','body':body})
    assert value['name']=='真正标题'
    assert value['body'].startswith('---\nname: "actual-title"\n')
    assert value['body'].endswith(body)


@pytest.mark.parametrize('length',[150,151,201])
def test_generated_display_title_limit_falls_back_without_truncating_body(tmp_path,length):
    title='中'*length
    body='# '+title+'\n\n完整正文不截断。'
    description='完整用途与触发说明。'
    value=normalize_generated_skill({'name':'safe-slug','description':description,'body':body})
    expected=title if length<=150 else 'safe-slug'
    assert value['name']==expected and value['description']==description
    assert value['body'].endswith(body)
    skill=SkillService(Store(tmp_path)).create(value)
    assert skill['name']==expected and skill['body']==value['body']


def test_generated_chinese_title_preserves_raw_reply_slug_description_and_imported_body(tmp_path):
    description='在需要安装 Easel 并配置内容工作流时使用；'+('保留完整触发条件、限制与说明。'*12)
    original={'name':'easel-content-workflow','description':description,
        'body':'---\nname: old-generated-header\ntitle: "# 不是正文标题"\n---\n\n```sh\n# 示例注释\n```\n\n# Easel 安装与内容创作工作流 ###\n\n依据固定来源整理，未执行。'}
    snapshot=json.loads(json.dumps(original))
    raw_reply=json.dumps(original,ensure_ascii=False,indent=2)
    calls=[]
    def handler(request):
        calls.append(request)
        task=json.loads(json.loads(request.content)['messages'][1]['content'])['task']
        assert 'body以一个描述用途的中文一级标题开始' in task
        assert '后续结构使用二级标题' in task
        assert 'JSON name仍为英文小写slug' in task
        return httpx.Response(200,json={'model':'contract-model','choices':[{'message':{'content':raw_reply}}],
            'usage':{'completion_tokens':100}})
    store,_,models=configured(tmp_path/'generated',handler)
    draft=models.skill_draft({'text':'[契约] 固定来源'},ANALYSIS)
    assert len(calls)==1 and draft['name']=='Easel 安装与内容创作工作流'
    assert draft['description']==description and original==snapshot
    assert draft['body'].startswith('---\nname: "easel-content-workflow"\n')
    assert 'description: '+json.dumps(description,ensure_ascii=False) in draft['body']
    assert draft['body'].endswith('# Easel 安装与内容创作工作流 ###\n\n依据固定来源整理，未执行。')
    assert store.get('model_runs',draft['generation']['model_run_id'])['response']==raw_reply
    skills=SkillService(store);skill=skills.create(draft)
    package=skills.export_zip(skill['id'])
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert {path.split('/')[0] for path in archive.namelist()}=={'easel-content-workflow'}
        assert archive.read('easel-content-workflow/SKILL.md')==draft['body'].encode()
        assert json.loads(archive.read('easel-content-workflow/manifest.json'))['name']==draft['name']
    imported=SkillService(Store(tmp_path/'imported')).import_zip(package)
    assert imported['name']==draft['name'] and imported['description']==description
    assert imported['body']==draft['body'] and imported['content_hash']==skill['content_hash']


def test_third_party_skill_import_never_rewrites_frontmatter_or_title(tmp_path):
    body=b'---\r\nname: upstream-skill\r\ndescription: upstream trigger\r\ncustom: preserve-this\r\n---\r\n\r\n# Upstream title\r\n'
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        archive.writestr('upstream-skill/SKILL.md',body)
    skills=SkillService(Store(tmp_path))
    imported=skills.import_zip(data.getvalue())
    assert imported['body'].encode()==body and imported['name']=='upstream-skill'
    restored,_=unpack(skills.export_zip(imported['id']))
    assert restored['SKILL.md']==body


def test_concurrent_draft_requests_do_not_duplicate_provider_calls(tmp_path):
    called=threading.Event();release=threading.Event();calls=[]
    def handler(req):
        calls.append(1);called.set();assert release.wait(5)
        return mock_model(req)
    app=create_app(tmp_path,start_worker=False,model_client=httpx.Client(transport=httpx.MockTransport(handler)))
    app.state.store.put('model_configs',{'id':'text','base_url':'https://contract.invalid/v1','model':'contract-model'})
    app.state.store.put('tasks',{'id':'draft-once','status':'succeeded','input':'fixture','analysis':ANALYSIS,'source':{'text':'known source','evidence':[{'id':'sub-1','excerpt':'known source'}]}})
    results=[]
    with TestClient(app) as client:
        threads=[threading.Thread(target=lambda:results.append(client.post('/api/tasks/draft-once/skill'))) for _ in range(2)]
        for thread in threads:thread.start()
        assert called.wait(3);release.set()
        for thread in threads:thread.join(timeout=8)
    assert len(calls)==1 and len(results)==2
    assert all(r.status_code==200 for r in results)
    assert results[0].json()['id']==results[1].json()['id']


def test_editing_model_keeps_unchanged_feishu_test_result(tmp_path):
    app=create_app(tmp_path,start_worker=False)
    connection=app.state.store.put('connections',{'id':'feishu','app_id':'cli_fixture','app_token':'baseFixture','table_id':'tblFixture','last_test':{'status':'passed','at':'fixture-test-time'}})
    with TestClient(app) as c:
        value=c.put('/api/settings',json={'models':{'text':{'base_url':'https://contract.invalid/v1','model':'fixture'}},'feishu':connection}).json()
        assert value['feishu']['last_test']['at']=='fixture-test-time'
        value=c.put('/api/settings',json={'feishu':{'table_id':'tblNewFixture'}}).json()
        assert 'last_test' not in value['feishu']
