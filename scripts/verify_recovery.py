"""Real process kill/restart with an explicitly synthetic, local model HTTP server."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parent.parent
SAMPLE={'overview':'[契约] 进程恢复样例','purpose':'恢复验收','audience':'测试维护者','steps':['读取证据'],'dependencies':[],'limitations':['本地模拟模型，不是外部模型验收'],'methods':[],'claims':[{'text':'测试字幕保留','evidence_ids':['sub-1'],'kind':'source_claim'}]}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


def main():
    called=threading.Event();release=threading.Event();calls=[]
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']));calls.append('explicit_local_contract')
            called.set()
            if len(calls)==1:release.wait(15)
            body=json.dumps({'model':'local-contract','choices':[{'message':{'content':json.dumps(SAMPLE,ensure_ascii=False)}}]}).encode()
            try:
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
    provider=ThreadingHTTPServer(('127.0.0.1',0),Model);provider.daemon_threads=True
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    port=free_port();base=f'http://127.0.0.1:{port}'
    process=None
    def wait_for(fn,timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                result=fn()
                if result:return result
            except (httpx.HTTPError,KeyError):pass
            time.sleep(.1)
        raise RuntimeError('recovery verification timed out')
    with tempfile.TemporaryDirectory(prefix='skill-collect-recovery-') as tmp:
        def start():
            p=subprocess.Popen([sys.executable,'-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=ROOT,env={**os.environ,'SKILL_COLLECT_DATA':tmp},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            wait_for(lambda:httpx.get(base+'/api/status',timeout=1).status_code==200)
            return p
        try:
            process=start()
            with httpx.Client(base_url=base,timeout=5,trust_env=False) as c:
                r=c.put('/api/settings',json={'models':{'text':{'base_url':f'http://127.0.0.1:{provider.server_port}/v1','model':'local-contract'}}});r.raise_for_status()
                r=c.post('/api/uploads',files={'file':('recovery-fixture.srt',b'1\n00:00:00,000 --> 00:00:02,000\nExplicit recovery fixture.\n')});r.raise_for_status();id=r.json()['id']
                assert called.wait(10)
                before=c.get('/api/tasks/'+id).json();assert before['stage']=='analysis' and before['status']=='running'
                process.kill();process.wait(timeout=10);release.set();process=start()
                recovered=c.get('/api/tasks/'+id).json()
                assert recovered['status']=='waiting_config' and recovered['error']['code']=='interrupted_request'
                c.post('/api/tasks/'+id+'/retry').raise_for_status()
                final=wait_for(lambda:(t if (t:=c.get('/api/tasks/'+id).json())['status']=='succeeded' else None))
                knowledge=c.get('/api/knowledge').json();assert len(knowledge)==1
                assert final['source']['snapshot_hash']==before['source']['snapshot_hash']
                result={'status':'passed','test_type':'real_uvicorn_process_kill_restart_with_explicit_local_mock_model','process_killed_during':'analysis','recovered_status':recovered['status'],'recovered_error':recovered['error']['code'],'final_status':final['status'],'source_hash_preserved':True,'knowledge_count':len(knowledge),'mock_model_calls':len(calls),'real_external_model':False}
                (ROOT/'artifacts/verification/process-recovery.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps(result,ensure_ascii=False,indent=2))
        finally:
            release.set()
            if process and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            provider.shutdown();provider.server_close()


if __name__=='__main__':main()
