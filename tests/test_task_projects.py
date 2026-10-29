"""Project association orchestration; all sources/services below are explicit fixtures."""
from copy import deepcopy
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.models import Analysis, ModelService
from backend.security import AppError, Vault
from backend.store import Store
from backend.tasks import TaskRunner


ANALYSIS = {'overview':'[契约] 视频提及项目','purpose':'测试关联', 'audience':'测试',
    'steps':['核对证据'], 'dependencies':[], 'limitations':['未执行项目'], 'methods':[],
    'claims':[], 'project_mentions':[{'name':'SampleProject','evidence_ids':['sub-1'],
        'quote':'介绍开源项目 SampleProject'}]}


class FixtureModels:
    def __init__(self):self.calls=0
    def analyze(self,source):
        self.calls+=1
        return deepcopy(ANALYSIS)


def runner_fixture(tmp_path):
    store=Store(tmp_path); models=FixtureModels()
    def forbidden():raise AssertionError('fixture must not call a live connector')
    runner=TaskRunner(store,Vault(tmp_path),models,forbidden)
    subtitle=tmp_path/'explicit-fixture.srt'
    subtitle.write_text('1\n00:00:01,000 --> 00:00:03,000\n介绍开源项目 SampleProject\n')
    task=runner.create('https://www.douyin.com/video/123456789', uploads=[
        {'path':str(subtitle),'name':subtitle.name}])
    return store,models,runner,task


def test_project_stage_saved_with_knowledge_and_original_evidence(tmp_path,monkeypatch):
    store,models,runner,task=runner_fixture(tmp_path); seen=[]
    def lookup(source,analysis,factory):
        seen.append(deepcopy(source))
        assert analysis['project_mentions'][0]['name']=='SampleProject'
        return {'status':'found','links':[{'id':'fixture-project','status':'candidates',
            'evidence':[{'id':'sub-1','time':source['evidence'][0]['time']}]}], 'errors':[]}
    monkeypatch.setattr('backend.tasks.resolve_project_links',lookup)
    runner.run(task['id']); result=store.get('tasks',task['id'])
    assert result['status']=='succeeded' and models.calls==1 and len(seen)==1
    assert result['source']==seen[0]
    assert result['project_links']['basis']=='subtitles'
    assert result['project_links']['source_snapshot_hash']==result['source']['snapshot_hash']
    assert store.get('knowledge',task['id'])['project_links']==result['project_links']
    stages=[s['stage'] for s in result['stages']]
    assert stages.index('analysis')<stages.index('project_links')<stages.index('save')


def test_lookup_failure_keeps_analysis_and_recheck_never_calls_models(tmp_path,monkeypatch):
    store,models,runner,task=runner_fixture(tmp_path); calls=[]
    def lookup(*args):
        calls.append(True)
        return {'status':'unavailable' if len(calls)==1 else 'found','links':[],
            'errors':[{'code':'rate_limit','message':'[契约] 限流'}] if len(calls)==1 else []}
    monkeypatch.setattr('backend.tasks.resolve_project_links',lookup)
    runner.run(task['id']); first=store.get('tasks',task['id'])
    assert first['status']=='succeeded' and first['project_links']['status']=='unavailable'
    knowledge=store.get('knowledge',task['id'])
    store.put('knowledge',{**knowledge,'summary':'用户编辑的摘要','body':'用户保留的正文'})
    result=runner.refresh_project_links(task['id'])
    assert models.calls==1 and len(calls)==2 and result['project_links']['status']=='found'
    assert result['analysis']==first['analysis'] and result['source']==first['source']
    saved=store.get('knowledge',task['id'])
    assert saved['summary']=='用户编辑的摘要' and saved['body']=='用户保留的正文'
    assert saved['project_links']==result['project_links']


def test_resume_preserves_project_checkpoint_and_avoids_model_or_search_calls(tmp_path,monkeypatch):
    store,models,runner,task=runner_fixture(tmp_path)
    source={'basis':'audio_transcript','platform':'douyin','text':'[契约] 音频材料',
        'evidence':[],'snapshot_hash':'fixture-fixed-snapshot','title':'[契约] 视频'}
    links={'status':'none','links':[],'source_snapshot_hash':source['snapshot_hash']}
    runner.checkpoint(task['id'],source=source,analysis=deepcopy(ANALYSIS),project_links=links)
    def forbidden(*args):raise AssertionError('saved checkpoint must be reused')
    monkeypatch.setattr('backend.tasks.resolve_project_links',forbidden)
    runner.run(task['id']); result=store.get('tasks',task['id'])
    assert result['status']=='succeeded' and result['project_links']==links and models.calls==0
    assert store.get('knowledge',task['id'])['project_links']==links


def test_new_material_clears_prior_project_links(tmp_path):
    store,_,runner,task=runner_fixture(tmp_path)
    runner.checkpoint(task['id'],status='failed',project_links={'status':'found'},analysis=ANALYSIS)
    result=runner.attach(task['id'],{'path':str(tmp_path/'new.srt'),'name':'new.srt'})
    assert 'project_links' not in result and 'analysis' not in result


def test_manual_lookup_requires_completed_material_and_rejects_concurrent_request(tmp_path):
    _,_,runner,task=runner_fixture(tmp_path)
    with pytest.raises(AppError,match='先完成'):runner.refresh_project_links(task['id'])
    runner.checkpoint(task['id'],status='succeeded',source={'basis':'source_files'},analysis=ANALYSIS)
    with pytest.raises(AppError,match='无需再次'):runner.refresh_project_links(task['id'])
    runner.checkpoint(task['id'],source={'basis':'subtitles'})
    runner.project_lookup_ids.add(task['id'])
    with pytest.raises(AppError,match='正在进行'):runner.refresh_project_links(task['id'])


def test_lookup_route_reuses_result_without_creating_task_or_running_model(tmp_path,monkeypatch):
    app=create_app(tmp_path,start_worker=False)
    task=app.state.runner.create('[契约] 保存的材料')
    app.state.runner.checkpoint(task['id'],status='succeeded',source={'basis':'subtitles',
        'snapshot_hash':'fixed','evidence':[]},analysis=ANALYSIS)
    monkeypatch.setattr('backend.tasks.resolve_project_links',lambda *_:{'status':'none','links':[],'errors':[]})
    with TestClient(app) as client:
        response=client.post('/api/tasks/'+task['id']+'/project-links')
    assert response.status_code==200 and response.json()['project_links']['source_snapshot_hash']=='fixed'
    assert len(app.state.store.list('tasks'))==1
    assert not app.state.store.list('model_runs')


def test_old_analysis_schema_defaults_to_no_model_project_mentions():
    legacy={k:v for k,v in ANALYSIS.items() if k!='project_mentions'}
    assert Analysis.model_validate(legacy).project_mentions==[]


@pytest.mark.parametrize('bad_hints',[None,{'name':'not-a-list'},[{'name':'missing-quote'}],
    ANALYSIS['project_mentions']*4])
def test_optional_hint_format_never_repeats_paid_core_analysis(tmp_path,bad_hints):
    store=Store(tmp_path); vault=Vault(tmp_path); calls=[]
    store.put('model_configs',{'id':'text','base_url':'https://explicit-contract.invalid/v1','model':'fixture'})
    raw=json.dumps({**ANALYSIS,'project_mentions':bad_hints})
    def reply(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':raw}}]})
    models=ModelService(store,vault,httpx.Client(transport=httpx.MockTransport(reply)))
    result=models.analyze({'evidence':[{'id':'sub-1','excerpt':'介绍开源项目 SampleProject'}],
        'basis':'subtitles','snapshot_hash':'fixture'})
    assert len(calls)==1 and result['overview']==ANALYSIS['overview']
    assert result['project_mention_warnings'] and len(result['project_mentions'])<=3
    assert store.get('model_runs',result['model_run_id'])['response']==raw


def test_failed_optional_hint_is_visible_in_lookup_report(tmp_path,monkeypatch):
    _,_,runner,_=runner_fixture(tmp_path)
    monkeypatch.setattr('backend.tasks.resolve_project_links',lambda *_:{'status':'none','links':[],'errors':[]})
    result=runner._project_links({'basis':'subtitles'}, {'project_mention_warnings':[{'code':'format'}]})
    assert result['status']=='unavailable'
    assert result['errors'][0]['code']=='project_mentions_format'
    assert result['warnings']


def test_new_url_submission_never_returns_old_supplemented_text_success(tmp_path):
    store=Store(tmp_path);runner=TaskRunner(store,Vault(tmp_path),object(),lambda:None)
    link='https://www.douyin.com/video/123456789'
    old=runner.create(link)
    runner.checkpoint(old['id'],status='succeeded',uploads=[{'path':'old-summary.txt'}],
        source={'basis':'text_only','platform':'upload'},analysis=ANALYSIS)
    fresh=runner.create(link)
    assert fresh['id']!=old['id'] and fresh['status']=='queued' and not fresh['uploads']
    assert store.get('tasks',old['id'])['source']['basis']=='text_only'
    assert runner.create(link)['id']==fresh['id']


def test_resubmitting_failed_automatic_url_resumes_without_manual_retry(tmp_path):
    store=Store(tmp_path);runner=TaskRunner(store,Vault(tmp_path),object(),lambda:None)
    link='https://v.douyin.com/explicit-contract'
    old=runner.create(link)
    runner.checkpoint(old['id'],status='waiting_material',error={'code':'douyin_material'})
    resumed=runner.create(link)
    assert resumed['id']==old['id'] and resumed['status']=='queued' and resumed['error'] is None
    assert len(store.list('tasks'))==1


def test_explicit_idempotency_key_keeps_original_result_even_when_supplemented(tmp_path):
    store=Store(tmp_path);runner=TaskRunner(store,Vault(tmp_path),object(),lambda:None)
    old=runner.create('https://v.douyin.com/explicit-contract',idempotency_key='explicit-request')
    runner.checkpoint(old['id'],status='succeeded',uploads=[{'path':'summary.txt'}],source={'basis':'text_only'})
    assert runner.create(old['input'],idempotency_key='explicit-request')['id']==old['id']


def test_capture_failure_keeps_safe_counts_without_headers_urls_or_provider_call(tmp_path,monkeypatch):
    store,models,runner,_=runner_fixture(tmp_path)
    task=runner.create('https://www.douyin.com/video/123456789')
    def blocked(*_args,**_kwargs):
        error=AppError('douyin_browser_material','[契约] 未取得原片')
        error.capture_diagnostic={'requests':3,'downloaded_bytes':1200,'blocked_requests':1,
            'http_status_counts':{'200':2},'error_counts':{'network':1,'bad:header':7},
            'headers':{'Cookie':'SYNTHETIC_FORBIDDEN'},'url':'https://fixture.invalid/?token=SYNTHETIC_FORBIDDEN'}
        raise error
    monkeypatch.setattr('backend.tasks.douyin_source',blocked)
    runner.run(task['id']); result=store.get('tasks',task['id'])
    assert result['status']=='waiting_material' and models.calls==0
    assert result['error']['capture_diagnostic']=={'requests':3,'downloaded_bytes':1200,
        'blocked_requests':1,'http_status_counts':{'200':2},'error_counts':{'network':1}}
    assert 'SYNTHETIC_FORBIDDEN' not in json.dumps(result)


def test_verbose_host_logging_cannot_print_private_network_urls_or_headers(tmp_path,caplog):
    levels={name:logging.getLogger(name).level for name in ('httpx','httpcore')}
    try:
        caplog.set_level(logging.DEBUG)
        for name in levels:logging.getLogger(name).setLevel(logging.DEBUG)
        create_app(tmp_path,start_worker=False)
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200))) as client:
            client.get('https://media.contract.invalid/video?signature=SYNTHETIC_PRIVATE_QUERY')
        logging.getLogger('httpcore.connection').debug('headers Cookie=SYNTHETIC_PRIVATE_COOKIE')
        assert 'SYNTHETIC_PRIVATE_QUERY' not in caplog.text
        assert 'SYNTHETIC_PRIVATE_COOKIE' not in caplog.text
    finally:
        for name,level in levels.items():logging.getLogger(name).setLevel(level)
