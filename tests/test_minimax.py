"""Explicit MiniMax ASR transport contracts; no real credentials or model calls."""
import wave

import httpx
import pytest

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
