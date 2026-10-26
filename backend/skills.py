import base64
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

from .security import AppError
from .store import now

MAX_FILES = 250
MAX_PACKAGE = 20 * 1024 * 1024


def digest(files):
    return hashlib.sha256(json.dumps({p:hashlib.sha256(b).hexdigest() for p,b in sorted(files.items())}, sort_keys=True).encode()).hexdigest()


def safe_path(name):
    p = PurePosixPath(name)
    if not name or '\\' in name or p.is_absolute() or '..' in p.parts or ':' in name or '\x00' in name or str(p)!=name.rstrip('/'):
        raise AppError('unsafe_package','技能包包含不安全路径')
    return p


def unpack(data):
    if len(data) > MAX_PACKAGE:
        raise AppError('package_limit','技能包不能超过 20 MB')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            entries = z.infolist()
            if len(entries)>MAX_FILES or sum(i.file_size for i in entries)>MAX_PACKAGE:
                raise AppError('package_limit','技能包文件过多或解压后超过 20 MB')
            result = {}
            for item in entries:
                safe_path(item.filename)
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise AppError('unsafe_package','技能包不允许符号链接')
                if item.is_dir():
                    continue
                if item.filename in result:
                    raise AppError('unsafe_package','技能包存在重复文件路径')
                result[item.filename] = z.read(item)
    except (zipfile.BadZipFile, RuntimeError, OSError) as e:
        raise AppError('invalid_package','无法读取 ZIP 技能包') from e
    if not result:
        raise AppError('invalid_package','技能包为空')
    if 'SKILL.md' not in result:
        roots = {p.split('/')[0] for p in result}
        if len(roots) != 1:
            raise AppError('invalid_package','技能包必须有单一根目录和 SKILL.md')
        prefix = next(iter(roots)) + '/'
        result = {p[len(prefix):]: b for p,b in result.items()}
    if 'SKILL.md' not in result:
        raise AppError('invalid_package','技能包缺少 SKILL.md')
    manifest = {}
    if 'manifest.json' in result:
        try:
            manifest = json.loads(result.pop('manifest.json'))
            if manifest.get('format_version') != 1:
                raise ValueError('version')
            listed = {v['path']:v for v in manifest['files']}
            if len(listed)!=len(manifest['files']) or set(listed) != set(result):
                raise ValueError('file list')
            for p,b in result.items():
                safe_path(p)
                if hashlib.sha256(b).hexdigest()!=listed[p]['sha256'] or (listed[p].get('size') is not None and listed[p]['size']!=len(b)):
                    raise ValueError('checksum')
            if manifest.get('content_hash') and manifest['content_hash']!=digest(result):
                raise ValueError('content hash')
        except (ValueError,KeyError,TypeError) as e:
            raise AppError('package_integrity','技能包清单或内容哈希校验失败') from e
    try:
        result['SKILL.md'].decode('utf-8')
    except UnicodeDecodeError:
        raise AppError('invalid_package','SKILL.md 必须是 UTF-8 文本')
    return result,manifest


class SkillService:
    def __init__(self,store):
        self.store = store

    def get(self,id):
        s = self.store.get('skills',id)
        if not s:
            raise AppError('not_found','未找到技能',status=404)
        return s

    def _files(self,skill):
        result = {}
        for f in skill.get('files',[]):
            p = str(safe_path(f['path']))
            if p == 'manifest.json':
                continue
            try:
                result[p] = base64.b64decode(f['content'],validate=True) if f.get('encoding')=='base64' else f['content'].encode()
            except (ValueError,KeyError):
                raise AppError('invalid_file','附属文件编码无效')
        result['SKILL.md'] = skill['body'].encode()
        if len(result)>MAX_FILES or sum(map(len,result.values()))>MAX_PACKAGE:
            raise AppError('package_limit','技能文件超出限制')
        return result

    def create(self, value):
        body = value.get('body','')
        if not isinstance(body,str) or not body.strip() or len(body)>2_000_000:
            raise AppError('invalid_skill','请填写技能正文')
        if not isinstance(value.get('name',''),str) or not isinstance(value.get('description',''),str) or not isinstance(value.get('tags',[]),list):
            raise AppError('invalid_skill','技能名称、简介和标签格式无效')
        skill = {k:value[k] for k in ('name','description','body','tags','category','files','origin','source','origin_key') if k in value}
        skill.update(name=skill.get('name','未命名技能')[:150], status='draft',version=1,verification_records=[],archived=False)
        return self._save(skill)

    def _save(self, skill):
        files = self._files(skill)
        skill['content_hash'] = digest(files)
        skill['files'] = [{'path':p,'content':base64.b64encode(b).decode(),'encoding':'base64','sha256':hashlib.sha256(b).hexdigest(),'size':len(b)} for p,b in files.items()]
        saved = self.store.put('skills',skill)
        self.store.put('skill_versions',{'skill_id':saved['id'],'version':saved['version'],'content_hash':saved['content_hash'],'snapshot':saved})
        return saved

    def update(self,id, value):
        with self.store.lock:
            return self._update(id,value)

    def _update(self,id, value):
        old = self.get(id)
        if value.get('expected_version') is not None and value['expected_version']!=old['version']:
            raise AppError('version_conflict','技能已在其他操作中更新，请重新打开后合并修改',status=409)
        editable = ('name','description','body','tags','category','files','archived','origin','source','origin_key','status')
        if value.get('status') not in (None,'draft','analyzed','disabled'):
            # Existing verified state in an edit payload is never a grant of verification.
            if value.get('status')!='verified' or old.get('status')!='verified':
                raise AppError('verification_evidence','已验证状态只能通过实际验证记录获得')
            value={k:v for k,v in value.items() if k!='status'}
        changed = {k:value[k] for k in editable if k in value}
        if all(old.get(k)==v for k,v in changed.items()):
            return old
        old.update(changed)
        if not isinstance(old.get('body'),str) or not old['body'].strip() or len(old['body'])>2_000_000:
            raise AppError('invalid_skill','技能正文不能为空')
        old.update(version=old['version']+1,status=changed.get('status','draft'))
        return self._save(old)

    def import_zip(self,data,origin=None):
        files,manifest = unpack(data)
        body = files['SKILL.md'].decode()
        name = manifest.get('name') or re.search(r'^name:\s*[\'\"]?(.+?)[\'\"]?\s*$',body,re.M)
        if not isinstance(name,str):
            name = name.group(1) if name else '导入技能'
        source = origin or manifest.get('source') or {'platform':'upload'}
        if isinstance(source,str):
            source = {'url':source}
        exported_id=manifest.get('skill_id')
        if exported_id:
            local=self.store.get('skills',str(exported_id))
            if local and local.get('content_hash')==digest(files) and (not origin or local.get('source')==source):
                return local
        key = source.get('origin_key') or '|'.join(str(source.get(k,'')) for k in ('platform','repository','path'))
        if not source.get('repository') and not source.get('path') and not source.get('origin_key'):
            key = ('local:'+str(source['id'])) if source.get('platform')=='local' and source.get('id') else 'package:' + digest(files)
        values = {'name':name,'description':manifest.get('description','从完整技能包导入，尚未执行验证'),'body':body,'source':source,'origin':source,'origin_key':key,
                  'files':[{'path':p,'content':base64.b64encode(b).decode(),'encoding':'base64'} for p,b in files.items()]}
        existing = next((s for s in self.store.list('skills') if s.get('origin_key')==key),None)
        if existing:
            # Content identical re-import is idempotent, including metadata.
            return existing if existing['content_hash']==digest(files) else self.update(existing['id'],values)
        return self.create(values)

    def export_zip(self,id):
        skill = self.get(id)
        files = self._files(skill)
        manifest = {'format_version':1,'skill_id':id,'name':skill['name'],'description':skill.get('description',''),'version':skill['version'],
                    'source':skill.get('source') or skill.get('origin') or {'platform':'local','id':id},'content_hash':digest(files),
                    'files':[{'path':p,'sha256':hashlib.sha256(b).hexdigest(),'size':len(b)} for p,b in sorted(files.items())]}
        slug = re.sub(r'[^a-zA-Z0-9_-]+','-',skill['name']).strip('-') or 'skill'
        out = io.BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
            for p,b in files.items():
                z.writestr(slug+'/'+p,b)
            z.writestr(slug+'/manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        return out.getvalue()

    def verify(self,id,value):
        s = self.get(id)
        if value.get('result') not in ('passed','failed','not_run') or not str(value.get('evidence','')).strip():
            raise AppError('verification_evidence','请提供实际验证证据及结果')
        record = {'result':value['result'],'notes':value.get('notes',''),'evidence':value['evidence'],'at':now(),'version':s['version'],'content_hash':s['content_hash'],'type':'user_record'}
        s.setdefault('verification_records',[]).append(record)
        s['status'] = 'verified' if value['result']=='passed' else 'analyzed' if value['result']=='failed' else 'draft'
        return self.store.put('skills',s)
