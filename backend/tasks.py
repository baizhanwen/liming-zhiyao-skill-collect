import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .media import douyin_source, extract_url, prepare_media, subtitle_source
from .security import AppError
from .project_links import resolve_project_links
from .store import now
from .task_progress import PHASE_DETAILS, STAGE_PHASES, live_snapshot, safe_observation, safe_progress_record

TERMINAL = {'succeeded','failed','cancelled','waiting_config','waiting_material'}


class TaskRunner:
    def __init__(self,store,vault,models,github_factory):
        self.store,self.vault,self.models,self.github_factory=store,vault,models,github_factory
        self.stop=threading.Event()
        self.wake=threading.Event()
        self.thread=None
        self.mutex=threading.RLock()
        self.active_ids=set()
        self.project_lookup_ids=set()
        self.active_threads={}
        self.run_tokens={}
        self.step_tokens={}
        self.progress_writes={}

    def present_many(self,tasks):
        """Attach an ephemeral server observation; never write on a status read."""
        observed=now()
        pending=[t['id'] for t in reversed(tasks) if t.get('status')=='queued']
        positions={id:i+1 for i,id in enumerate(pending)}
        with self.mutex:
            active={id for id,thread in self.active_threads.items() if thread.is_alive()}
        result=[]
        for task in tasks:
            value={**task,'live':live_snapshot(task,observed,task['id'] in active,positions.get(task['id']))}
            if 'progress' in value:
                safe=safe_progress_record(value['progress'])
                if safe:value['progress']=safe
                else:value.pop('progress',None)
            result.append(value)
        return result

    def present(self,task):
        tasks=self.store.list('tasks')
        # Use the returned value for this task while sharing a FIFO queue view.
        values=[task if t['id']==task['id'] else t for t in tasks]
        if not any(t['id']==task['id'] for t in values):values.append(task)
        return next(t for t in self.present_many(values) if t['id']==task['id'])

    def observer(self,id,stage):
        with self.mutex:
            token=self.run_tokens.get(id)
            step_token=self.step_tokens.get(id)
        return lambda event:self.progress(id,event,stage=stage,token=token,step_token=step_token)

    def progress(self,id,event,*,stage,token,step_token):
        """Only changed, safe facts advance activity; telemetry cannot retry work."""
        value=safe_observation(event)
        if value is None:return False
        try:
            with self.mutex:
                if token is None or self.run_tokens.get(id)!=token or step_token is None or self.step_tokens.get(id)!=step_token:return False
                task=self.store.get('tasks',id)
                if not task:return False
                if task['status']=='cancelled' or self.stop.is_set():
                    raise AppError('cancelled','任务已取消或服务正在停止')
                if task['status']!='running' or task.get('stage')!=stage:return False
                previous=safe_progress_record(task.get('progress')) or {}
                same_phase=previous.get('phase')==value['phase'] and previous.get('stage')==stage
                if same_phase:
                    value['metrics']={**previous.get('metrics',{}),**value['metrics']}
                if same_phase and previous.get('metrics',{})==value['metrics']:
                    return False
                clock=time.monotonic()
                finished='completed' in value['metrics'] and value['metrics'].get('total',0)>0 and value['metrics']['completed']>=value['metrics']['total']
                last=self.progress_writes.get(id)
                if same_phase and not finished and last is not None and clock-last<1:return False
                stamp=now()
                task['progress']={**value,'stage':stage,'at':stamp}
                task['last_activity_at']=stamp
                self.store.put('tasks',task)
                self.progress_writes[id]=clock
                return True
        except AppError as exc:
            if exc.code=='cancelled':raise
            return False
        except Exception:
            # A broken progress observer must not issue another supplier call.
            return False

    def recover(self):
        for task in self.store.list('tasks'):
            if task['status'] in ('running','queued'):
                uncertain=task.get('stage') in ('analysis','vision','transcription') and task['status']=='running'
                stamp=now()
                task.update(status='waiting_config' if uncertain else 'queued',error={'code':'interrupted_request','message':'服务中断时有模型请求未确认结果，请核对配置后手动继续；可能再次产生调用费用','retryable':False} if uncertain else None)
                task['last_activity_at']=stamp
                if not uncertain:
                    task.update(stage='queued',stage_started_at=stamp,
                        progress={'stage':'queued','phase':'queued','detail':PHASE_DETAILS['queued'],'metrics':{},'at':stamp})
                task.setdefault('stages',[]).append({'stage':'recovery','status':task['status'],'message':'服务重启，复用已保存检查点；未确认的模型请求需手动继续' if uncertain else '服务重启，复用已保存检查点继续','at':stamp})
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
        automatic_video=False
        if not uploads:
            try:
                host=urlsplit(extract_url(input)).hostname or ''
                automatic_video=host in ('douyin.com','iesdouyin.com') or host.endswith(('.douyin.com','.iesdouyin.com'))
            except AppError:pass
        with self.mutex:
            matches=[t for t in self.store.list('tasks') if t.get('idempotency_key')==key and t['status']!='cancelled']
            # A prior supplement changes the material, not the original URL's
            # default dedup key. Never return a text-only success as a new
            # automatic-video request; preserve it as a separate historical task.
            if automatic_video and not idempotency_key:
                matches=[t for t in matches if not t.get('uploads') and (
                    not t.get('source') or (t['source'].get('platform')=='douyin'
                    and t['source'].get('media_path') and t['source'].get('basis') not in ('text_only','subtitles')))]
            old=next(iter(matches),None)
            if old:
                if automatic_video and not idempotency_key and old['status'] in ('failed','waiting_material','waiting_config'):
                    return self.retry(old['id'])
                return old
            task=self.store.put('tasks',{'input':input[:8000],'title':input[:100] or '上传材料','depth':depth,'status':'queued','stage':'queued','attempts':0,'stages':[],'uploads':uploads or [],'idempotency_key':key,'error':None})
        self.wake.set();return task

    def checkpoint(self,id,**values):
        with self.mutex:
            task=self.store.get('tasks',id)
            if task['status']=='cancelled' or self.stop.is_set():
                raise AppError('cancelled','任务已取消')
            if any(task.get(k)!=v for k,v in values.items()):
                task['last_activity_at']=now()
            task.update(values)
            return self.store.put('tasks',task)

    def stage(self,id,stage,message,status='running'):
        with self.mutex:
            task=self.store.get('tasks',id)
            if task['status']=='cancelled' or self.stop.is_set():
                raise AppError('cancelled','任务已取消或服务正在停止')
            stamp=now()
            task['stage']=stage
            task['stage_started_at']=stamp
            task['last_activity_at']=stamp
            self.progress_writes.pop(id,None)
            phase=STAGE_PHASES.get(stage,'')
            task['progress']={'stage':stage,'phase':phase,'detail':PHASE_DETAILS.get(phase,message),'metrics':{},'at':stamp}
            task.setdefault('stages',[]).append({'stage':stage,'message':message,'status':status,'at':stamp,'attempt':task.get('attempts',0)})
            result=self.store.put('tasks',task)
            self.step_tokens[id]=uuid.uuid4().hex
            return result

    def retry(self,id):
        with self.mutex:
            task=self.store.get('tasks',id)
            if not task:raise AppError('not_found','未找到任务',status=404)
            if id in self.active_ids and task['status']=='cancelled':
                raise AppError('task_stopping','任务正在结束当前请求，请稍后继续',status=409)
            if task['status'] in ('running','queued','succeeded'):return task
            stamp=now()
            task.update(status='queued',stage='queued',error=None,attempts=0,stage_started_at=stamp,last_activity_at=stamp)
            task.pop('last_error',None)
            task['progress']={'stage':'queued','phase':'queued','detail':PHASE_DETAILS['queued'],'metrics':{},'at':stamp}
            task.setdefault('stages',[]).append({'stage':'queued','status':'queued','message':'任务已重新排队，复用已保存检查点','at':stamp})
            self.store.put('tasks',task)
        self.wake.set();return task

    def cancel(self,id):
        with self.mutex:
            t=self.store.get('tasks',id)
            if not t:raise AppError('not_found','未找到任务',status=404)
            if t['status']=='succeeded':raise AppError('task_complete','任务已完成')
            t.update(status='cancelled',ended_at=now(),last_activity_at=now())
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
            for k in ('source','media','analysis','vision_result','transcripts','media_complete','skill_id','project_links','progress','last_error','stage_started_at'):
                t.pop(k,None)
            t.update(status='queued',stage='queued',error=None,attempts=0,last_activity_at=now())
            self.store.put('tasks',t)
        self.wake.set();return t

    def _project_links(self,source,analysis):
        result=resolve_project_links(source,analysis,self.github_factory)
        if analysis.get('project_mention_warnings'):
            result.setdefault('errors',[]).append({'code':'project_mentions_format',
                'message':'部分项目线索格式无效或超量；未重复调用文字模型，对应线索未检索。','retryable':False})
            result.setdefault('warnings',[]).append('主分析已保留；项目识别存在格式限制，不能据此断言视频未提及项目。')
            result['status']='partial' if result.get('status') in ('found','partial') else 'unavailable'
        return {**result,'source_snapshot_hash':source.get('snapshot_hash'),'basis':source.get('basis'),
                'checked_at':now()}

    def refresh_project_links(self,id):
        """Retry read-only repository lookup without repeating paid media/model stages."""
        with self.mutex:
            task=self.store.get('tasks',id)
            if not task:raise AppError('not_found','未找到任务',status=404)
            if id in self.active_ids or id in self.project_lookup_ids:
                raise AppError('task_busy','项目检索正在进行，请等待当前操作结束',status=409)
            if task.get('status')!='succeeded' or not task.get('analysis') or not task.get('source'):
                raise AppError('analysis_missing','请先完成视频或相关材料分析',status=409)
            source=task['source']
            if source.get('basis')=='source_files':
                raise AppError('project_source','仓库来源无需再次按视频检索项目',status=409)
            self.project_lookup_ids.add(id)
        try:
            result=self._project_links(source,task['analysis'])
            with self.mutex:
                current=self.store.get('tasks',id)
                if (current.get('source') or {}).get('snapshot_hash')!=source.get('snapshot_hash'):
                    raise AppError('source_changed','材料已变化，请在新分析完成后重新检索',status=409)
                current['project_links']=result
                current.setdefault('stages',[]).append({'stage':'project_links',
                    'status':'failed' if result.get('status')=='unavailable' else 'succeeded',
                    'message':'项目检索已结束；结果与限制单独保存，未重复调用转写、视觉或文字模型','at':now()})
                current=self.store.put('tasks',current)
                knowledge=self.store.get('knowledge',current.get('knowledge_id',id))
                if knowledge:self.store.put('knowledge',{**knowledge,'project_links':result})
                return current
        finally:
            with self.mutex:self.project_lookup_ids.discard(id)

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
            self.active_threads[id]=threading.current_thread()
            self.run_tokens[id]=uuid.uuid4().hex
            self.progress_writes.pop(id,None)
        try:
            self._run(id)
        finally:
            with self.mutex:
                self.active_ids.discard(id)
                self.active_threads.pop(id,None)
                self.run_tokens.pop(id,None)
                self.step_tokens.pop(id,None)
                self.progress_writes.pop(id,None)

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
                capture=getattr(e,'capture_diagnostic',None)
                if isinstance(capture,dict):
                    safe={k:capture[k] for k in ('requests','downloaded_bytes','blocked_requests',
                        'media_responses','complete_bound_media','proxy_connections','proxy_bytes',
                        'proxy_denied') if type(capture.get(k)) is int}
                    for name in ('http_status_counts','error_counts'):
                        if isinstance(capture.get(name),dict):
                            safe[name]={str(k):v for k,v in capture[name].items() if
                                re.fullmatch(r'[A-Za-z0-9_]{1,48}',str(k)) and type(v) is int}
                    task['error']['capture_diagnostic']=safe
                task.setdefault('stages',[]).append({'stage':task['stage'],'status':'failed','message':message,'at':now(),'attempt':retry+1})
                if retryable and retry<2:
                    task['attempts']+=1
                    stamp=now()
                    task['last_error']=task['error']
                    task['error']=None
                    task['last_activity_at']=stamp
                    task['progress']={'stage':task['stage'],'phase':'retry_wait',
                        'detail':PHASE_DETAILS['retry_wait'],'metrics':{'retry_attempt':retry+2,'max_attempts':3},'at':stamp}
                    task.setdefault('stages',[]).append({'stage':task['stage'],'status':'retrying',
                        'message':f'本轮自动尝试 {retry+2}/3，累计已尝试 {task["attempts"]} 次；上一轮错误已保留在处理记录','at':stamp,'attempt':task['attempts']})
                    self.store.put('tasks',task)
                    if self.stop.wait(0.5*(2**retry)):return
                    continue
                task['status']='waiting_config' if code=='configuration' else 'waiting_material' if code.startswith('douyin_') else 'failed'
                task['ended_at']=now()
                task['last_activity_at']=task['ended_at']
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
                    try:
                        if hasattr(connector,'set_progress_callback'):
                            connector.set_progress_callback(self.observer(id,'fetch'))
                        source=connector.snapshot(url,task['depth'])
                    finally:
                        if hasattr(connector,'close'):connector.close()
                    source['basis']='source_files'
                else:
                    self.stage(id,'fetch','自动读取抖音原片；由项目采集器处理，无需 Codex 或手动下载')
                    source=douyin_source(url,folder,cancelled=lambda:self.stop.is_set() or
                        (self.store.get('tasks',id) or {}).get('status')=='cancelled',
                        progress_callback=self.observer(id,'fetch'))
            source['original_input']=task['input']
            source['acquired_at']=now()
            source['snapshot_hash']=hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            self.store.put('sources',{'id':id,**source})
            self.checkpoint(id,source=source,title=source.get('title',task['title']))
        task=self.store.get('tasks',id)
        if source.get('media_path') and not task.get('media_complete'):
            self.stage(id,'media','检查媒体、分段音频与抽取关键帧')
            media_key=hashlib.sha256(Path(source['media_path']).read_bytes()).hexdigest()[:20]
            prepared=task.get('media') or prepare_media(Path(source['media_path']),folder/'processed'/media_key,task['depth'],
                progress_callback=self.observer(id,'media'))
            self.checkpoint(id,media=prepared)
            transcripts=task.get('transcripts',{})
            has_subtitles=source.get('basis') in ('subtitles','text_only')
            if prepared['audio'] and not has_subtitles:
                for i,chunk in enumerate(prepared['chunks']):
                    if str(i) in transcripts:continue
                    self.stage(id,'transcription',f'转写音频段 {i+1}/{len(prepared["chunks"])}，起点 {chunk["time"]} 秒')
                    observation=self.observer(id,'transcription')
                    observation({'phase':'model_transcription','transcribed_chunks':len(transcripts),'total_chunks':len(prepared['chunks'])})
                    data=self.models.transcribe(Path(chunk['path']))
                    transcripts[str(i)]={'text':data['text'],'time':chunk['time'],'usage':data.get('usage')}
                    self.checkpoint(id,transcripts=transcripts)
                    observation({'phase':'transcription_complete','transcribed_chunks':len(transcripts),'total_chunks':len(prepared['chunks'])})
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
                    observation=self.observer(id,'vision')
                    observation({'phase':'model_vision','sampled_frames':len(prepared['frames']),'total_frames':len(prepared['frames'])})
                    text,meta=self.models.vision([{'path':Path(f['path']),'time':f['time']} for f in prepared['frames']])
                    vision={'text':text,**meta}
                    self.checkpoint(id,vision_result=vision)
                    observation({'phase':'vision_complete','sampled_frames':len(prepared['frames']),'total_frames':len(prepared['frames'])})
                source=dict(source)
                source['text']+='\n画面观察：\n'+vision['text']
                source['evidence']=[e for e in source.get('evidence',[]) if e['id']!='frames-observation']+[{'id':'frames-observation','excerpt':vision['text'],'time':', '.join(str(f['time'])+'s' for f in prepared['frames']),'kind':'model_frame_observation','model':vision['model']}]
                source['basis']=('subtitles_and_frames' if has_subtitles else 'transcript_and_frames' if transcripts else 'frames_only')
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
        task=self.store.get('tasks',id)
        project_links=task.get('project_links')
        if source.get('basis')!='source_files' and not project_links:
            self.stage(id,'project_links','根据保存的证据识别并检索 GitHub 项目；候选不代表已确认对应')
            project_links=self._project_links(source,analysis)
            self.checkpoint(id,project_links=project_links)
        self.stage(id,'save','保存分析与知识卡片')
        # Stable task ID makes saving idempotent across process restarts.
        knowledge=self.store.get('knowledge',id) or self.store.put('knowledge',{'id':id,'title':source.get('title','来源知识'),'summary':analysis['overview'],'body':'\n\n'.join(analysis.get('steps',[])),'analysis':analysis,'evidence':source.get('evidence',[]),'source_id':id,'task_id':id,'tags':[],'category':'未分类','archived':False})
        if project_links and knowledge.get('project_links')!=project_links:
            self.store.put('knowledge',{**knowledge,'project_links':project_links})
        self.stage(id,'complete','分析和知识卡片已保存','succeeded')
        self.checkpoint(id,status='succeeded',knowledge_id=knowledge['id'],error=None,ended_at=now())
