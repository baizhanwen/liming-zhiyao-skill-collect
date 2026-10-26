import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .security import AppError, fetch_public

MAX_UPLOAD = 100*1024*1024
MAX_DURATION = 1200
ALLOWED = {'.mp4','.mov','.mkv','.webm','.m4v','.mp3','.wav','.m4a','.srt','.vtt','.txt'}


def extract_url(text):
    m=re.search(r'https?://[^\s<>"，。；、]+',text)
    if not m:
        raise AppError('url','请提供 GitHub 或抖音链接，或上传视频/字幕')
    return m.group(0).rstrip(').!）]')


def subtitle_source(path):
    try:
        text=path.read_text('utf-8-sig')
    except UnicodeDecodeError:
        raise AppError('subtitle_encoding','字幕需为 UTF-8 编码')
    if len(text)>1_000_000:
        raise AppError('subtitle_limit','字幕文本过长，请分段上传')
    cues=[]
    for block in re.split(r'\n\s*\n',text.replace('\r','')):
        lines=block.strip().splitlines()
        stamp=next((i for i,l in enumerate(lines) if '-->' in l),None)
        if stamp is not None:
            at=lines[stamp].strip()
            match=re.fullmatch(r'((?:\d{2}:)?\d{2}:\d{2}[,.]\d{3})\s+-->\s+((?:\d{2}:)?\d{2}:\d{2}[,.]\d{3})(?:\s+[^\n]*)?',at)
            def seconds(value):
                parts=value.replace(',','.').split(':')
                return sum(float(v)*60**i for i,v in enumerate(reversed(parts)))
            if not match or seconds(match[2])<=seconds(match[1]):
                raise AppError('subtitle_time','字幕时间格式无效或结束时间不晚于开始时间')
            content=re.sub(r'<[^>]+>','', ' '.join(lines[stamp+1:]))
            if content:
                cues.append({'id':f'sub-{len(cues)+1}','time':at,'excerpt':content,'kind':'transcript','path':path.name})
    if path.suffix.lower() in ('.srt','.vtt') and not cues:
        raise AppError('subtitle_format','未找到有效的 SRT/VTT 时间片段')
    if not cues:
        cues=[{'id':'text-1','excerpt':text[:50000],'kind':'text','path':path.name}]
    if len(cues)>5000:
        raise AppError('subtitle_limit','字幕片段超过 5000 条，请分段上传')
    return {'title':path.name,'text':'\n'.join(f"[{c.get('time','纯文本')}] {c['excerpt']}" for c in cues), 'evidence':cues,'basis':'subtitles' if path.suffix.lower()!='.txt' else 'text_only','platform':'upload','files':[],'limitations':['仅基于上传文字，未分析视频画面']}


def run_media(args,timeout=90):
    try:
        return subprocess.run(args,check=True,capture_output=True,timeout=timeout).stdout
    except FileNotFoundError:
        raise AppError('ffmpeg_missing','未安装 FFmpeg/FFprobe，请在诊断页查看安装说明')
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired):
        raise AppError('media_processing','媒体处理失败或超时，请检查文件格式与时长')


def inspect_video(path):
    info=json.loads(run_media(['ffprobe','-v','error','-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska,webm,wav,mp3','-show_format','-show_streams','-of','json',str(path)]))
    try:
        duration=float(info['format']['duration'])
        if duration<=0 or duration>MAX_DURATION:
            raise ValueError()
    except (KeyError,ValueError):
        raise AppError('duration','媒体时长须在 0—20 分钟范围内')
    return {'duration':duration,'video':any(s['codec_type']=='video' for s in info['streams']),'audio':any(s['codec_type']=='audio' for s in info['streams'])}


def prepare_media(path,folder,depth='quick'):
    meta=inspect_video(path)
    folder.mkdir(parents=True,exist_ok=True)
    chunks=[]
    if meta['audio']:
        for index,start in enumerate(range(0,int(meta['duration'])+1,120)):
            if start>=meta['duration']:
                break
            target=folder/f'audio-{index}.wav'
            if not target.exists():
                run_media(['ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska,webm,wav,mp3','-i',str(path),'-ss',str(start),'-t','120','-vn','-ar','16000','-ac','1',str(target)])
            chunks.append({'path':str(target),'time':start})
    frames=[]
    if meta['video']:
        count=4 if depth=='quick' else 8
        for i in range(count):
            at=round(meta['duration']*(i+0.5)/count,2)
            target=folder/f'frame-{i}.jpg'
            if not target.exists():
                run_media(['ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska,webm,wav,mp3','-ss',str(at),'-i',str(path),'-frames:v','1','-vf','scale=640:-2',str(target)])
            frames.append({'path':str(target),'time':at})
    return {**meta,'chunks':chunks,'frames':frames}


def douyin_source(url,folder):
    host=urlsplit(url).hostname or ''
    if not (host=='douyin.com' or host.endswith('.douyin.com') or host=='iesdouyin.com' or host.endswith('.iesdouyin.com')):
        raise AppError('unsupported_source','当前仅支持 GitHub、抖音链接和文件上传')
    try:
        payload,final,headers=fetch_public(url)
    except AppError as e:
        raise AppError('douyin_fetch',f'抖音来源获取未完成：{e.message}；可在原任务补传视频或字幕',e.retryable)
    except Exception:
        raise AppError('douyin_fetch','抖音公开页获取失败，请补传视频或字幕；不绕过登录或访问限制')
    page=payload.decode('utf-8','replace')
    title=re.search(r'<title[^>]*>(.*?)</title>',page,re.S|re.I)
    title=html.unescape(title.group(1)).strip() if title else '抖音视频'
    # Only accept an explicitly published play URL from public page data.
    decoded=html.unescape(unquote(page)).replace('\\u002F','/').replace('\\/','/')
    matches=re.findall(r'"play_addr"\s*:\s*\{[^{}]{0,4000}?"url_list"\s*:\s*\[\s*"(https?[^"\s]+)"',decoded)
    if not matches:
        raise AppError('douyin_material','公开页未提供可用媒体（可能需要登录或受访问限制）；请在原任务补传视频或字幕')
    media_url=matches[0]
    media,_,_=fetch_public(media_url,MAX_UPLOAD)
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/'douyin.mp4';path.write_bytes(media)
    return {'platform':'douyin','url':url,'resolved_url':final,'title':title,'text':'','evidence':[],'media_path':str(path),'basis':'video_pending','files':[]}
