"""Explicit MiniMax ASR transport contracts; no real credentials or model calls."""
import wave
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app

from backend.models import MINIMAX_ASR_MAX_BYTES, ModelService
from backend.security import AppError
from backend.store import Store


class ContractVault:
    def __init__(self):
        self.accessed = []

    def get(self, key):
        self.accessed.append(key)
        assert key == 'model_transcription', 'ASR must never copy/read a different capability credential'
        return 'synthetic-asr-key'


def service(tmp_path, handler, base_url='https://api.minimax.cn/v1'):
    store = Store(tmp_path)
    store.put('model_configs', {'id':'transcription', 'base_url':base_url, 'model':'asr-1.0'})
    store.put('model_configs', {'id':'text', 'base_url':'https://text.contract.invalid/v1', 'model':'unchanged-text'})
    vault = ContractVault()
    return store, vault, ModelService(store, vault, client=httpx.Client(transport=httpx.MockTransport(handler)))


def write_wav(path, seconds=1):
    with wave.open(str(path),'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x00' * 16000 * seconds)
    return path


@pytest.mark.parametrize('base_url,expected_path', [
    ('https://api.minimax.cn/v1','/v1/speech_to_text'),
    ('https://api.minimax.cn/','/v1/speech_to_text'),
    ('https://api.minimax.io/v1/','/v1/speech_to_text'),
    ('https://api.minimax.io','/v1/speech_to_text'),
    ('https://provider.contract.invalid/v1','/v1/audio/transcriptions'),
    ('https://api.minimax.cn.evil.invalid/v1','/v1/audio/transcriptions'),
    ('https://tenant.api.minimax.cn/v1','/v1/audio/transcriptions'),
])
def test_probe_and_transcription_route_only_exact_official_hosts(tmp_path,base_url,expected_path):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.path == expected_path
        assert request.headers['authorization'] == 'Bearer synthetic-asr-key'
        assert 'multipart/form-data' in request.headers['content-type']
        assert b'name="model"\r\n\r\nasr-1.0' in request.content
        assert b'name="response_format"\r\n\r\njson' in request.content
        assert b'RIFF' in request.content
        return httpx.Response(200,json={'text':'', 'duration':1.0, 'trace_id':'contract-asr-trace'})
    store,vault,models=service(tmp_path,handler,base_url)
    original_text=store.get('model_configs','text')
    assert models.probe('transcription')['status'] == 'passed'
    result=models.transcribe(write_wav(tmp_path/'fixture.wav'))
    assert result == {'text':'', 'duration':1.0, 'trace_id':'contract-asr-trace'}
    assert len(calls) == 2 and vault.accessed == ['model_transcription','model_transcription']
    assert store.get('model_configs','text') == original_text
    assert store.get('model_configs','vision') is None
    assert store.get('model_configs','transcription')['model'] == 'asr-1.0'


@pytest.mark.parametrize('status,expected,retryable', [
    (400,'protocol',False),(401,'authentication',False),(402,'quota',False),
    (413,'upload_limit',False),(422,'content_rejected',False),
    (429,'rate_limit',True),(500,'upstream',True),
])
def test_native_asr_http_errors_are_classified_without_provider_echo(tmp_path,status,expected,retryable):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={'type':'error','error':{'type':'contract_error','message':'synthetic-private-provider-echo'}})
    _,_,models=service(tmp_path,handler)
    with pytest.raises(AppError) as caught:
        models.probe('transcription')
    assert caught.value.code == expected and caught.value.retryable is retryable
    assert 'synthetic-private-provider-echo' not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize('result', [
    [], {'duration':1}, {'text':1},
    {'text':'must not pass','error':{'type':'contract_error'}},
    {'text':'must not pass','base_resp':{'status_code':1008,'status_msg':'synthetic-private-provider-echo'}},
])
def test_unexpected_200_envelope_cannot_be_a_successful_asr_probe(tmp_path,result):
    _,_,models=service(tmp_path,lambda request:httpx.Response(200,json=result))
    with pytest.raises(AppError) as caught:
        models.probe('transcription')
    assert caught.value.code == 'protocol'
    assert 'synthetic-private-provider-echo' not in str(caught.value)


def test_minimax_asr_size_and_duration_limits_stop_before_network(tmp_path):
    calls=[]
    _,vault,models=service(tmp_path,lambda request:calls.append(request))
    too_large=tmp_path/'oversized.mp3'
    with too_large.open('wb') as audio:
        audio.truncate(MINIMAX_ASR_MAX_BYTES+1)
    with pytest.raises(AppError) as caught:
        models.transcribe(too_large)
    assert caught.value.code == 'upload_limit'
    with pytest.raises(AppError) as caught:
        models.transcribe(write_wav(tmp_path/'overlong.wav',501))
    assert caught.value.code == 'duration'
    assert calls == [] and vault.accessed == []


def test_regular_compatible_asr_keeps_existing_request_path_and_file_handling(tmp_path):
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200,json={'text':'contract-compatible-result'})
    _,_,models=service(tmp_path,handler,'https://compatible.contract.invalid/v1')
    fixture=tmp_path/'provider-specific.wav'
    fixture.write_bytes(b'provider-decides-how-to-decode')
    assert models.transcribe(fixture)['text'] == 'contract-compatible-result'
    assert paths == ['/v1/audio/transcriptions']


def test_official_asr_rejects_base_url_containing_an_endpoint_before_network(tmp_path):
    calls=[]
    _,vault,models=service(tmp_path,lambda request:calls.append(request),
        'https://api.minimax.cn/v1/speech_to_text')
    with pytest.raises(AppError) as caught:
        models.probe('transcription')
    assert caught.value.code == 'endpoint'
    assert calls == [] and vault.accessed == []


class ContractTextVault:
    def __init__(self):
        self.accessed=[]

    def get(self,key):
        self.accessed.append(key)
        assert key=='model_text'
        return 'synthetic-text-key'

    def redact(self,value,limit=None):
        return value if limit is None else value[:limit]


def text_service(tmp_path,handler,base_url='https://api.minimax.cn/v1',model='MiniMax-M3.1-Flash',**options):
    store=Store(tmp_path)
    store.put('model_configs',{'id':'text','base_url':base_url,'model':model,'max_tokens':3000,'thinking_mode':'disabled',**options})
    vault=ContractTextVault()
    return store,vault,ModelService(store,vault,httpx.Client(transport=httpx.MockTransport(handler)))


def text_response(finish_reason='stop',content='[契约] OK'):
    return httpx.Response(200,json={'model':'contract-text','choices':[{'finish_reason':finish_reason,
        'message':{'content':content,'reasoning_content':'synthetic-reasoning-must-not-be-persisted'}}],
        'usage':{'completion_tokens':42}})


@pytest.mark.parametrize('base_url,model,expected',[
    ('https://api.minimax.cn/v1','MiniMax-M3.1-Flash','low'),
    ('https://api.minimax.io/v1','MiniMax-M3.1-Flash','low'),
    ('https://api.minimax.cn/v1','MiniMax-M3',None),
    ('https://api.minimax.cn/v1','MiniMax-M3.1-Flash-Preview',None),
    ('https://api.minimax.cn/v1','minimax-m3.1-flash',None),
    ('https://api.minimax.cn.evil.invalid/v1','MiniMax-M3.1-Flash',None),
    ('https://tenant.api.minimax.cn/v1','MiniMax-M3.1-Flash',None),
    ('https://compatible.contract.invalid/v1','MiniMax-M3.1-Flash',None),
])
def test_minimax_reasoning_default_only_for_exact_official_host_and_model(tmp_path,base_url,model,expected):
    payloads=[]
    def handler(request):
        payloads.append(json.loads(request.content));return text_response()
    options={} if expected else {'reasoning_effort':'max'}
    store,vault,models=text_service(tmp_path,handler,base_url,model,**options)
    answer,meta=models.chat([{'role':'user','content':'contract'}])
    assert len(payloads)==1 and payloads[0]['max_tokens']==3000
    assert 'thinking' not in payloads[0], 'MiniMax must not receive the prior DeepSeek disabled setting'
    if expected:
        assert payloads[0]['reasoning_effort']==expected and meta['reasoning_effort']==expected
    else:
        assert 'reasoning_effort' not in payloads[0] and 'reasoning_effort' not in meta
    assert answer=='[契约] OK' and vault.accessed==['model_text']
    assert 'synthetic-reasoning-must-not-be-persisted' not in json.dumps(store.list('model_runs'))
    assert 'reasoning_content' not in json.dumps(store.list('model_runs'))


@pytest.mark.parametrize('effort',['low','medium','high','xhigh','max'])
def test_minimax_reasoning_user_override_keeps_token_budget(tmp_path,effort):
    payloads=[]
    def handler(request):
        payloads.append(json.loads(request.content));return text_response()
    _,_,models=text_service(tmp_path,handler,reasoning_effort=effort)
    _,meta=models.chat([{'role':'user','content':'contract'}])
    assert len(payloads)==1 and payloads[0]['reasoning_effort']==effort
    assert payloads[0]['max_tokens']==meta['max_tokens']==3000
    assert meta['reasoning_effort']==effort


@pytest.mark.parametrize('invalid',['none','disabled','provider_default',None,[],{}])
def test_minimax_invalid_reasoning_stops_before_network(tmp_path,invalid):
    calls=[]
    _,vault,models=text_service(tmp_path,lambda request:calls.append(request),reasoning_effort=invalid)
    with pytest.raises(AppError) as caught:
        models.chat([{'role':'user','content':'contract'}])
    assert caught.value.code=='configuration'
    assert calls==[] and vault.accessed==[]


def test_minimax_reasoning_settings_validated_and_saved_without_model_call(tmp_path):
    calls=[]
    app=create_app(tmp_path,start_worker=False,model_client=httpx.Client(transport=httpx.MockTransport(lambda request:calls.append(request))))
    with TestClient(app) as client:
        original={'id':'text','base_url':'https://api.minimax.cn/v1','model':'MiniMax-M3.1-Flash','max_tokens':3000,
            'last_test':{'status':'passed'}}
        app.state.store.put('model_configs',original)
        for effort in ('low','medium','high','xhigh','max'):
            response=client.put('/api/settings',json={'models':{'text':{'reasoning_effort':effort}}})
            assert response.status_code==200
            value=response.json()['models']['text']
            assert value['reasoning_effort']==effort and value['max_tokens']==3000
            assert 'last_test' not in value
        before=app.state.store.get('model_configs','text')
        for invalid in ('none','disabled','provider_default',None,[],{}):
            response=client.put('/api/settings',json={'models':{'text':{'reasoning_effort':invalid}}})
            assert response.status_code==400 and response.json()['error']['code']=='configuration'
            assert app.state.store.get('model_configs','text')==before
    assert calls==[]


def test_minimax_output_limit_keeps_diagnostic_without_retry_budget_growth_or_reasoning_storage(tmp_path):
    calls=[]
    def handler(request):
        calls.append(json.loads(request.content));return text_response('length','{"overview":"truncated')
    store,_,models=text_service(tmp_path,handler)
    previous=store.put('model_runs',{'id':'previous-contract-limit','response':'previous bounded reply',
        'finish_reason':'length','validation':'failed'})
    with pytest.raises(AppError) as caught:
        models.analyze({'text':'contract source','evidence':[],'snapshot_hash':'contract-snapshot'})
    assert caught.value.code=='output_limit' and len(calls)==1
    assert calls[0]['max_tokens']==3000 and calls[0]['reasoning_effort']=='low'
    assert store.get('model_runs',previous['id'])==previous
    diagnostic=store.get('model_runs',caught.value.diagnostic_id)
    assert diagnostic['finish_reason']=='length' and diagnostic['validation']=='failed'
    assert diagnostic['reasoning_effort']=='low' and diagnostic['source_snapshot_hash']=='contract-snapshot'
    assert 'synthetic-reasoning-must-not-be-persisted' not in json.dumps(store.list('model_runs'))


@pytest.mark.parametrize('content',['',None])
def test_minimax_thinking_limit_with_empty_content_preserves_usage_and_is_not_protocol_error(tmp_path,content):
    calls=[]
    usage={'prompt_tokens':140,'completion_tokens':3000,'total_tokens':3140}
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'model':'MiniMax-M3.1-Flash',
            'choices':[{'finish_reason':'length','message':{'content':content,
                'reasoning_content':'synthetic-reasoning-must-not-be-persisted'}}],'usage':usage})
    store,_,models=text_service(tmp_path,handler)
    with pytest.raises(AppError) as caught:
        models.analyze({'text':'contract source','evidence':[],'snapshot_hash':'empty-limit-contract'})
    assert caught.value.code=='output_limit' and len(calls)==1
    assert calls[0]['max_tokens']==3000 and calls[0]['reasoning_effort']=='low'
    diagnostic=store.get('model_runs',caught.value.diagnostic_id)
    assert diagnostic['response']=='' and diagnostic['finish_reason']=='length'
    assert diagnostic['usage']==usage and diagnostic['max_tokens']==3000
    assert diagnostic['validation']=='failed' and diagnostic['validation_errors']==[{'type':'output_limit'}]
    assert diagnostic['source_snapshot_hash']=='empty-limit-contract'
    assert 'reasoning_content' not in json.dumps(store.list('model_runs'))
    assert 'synthetic-reasoning-must-not-be-persisted' not in json.dumps(store.list('model_runs'))
