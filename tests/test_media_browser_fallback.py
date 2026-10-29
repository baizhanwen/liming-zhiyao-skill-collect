"""Product source gate using explicit fake capture/probe responses, never a website."""
import hashlib
from pathlib import Path

import pytest

from backend import media
from backend.security import AppError

VIDEO='1234567890123456789'
SHARE='https://v.douyin.com/explicit-offline-fixture/'
PAGE='https://www.douyin.com/video/'+VIDEO


def capture_fixture(folder,contents=(b'AV fixture',)):
    folder=Path(folder);work=folder/'browser-fixture';work.mkdir(parents=True,exist_ok=True)
    paths=[];records=[]
    for index,payload in enumerate(contents):
        path=work/(str(index)+'.mp4');path.write_bytes(payload);paths.append(str(path))
        records.append({'path':str(path),'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),
            'coverage':[[0,len(payload)]],'complete':True,'video_id':VIDEO})
    return {'media_paths':paths,'media_files':records,'observed_video_id':VIDEO,'resolved_url':PAGE,
        'reported_duration':10,'title':'[契约] 指定视频','acquisition':{'method':'project_browser',
            'requests':2,'renderer_version':'contract-only','cookie':'must-never-propagate',
            'signed_url':'must-never-propagate'}}


def probe(path):
    payload=Path(path).read_bytes()
    return {'duration':10,'video':not payload.startswith(b'AUDIO'),
        'audio':not payload.startswith(b'VIDEO')}


def test_share_static_metadata_automatically_uses_project_capture_and_preserves_source(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(media,'fetch_public',lambda *_args,**_kwargs:(b'<title>summary only</title>',PAGE,{}))
    def capture(url,folder,**options):
        calls.append((url,options))
        return capture_fixture(folder)
    monkeypatch.setattr('backend.browser_capture.capture_douyin',capture)
    monkeypatch.setattr(media,'inspect_video',probe)
    cancelled=lambda:False
    result=media.douyin_source(SHARE,tmp_path,cancelled=cancelled)
    assert len(calls)==1 and calls[0][0]==PAGE and calls[0][1]['video_id']==VIDEO
    assert calls[0][1]['cancelled'] is cancelled
    assert result['url']==SHARE and result['resolved_url']==PAGE and result['basis']=='video_pending'
    assert result['text']=='' and result['evidence']==[]
    assert result['acquisition']['method']=='project_browser'
    assert result['acquisition']['probe']=={'duration':10,'video':True,'audio':True}
    assert 'cookie' not in result['acquisition'] and 'signed_url' not in result['acquisition']
    assert Path(result['media_path']).read_bytes()==b'AV fixture'


def test_browser_metadata_alone_never_counts_as_original_media(tmp_path,monkeypatch):
    monkeypatch.setattr('backend.browser_capture.capture_douyin',lambda *_a,**_k:{
        'media_paths':[],'observed_video_id':VIDEO,'resolved_url':PAGE,'title':'metadata only'})
    monkeypatch.setattr(media,'inspect_video',lambda *_:pytest.fail('no media to inspect'))
    with pytest.raises(AppError,match='尚未取得完整原片'):
        media._browser_douyin_source(SHARE,tmp_path,VIDEO)


@pytest.mark.parametrize('invalid',['wrong_id','digest','coverage','duration','outside_folder'])
def test_identity_hash_coverage_duration_and_private_path_are_required(tmp_path,monkeypatch,invalid):
    captured=capture_fixture(tmp_path)
    if invalid=='wrong_id':captured['observed_video_id']='987654321'
    elif invalid=='digest':captured['media_files'][0]['sha256']='0'*64
    elif invalid=='coverage':captured['media_files'][0]['coverage']=[[0,1]]
    elif invalid=='duration':captured['reported_duration']=200
    else:
        outside=tmp_path.parent/'outside-fixture.mp4';outside.write_bytes(b'AV fixture')
        captured['media_paths']=[str(outside)]
        captured['media_files'][0]['path']=str(outside)
    monkeypatch.setattr('backend.browser_capture.capture_douyin',lambda *_a,**_k:captured)
    monkeypatch.setattr(media,'inspect_video',probe)
    with pytest.raises(AppError):media._browser_douyin_source(SHARE,tmp_path,VIDEO)
    assert not (tmp_path/'douyin.mp4').exists()


def test_split_target_audio_video_are_remuxed_without_reencoding(tmp_path,monkeypatch):
    captured=capture_fixture(tmp_path,(b'VIDEO fixture',b'AUDIO fixture'));commands=[]
    monkeypatch.setattr('backend.browser_capture.capture_douyin',lambda *_a,**_k:captured)
    monkeypatch.setattr(media,'inspect_video',probe)
    def mux(command):
        commands.append(command);Path(command[-1]).write_bytes(b'AV mux fixture')
    monkeypatch.setattr(media,'run_media',mux)
    result=media._browser_douyin_source(SHARE,tmp_path,VIDEO)
    assert result['acquisition']['probe']['audio']
    assert len(result['acquisition']['component_media_sha256'])==2
    assert commands[0][commands[0].index('-c')+1]=='copy'
    assert Path(result['media_path']).read_bytes()==b'AV mux fixture'


def test_actual_silent_video_is_explicitly_limited(tmp_path,monkeypatch):
    captured=capture_fixture(tmp_path,(b'VIDEO silent fixture',))
    monkeypatch.setattr('backend.browser_capture.capture_douyin',lambda *_a,**_k:captured)
    monkeypatch.setattr(media,'inspect_video',probe)
    result=media._browser_douyin_source(SHARE,tmp_path,VIDEO)
    assert result['acquisition']['probe']['audio'] is False
    assert result['limitations']==['采集到的视频未发现音轨，当前不能分析原音']
