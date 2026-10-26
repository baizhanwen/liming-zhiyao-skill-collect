import hashlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .media import douyin_source, extract_url, prepare_media, subtitle_source
from .security import AppError
from .store import now

TERMINAL = {'succeeded','failed','cancelled','waiting_config','waiting_material'}


class TaskRunner:
    def __init__(self,store,vault,models,github_factory):
        self.store,self.vault,self.models,self.github_factory=store,vault,models,github_factory
        self.stop=threading.Event()
        self.wake=threading.Event()
        self.thread=None
        self.mutex=threading.RLock()
        self.active_ids=set()

    def recover(self):
        for task in self.store.list('tasks'):
            if task['status'] in ('running','queued'):
                uncertain=task.get('stage') in ('analysis','vision','transcription') and task['status']=='running'
                task.update(status='waiting_config' if uncertain else 'queued',error={'code':'interrupted_request','message':'服务中断时有模型请求未确认结果，请核对配置后手动继续；可能再次产生调用费用','retryable':False} if uncertain else None)
                task.setdefault('stages',[]).append({'stage':'recovery','status':task['status'],'message':'服务重启，复用已保存检查点；未确认的模型请求需手动继续' if uncertain else '服务重启，复用已保存检查点继续','at':now()})
                self.store.put('tasks',task)

    def start(self):
        self.recover()
        self.thread=threading.Thread(target=self._loop,name='skill-collect-worker',daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set();self.wake.set()
        if self.thread:self.thread.join(timeout=2)

    def create(self,input='',depth='quick',uploads=None,idempotency_key=None):
        if depth not in ('quick','deep'):
            raise AppError('depth','分析深度无效')
        if not input.strip() and not uploads:
            raise AppError('input','请粘贴来源链接或上传文件')
        key=idempotency_key or hashlib.sha256((input.strip()+'|'+depth+'|'+json.dumps(uploads or [])).encode()).hexdigest()
        with self.mutex:
            old=next((t for t in self.store.list('tasks') if t.get('idempotency_key')==key and t['status']!='cancelled'),None)
            if old:return old
            task=self.store.put('tasks',{'input':input[:8000],'title':input[:100] or '上传材料','depth':depth,'status':'queued','stage':'queued','attempts':0,'stages':[],'uploads':uploads or [],'idempotency_key':key,'error':None})
        self.wake.set();return task

    def checkpoint(self,id,**values):
        with self.mutex:
            task=self.store.get('tasks',id)
            if task['status']=='cancelled' or self.stop.is_set():
                raise AppError('cancelled','任务已取消')
            task.update(values)
            return self.store.put('tasks',task)

    def stage(self,id,stage,message,status='running'):
        with self.mutex:
            task=self.store.get('tasks',id)
            if task['status']=='cancelled' or self.stop.is_set():
                raise AppError('cancelled','任务已取消或服务正在停止')
            task['stage']=stage
            task.setdefault('stages',[]).append({'stage':stage,'message':message,'status':status,'at':now()})
            return self.store.put('tasks',task)

    def retry(self,id):
        with self.mutex:
            task=self.store.get('tasks',id)
            if not task:raise AppError('not_found','未找到任务',status=404)
            if id in self.active_ids and task['status']=='cancelled':
                raise AppError('task_stopping','任务正在结束当前请求，请稍后继续',status=409)
            if task['status'] in ('running','queued','succeeded'):return task
            task.update(status='queued',error=None,attempts=0)
            self.store.put('tasks',task)
        self.wake.set();return task

    def cancel(self,id):
        with self.mutex:
            t=self.store.get('tasks',id)
            if not t:raise AppError('not_found','未找到任务',status=404)
            if t['status']=='succeeded':raise AppError('task_complete','任务已完成')
            t.update(status='cancelled',ended_at=now())
            t.setdefault('stages',[]).append({'stage':'cancel','status':'cancelled','message':'用户取消；停止后续阶段。正在进行的请求最多等待至超时。','at':now()})
            return self.store.put('tasks',t)

    def attach(self,id,upload):
        with self.mutex:
            t=self.store.get('tasks',id)
            if not t:raise AppError('not_found','未找到任务',status=404)
            if id in self.active_ids:raise AppError('task_stopping','任务正在结束当前请求，请稍后补充材料',status=409)
            if t['status'] in ('running','queued'):raise AppError('task_busy','请先取消或等待当前处理结束',status=409)
            if t['status']=='succeeded':raise AppError('task_complete','该任务已完成；新材料请创建新任务，以保留原分析与知识',status=409)
            t.setdefault('uploads',[]).append(upload)
            for k in ('source','media','analysis','vision_result','transcripts','media_complete','skill_id'):
                t.pop(k,None)
            t.update(status='queued',error=None,attempts=0)
            self.store.put('tasks',t)
        self.wake.set();return t

    def _loop(self):
        while not self.stop.is_set():
            pending=[t for t in reversed(self.store.list('tasks')) if t['status']=='queued']
            if not pending:
                self.wake.wait(1);self.wake.clear();continue
            self.run(pending[0]['id'])

    def run(self,id):
        with self.mutex:
            if id in self.active_ids:return
            self.active_ids.add(id)
        try:
            self._run(id)
        finally:
            with self.mutex:self.active_ids.discard(id)

    def _run(self,id):
        with self.mutex:
            task=self.store.get('tasks',id)
            if not task or task['status']!='queued':return
            task.update(status='running',started_at=task.get('started_at') or now(),attempts=task.get('attempts',0)+1)
            self.store.put('tasks',task)
        for retry in range(3):
            try:
                self._execute(id)
                return
            except Exception as e:
                task=self.store.get('tasks',id)
                if task['status']=='cancelled' or self.stop.is_set():return
                code=getattr(e,'code','internal')
                retryable=getattr(e,'retryable',False)
                message=self.vault.redact(getattr(e,'message','内部处理失败，请查看诊断与任务阶段'))
                task['error']={'code':code,'message':message,'retryable':retryable,'stage':task['stage']}
                if getattr(e,'diagnostic_id',None):task['error']['diagnostic_id']=e.diagnostic_id
                task.setdefault('stages',[]).append({'stage':task['stage'],'status':'failed','message':message,'at':now(),'attempt':retry+1})
                if retryable and retry<2:
                    task['attempts']+=1
                    self.store.put('tasks',task)
                    if self.stop.wait(0.5*(2**retry)):return
                    continue
                task['status']='waiting_config' if code=='configuration' else 'waiting_material' if code.startswith('douyin_') else 'failed'
                task['ended_at']=now()
                self.store.put('tasks',task);return

    def _execute(self,id):
        task=self.store.get('tasks',id)
        folder=self.store.root/'tasks'/id
        folder.mkdir(parents=True,exist_ok=True)
        source=task.get('source')
        if not source:
            self.stage(id,'fetch','获取并保存来源')
            uploads=task.get('uploads',[])
            subtitles=[u for u in uploads if Path(u['path']).suffix.lower() in ('.srt','.vtt','.txt')]
            videos=[u for u in uploads if u not in subtitles]
            if subtitles:
                source=subtitle_source(Path(subtitles[-1]['path']))
                source['title']=subtitles[-1]['name']
                for cue in source['evidence']:cue['path']=subtitles[-1]['name']
                if videos:source['media_path']=videos[-1]['path']
            elif videos:
                source={'platform':'upload','title':videos[-1]['name'],'text':'','evidence':[],'media_path':videos[-1]['path'],'basis':'video_pending','files':[]}
            else:
                url=extract_url(task['input'])
                if urlsplit(url).hostname in ('github.com','www.github.com','raw.githubusercontent.com'):
                    connector=self.github_factory()
                    try:source=connector.snapshot(url,task['depth'])
                    finally:
                        if hasattr(connector,'close'):connector.close()
                    source['basis']='source_files'
                else:
                    source=douyin_source(url,folder)
            source['original_input']=task['input']
            source['acquired_at']=now()
            source['snapshot_hash']=hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            self.store.put('sources',{'id':id,**source})
            self.checkpoint(id,source=source,title=source.get('title',task['title']))
        task=self.store.get('tasks',id)
        if source.get('media_path') and not task.get('media_complete'):
            self.stage(id,'media','检查媒体、分段音频与抽取关键帧')
            media_key=hashlib.sha256(Path(source['media_path']).read_bytes()).hexdigest()[:20]
            prepared=task.get('media') or prepare_media(Path(source['media_path']),folder/'processed'/media_key,task['depth'])
            self.checkpoint(id,media=prepared)
            transcripts=task.get('transcripts',{})
            has_subtitles=source.get('basis') in ('subtitles','text_only')
            if prepared['audio'] and not has_subtitles:
                for i,chunk in enumerate(prepared['chunks']):
                    if str(i) in transcripts:continue
                    self.stage(id,'transcription',f'转写音频段 {i+1}/{len(prepared["chunks"])}，起点 {chunk["time"]} 秒')
                    data=self.models.transcribe(Path(chunk['path']))
                    transcripts[str(i)]={'text':data['text'],'time':chunk['time'],'usage':data.get('usage')}
                    self.checkpoint(id,transcripts=transcripts)
            if transcripts and not has_subtitles:
                source=dict(source)
                source['evidence']=[{'id':f'audio-{i}','time':f'{v["time"]}—{min(v["time"]+120,prepared["duration"])}s','excerpt':v['text'],'kind':'transcript'} for i,v in transcripts.items()]
                source['text']='\n'.join(f"[{e['time']}] {e['excerpt']}" for e in source['evidence'])
                source['basis']='audio_transcript'
            vision=task.get('vision_result')
            vision_config=self.store.get('model_configs','vision') or {}
            if prepared['frames'] and vision_config.get('base_url') and vision_config.get('model'):
                if not vision:
                    self.stage(id,'vision','理解已抽取画面；画面材料将发送到视觉模型服务')
                    text,meta=self.models.vision([{'path':Path(f['path']),'time':f['time']} for f in prepared['frames']])
                    vision={'text':text,**meta}
                    self.checkpoint(id,vision_result=vision)
                source=dict(source)
                source['text']+='\n画面观察：\n'+vision['text']
                source['evidence']=[e for e in source.get('evidence',[]) if e['id']!='frames-observation']+[{'id':'frames-observation','excerpt':vision['text'],'time':', '.join(str(f['time'])+'s' for f in prepared['frames']),'kind':'model_frame_observation','model':vision['model']}]
                source['basis']=('subtitles_and_frames' if has_subtitles else 'transcript_and_frames')
            else:
                source.setdefault('limitations',[]).append('未配置视觉模型，未分析视频画面')
            if not source.get('text','').strip():
                raise AppError('configuration','该媒体没有可用字幕或转写，请配置对应模型或补充字幕')
            source['snapshot_hash']=hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            self.store.put('sources',{'id':id,**source})
            self.checkpoint(id,source=source,media_complete=True)
        task=self.store.get('tasks',id)
        analysis=task.get('analysis')
        if not analysis:
            self.stage(id,'analysis','依据已保存来源分析；材料将发送到设置中的文字模型服务')
            analysis=self.models.analyze(source)
            self.store.put('analyses',{'id':id,**analysis})
            self.checkpoint(id,analysis=analysis)
        self.stage(id,'save','保存分析与知识卡片')
        # Stable task ID makes saving idempotent across process restarts.
        knowledge=self.store.get('knowledge',id) or self.store.put('knowledge',{'id':id,'title':source.get('title','来源知识'),'summary':analysis['overview'],'body':'\n\n'.join(analysis.get('steps',[])),'analysis':analysis,'evidence':source.get('evidence',[]),'source_id':id,'task_id':id,'tags':[],'category':'未分类','archived':False})
        self.stage(id,'complete','分析和知识卡片已保存','succeeded')
        self.checkpoint(id,status='succeeded',knowledge_id=knowledge['id'],error=None,ended_at=now())
