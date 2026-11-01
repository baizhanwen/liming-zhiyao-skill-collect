"""Explicit offline application contracts; no real accounts, sites or models."""
from copy import deepcopy
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.security import AppError, Vault
from backend.store import Store
from backend.tasks import TaskRunner
from backend.douyin_session import DouyinSessionService
from scripts.backup import make_backup
import backend.media as media

VIDEO = '1234567890123456789'
SHARE = 'https://v.douyin.com/EXPLICIT_SYNTHETIC_SHARE/'
PAGE = 'https://www.douyin.com/video/'+VIDEO
PRIVATE = 'synthetic-private-account-cookie'
STATE = {'cookies': [{'name': 'sessionid', 'value': PRIVATE, 'domain': '.douyin.com',
    'path': '/', 'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}], 'origins': []}


class SessionFixture:
    def __init__(self, store=None, vault=None, status='signed_out'):
        self.value = status
        self.calls = []
        self.closed = False

    def status(self):
        return {'status': self.value, 'session_saved': self.value=='logged_in',
            'browser_open': self.value in ('waiting_scan','waiting_confirmation','checking'),
            'verified_at': '2026-11-01T00:00:00+00:00' if self.value=='logged_in' else None,
            'expires_at': None, 'error': None}

    def start_login(self):
        self.calls.append('login'); self.value='waiting_scan'; return self.status()

    def check_login(self):
        self.calls.append('check'); self.value='logged_in'; return self.status()

    def cancel_login(self):
        self.calls.append('cancel'); self.value='cancelled'; return self.status()

    def clear(self):
        self.calls.append('clear'); self.value='signed_out'; return self.status()

    def storage_state(self):
        return deepcopy(STATE) if self.value=='logged_in' else None

    def qr_image(self,updated_at=None):
        # Explicit synthetic protocol fixture, not an official login QR.
        if updated_at is not None and updated_at!='fixture-current':return None
        return b'\x89PNG\r\n\x1a\nsynthetic-qr-fixture' if self.value=='waiting_scan' else None

    def verification_image(self,frame_id=None):
        if frame_id is not None and frame_id!='1':return None
        return b'\xff\xd8explicit-verification-image-fixture\xff\xd9' if self.value=='verification_required' else None

    def verification_action(self,action):
        if self.value!='verification_required':raise AppError('verification_inactive','官方验证当前不可用',status=409)
        self.calls.append({'kind':action['kind'],'frame_id':action['frame_id']})
        return self.status()

    def close(self):self.closed=True


def test_session_routes_are_metadata_only_and_close_with_application(tmp_path):
    fixture=SessionFixture()
    app=create_app(tmp_path,start_worker=False,douyin_factory=lambda *_:fixture)
    with TestClient(app) as client:
        assert client.get('/api/douyin/session').json()['status']=='signed_out'
        for action,status in (('login','waiting_scan'),('check','logged_in'),('cancel','cancelled'),('clear','signed_out')):
            response=client.post('/api/douyin/session/'+action,json={})
            assert response.status_code==200 and response.json()['status']==status
            assert PRIVATE not in response.text and 'cookies' not in response.text
        assert client.get('/api/status').json()['douyin']['status']=='signed_out'
        assert 'douyin_storage_state' not in client.get('/api/settings').text
    assert fixture.closed and fixture.calls==['login','check','cancel','clear']


def test_cross_origin_cannot_open_or_clear_login_browser(tmp_path):
    fixture=SessionFixture()
    app=create_app(tmp_path,start_worker=False,douyin_factory=lambda *_:fixture)
    with TestClient(app) as client:
        for action in ('login','check','cancel','clear'):
            assert client.post('/api/douyin/session/'+action,json={},
                headers={'Origin':'https://untrusted.contract.invalid'}).status_code==403
    assert fixture.calls==[]


def test_qr_is_ephemeral_image_only_and_not_cross_site_embeddable(tmp_path):
    fixture=SessionFixture()
    app=create_app(tmp_path,start_worker=False,douyin_factory=lambda *_:fixture)
    with TestClient(app) as client:
        missing=client.get('/api/douyin/session/qr')
        assert missing.status_code==404 and missing.headers['cache-control']=='no-store, private'
        client.post('/api/douyin/session/login',json={})
        image=client.get('/api/douyin/session/qr',headers={'sec-fetch-site':'same-origin'})
        assert image.status_code==200 and image.content==fixture.qr_image()
        assert image.headers['content-type']=='image/png'
        assert image.headers['cross-origin-resource-policy']=='same-origin'
        assert PRIVATE not in image.text
        assert client.get('/api/douyin/session/qr?v=fixture-current').status_code==200
        assert client.get('/api/douyin/session/qr?v=stale').status_code==404
        for site in ('same-site','cross-site'):
            assert client.get('/api/douyin/session/qr',headers={'sec-fetch-site':site}).status_code==403
        assert client.get('/api/douyin/session/qr',headers={'Origin':'https://contract.invalid'}).status_code==403
        client.post('/api/douyin/session/cancel',json={})
        assert client.get('/api/douyin/session/qr').status_code==404


def test_official_verification_view_is_same_origin_no_store_and_user_action_only(tmp_path,caplog):
    fixture=SessionFixture()
    app=create_app(tmp_path,start_worker=False,douyin_factory=lambda *_:fixture)
    private_text='EXPLICIT_PRIVATE_VERIFICATION_VALUE'
    with TestClient(app) as client:
        assert client.get('/api/douyin/session/verification').status_code==404
        fixture.value='verification_required'
        picture=client.get('/api/douyin/session/verification')
        assert picture.status_code==200 and picture.content==fixture.verification_image()
        assert picture.headers['content-type']=='image/jpeg'
        assert picture.headers['cache-control']=='no-store, private'
        assert picture.headers['cross-origin-resource-policy']=='same-origin'
        assert client.get('/api/douyin/session/verification?v=1').status_code==200
        assert client.get('/api/douyin/session/verification?v=stale').status_code==404
        for site in ('same-site','cross-site'):
            assert client.get('/api/douyin/session/verification',headers={'sec-fetch-site':site}).status_code==403
        route='/api/douyin/session/verification/action'
        action={'frame_id':'1','kind':'text','text':private_text}
        assert client.post(route,json=action,headers={'Origin':'https://untrusted.contract.invalid'}).status_code==403
        result=client.post(route,json=action)
        assert result.status_code==200 and private_text not in result.text
        assert fixture.calls==[{'kind':'text','frame_id':'1'}]
        invalid=[{'frame_id':'1','kind':'eval','text':private_text},
            {'frame_id':'1','kind':'click','x':1.5,'y':0},
            {'frame_id':'1','kind':'click','x':True,'y':0},
            {'frame_id':'1','kind':'text','text':private_text*5},
            {'frame_id':'1','kind':'text','text':private_text,'callback':private_text}]
        for value in invalid:
            rejected=client.post(route,json=value)
            assert rejected.status_code==422 and private_text not in rejected.text
        assert len(fixture.calls)==1
        fixture.value='signed_out'
        assert client.get('/api/douyin/session/verification').status_code==404
        assert client.post(route,json=action).status_code==409
        assert private_text not in client.get('/api/settings').text
        assert private_text not in json.dumps(app.state.store.list('connections'))
    assert private_text not in caplog.text


def test_clear_stops_only_current_douyin_fetch_not_models_or_other_sources(tmp_path):
    fixture=SessionFixture(status='logged_in')
    app=create_app(tmp_path,start_worker=False,douyin_factory=lambda *_:fixture)
    runner=app.state.runner
    first=runner.create(SHARE,idempotency_key='fixture-one')
    other=runner.create('https://github.com/contract/repository',idempotency_key='fixture-two')
    model=runner.create(SHARE,idempotency_key='fixture-three')
    for task,stage in ((first,'fetch'),(other,'fetch'),(model,'analysis')):
        runner.checkpoint(task['id'],status='running',stage=stage)
    with TestClient(app) as client:
        assert client.post('/api/douyin/session/clear',json={}).json()['status']=='signed_out'
        assert app.state.store.get('tasks',first['id'])['status']=='cancelled'
        assert app.state.store.get('tasks',other['id'])['status']=='running'
        assert app.state.store.get('tasks',model['id'])['status']=='running'


def test_page_diagnostic_is_explicit_readonly_and_does_not_use_session(tmp_path):
    fixture=SessionFixture(status='logged_in'); seen=[]
    def diagnose(url):
        seen.append(url)
        return {'summary':'[契约] 仅检查公开页','network':{'status':'page_received','category':'none'}}
    app=create_app(tmp_path,start_worker=False,douyin_factory=lambda *_:fixture,douyin_diagnostic=diagnose)
    with TestClient(app) as client:
        response=client.post('/api/douyin/diagnose',json={'input':'[契约分享] '+SHARE})
        assert response.status_code==200 and seen==[SHARE]
        assert PRIVATE not in response.text and fixture.calls==[]
        assert client.post('/api/douyin/diagnose',json={'input':'https://user:synthetic@www.douyin.com/'}).status_code==400
        assert len(seen)==1 and not app.state.store.list('model_runs')


def capture_fixture(folder, **overrides):
    path=folder/'explicit-fixture.mp4';path.parent.mkdir(parents=True,exist_ok=True)
    binary=b'explicit AV fixture';path.write_bytes(binary)
    result={'observed_video_id':VIDEO,'resolved_url':PAGE,'reported_duration':9,
        'title':'[契约] 原片','media_paths':[str(path)],
        'media_files':[{'path':str(path),'size':len(binary),'sha256':hashlib.sha256(binary).hexdigest(),
            'coverage':[[0,len(binary)]],'complete':True,'video_id':VIDEO}],
        'acquisition':{'session_used':True,'storage_state':STATE,'cookie':PRIVATE}}
    result.update(overrides);return result


def test_logged_in_source_skips_anonymous_http_and_forwards_state_only_to_browser(tmp_path,monkeypatch):
    events=[];seen=[]
    monkeypatch.setattr(media,'fetch_public',lambda *_a,**_k:pytest.fail('authenticated acquisition must use the browser'))
    def resolve(url,folder,**options):
        seen.append(('resolve',url,options))
        return {'video_id':VIDEO,'resolved_url':PAGE,'method':'official_browser_redirect'}
    def capture(url,folder,**options):
        seen.append(('capture',url,options));return capture_fixture(folder)
    monkeypatch.setattr('backend.browser_capture.resolve_douyin_url',resolve)
    monkeypatch.setattr('backend.browser_capture.capture_douyin',capture)
    monkeypatch.setattr(media,'inspect_video',lambda *_:{'duration':9,'video':True,'audio':True})
    result=media.douyin_source(SHARE,tmp_path,storage_state=STATE,progress_callback=events.append)
    assert [item[0] for item in seen]==['resolve','capture']
    assert seen[0][1]==SHARE and seen[1][1]==PAGE
    assert all(item[2]['storage_state']==STATE for item in seen)
    assert result['acquisition']['session_used'] is True
    assert PRIVATE not in json.dumps(result) and 'storage_state' not in json.dumps(result)
    assert [event['phase'] for event in events][:2]==['resolving_share','resolving_share']


def test_initial_short_link_failure_can_still_resolve_in_project_browser(tmp_path,monkeypatch):
    monkeypatch.setattr(media,'fetch_public',lambda *_a,**_k:(_ for _ in ()).throw(AppError('dns','[契约] 公开HTTP失败')))
    calls=[]
    def resolve(url,folder,**options):
        calls.append(options)
        return {'video_id':VIDEO,'resolved_url':PAGE,'method':'official_browser_redirect'}
    monkeypatch.setattr('backend.browser_capture.resolve_douyin_url',resolve)
    monkeypatch.setattr(media,'_browser_douyin_source',lambda *args,**kwargs:{'fixture_video_id':args[2]})
    result=media.douyin_source(SHARE,tmp_path)
    assert result=={'fixture_video_id':VIDEO} and len(calls)==1
    assert 'storage_state' not in calls[0]


@pytest.mark.parametrize('overrides',[
    {'method':'unattested_dom'}, {'video_id':'999'},
    {'video_id':'１２３'}, {'resolved_url':'https://other.contract.invalid/video/'+VIDEO},
    {'resolved_url':'http://www.douyin.com/video/'+VIDEO},
    {'resolved_url':'https://user:synthetic@www.douyin.com/video/'+VIDEO},
    {'resolved_url':'https://www.douyin.com:8443/video/'+VIDEO},
])
def test_resolver_result_cannot_relax_official_source_identity(tmp_path,monkeypatch,overrides):
    result={'video_id':VIDEO,'resolved_url':PAGE,'method':'official_browser_redirect',**overrides}
    monkeypatch.setattr('backend.browser_capture.resolve_douyin_url',lambda *_a,**_k:result)
    monkeypatch.setattr(media,'_browser_douyin_source',lambda *_a,**_k:pytest.fail('unattested identity'))
    with pytest.raises(AppError) as error:
        media.douyin_source(SHARE,tmp_path,storage_state=STATE)
    assert error.value.code=='douyin_browser_identity'


@pytest.mark.parametrize('status',['opening','waiting_scan','waiting_confirmation','checking','verification_required','expired'])
def test_pending_or_expired_login_does_not_start_capture_or_model(tmp_path,monkeypatch,status):
    store=Store(tmp_path);vault=Vault(tmp_path);session=SessionFixture(status=status)
    runner=TaskRunner(store,vault,None,lambda:None,douyin_session=session)
    monkeypatch.setattr('backend.tasks.douyin_source',lambda *_a,**_k:pytest.fail('must wait for login'))
    task=runner.create(SHARE);runner.run(task['id']);result=store.get('tasks',task['id'])
    assert result['status']=='waiting_material' and result['attempts']==1
    assert result['error']['code'] in ('douyin_login_pending','douyin_session_expired')
    assert not store.list('model_runs')


def test_encrypted_login_is_not_in_settings_database_or_private_backup(tmp_path):
    class CompleteRunner:
        def run(self,options,on_message,cancelled):
            return {'type':'result','ok':True,'authenticated':True,'storage_state':STATE,
                'verification_method':'official_profile_response'}
        def send(self,message):pass
        def close(self):pass
    def factory(store,vault):
        return DouyinSessionService(store,vault,runner_factory=CompleteRunner)
    app=create_app(tmp_path,start_worker=False,douyin_factory=factory)
    with TestClient(app) as client:
        client.post('/api/douyin/session/login',json={})
        deadline=time.monotonic()+2
        while app.state.douyin.status()['status'] in ('opening','waiting_scan','checking') and time.monotonic()<deadline:
            time.sleep(.005)
        assert app.state.douyin.status()['status']=='logged_in'
        for endpoint in ('/api/douyin/session','/api/status','/api/settings'):
            text=client.get(endpoint).text
            assert PRIVATE not in text and 'douyin_storage_state' not in text and 'cookies' not in text
        assert PRIVATE.encode() not in (tmp_path/'secrets.enc').read_bytes()
        with zipfile.ZipFile(io.BytesIO(make_backup(app.state.store))) as backup:
            assert 'secrets.enc' not in backup.namelist() and '.secrets.key' not in backup.namelist()
            assert all(PRIVATE.encode() not in backup.read(name) for name in backup.namelist())
