import html
import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit, urlunsplit

from .security import AppError, fetch_public

MAX_UPLOAD = 100*1024*1024
MAX_DURATION = 1200
ALLOWED = {'.mp4','.mov','.mkv','.webm','.m4v','.mp3','.wav','.m4a','.srt','.vtt','.txt'}
_PROGRESS_PHASES = {'public_page', 'browser', 'validating_media', 'merging_media', 'audio_chunks', 'frames'}
_PROGRESS_COUNTS = {'completed', 'total', 'requests', 'downloaded_bytes', 'proxy_connections',
                    'proxy_bytes', 'media_responses', 'complete_bound_media'}


def _progress_count(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _notify_progress(callback, phase, **counts):
    """Observability is optional; cancellation remains a business control signal."""
    if callback is None or phase not in _PROGRESS_PHASES:
        return
    event = {'phase': phase, **{key: value for key, value in counts.items()
                             if key in _PROGRESS_COUNTS and _progress_count(value)}}
    try:
        callback(event)
    except AppError as exc:
        if exc.code == 'cancelled':
            raise
    except Exception:
        pass


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
        if not math.isfinite(duration) or duration<=0 or duration>MAX_DURATION:
            raise ValueError()
    except (KeyError,ValueError):
        raise AppError('duration','媒体时长须在 0—20 分钟范围内')
    return {'duration':duration,'video':any(s['codec_type']=='video' for s in info['streams']),'audio':any(s['codec_type']=='audio' for s in info['streams'])}


def prepare_media(path,folder,depth='quick',*,progress_callback=None):
    _notify_progress(progress_callback, 'validating_media', completed=0, total=1)
    meta=inspect_video(path)
    _notify_progress(progress_callback, 'validating_media', completed=1, total=1)
    folder.mkdir(parents=True,exist_ok=True)
    chunks=[]
    if meta['audio']:
        starts=[start for start in range(0,int(meta['duration'])+1,120) if start<meta['duration']]
        _notify_progress(progress_callback, 'audio_chunks', completed=0, total=len(starts))
        for index,start in enumerate(starts):
            target=folder/f'audio-{index}.wav'
            if not target.exists():
                run_media(['ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska,webm,wav,mp3','-i',str(path),'-ss',str(start),'-t','120','-vn','-ar','16000','-ac','1',str(target)])
            chunks.append({'path':str(target),'time':start})
            _notify_progress(progress_callback, 'audio_chunks', completed=len(chunks), total=len(starts))
    frames=[]
    if meta['video']:
        count=4 if depth=='quick' else 8
        _notify_progress(progress_callback, 'frames', completed=0, total=count)
        for i in range(count):
            at=round(meta['duration']*(i+0.5)/count,2)
            target=folder/f'frame-{i}.jpg'
            if not target.exists():
                run_media(['ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska,webm,wav,mp3','-ss',str(at),'-i',str(path),'-frames:v','1','-vf','scale=640:-2',str(target)])
            frames.append({'path':str(target),'time':at})
            _notify_progress(progress_callback, 'frames', completed=len(frames), total=count)
    return {**meta,'chunks':chunks,'frames':frames}


_MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')


def _douyin_host(host):
    return any(host == domain or host.endswith('.' + domain) for domain in ('douyin.com', 'iesdouyin.com'))


def _video_id(url):
    try:
        match = re.search(r'/(?:share/)?video/(\d+)(?:/|$)', urlsplit(url).path)
    except ValueError:
        return None
    return match[1] if match else None


class _PublicPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.current, self.metadata, self.videos = [], None, {}, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.current = (attrs, [])
        elif tag == 'meta':
            self.metadata[attrs.get('property', attrs.get('name', ''))] = attrs.get('content', '')
        elif tag in ('video', 'source'):
            self.videos.append(attrs)

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            self.scripts.append((self.current[0], ''.join(self.current[1])))
            self.current = None


def _published_media(page, video_id):
    """Parse static public data only; never execute scripts or solve challenges.

    Only media belonging to an explicit matching item ID is considered. A nearby
    recommendation, cover image, platform AI summary or an invented API URL is
    never a substitute for the requested video's media.
    """
    parser = _PublicPage()
    parser.feed(page)
    documents = []
    for attrs, script in parser.scripts:
        try:
            if attrs.get('id') == 'RENDER_DATA':
                documents.append(json.loads(unquote(script)))
            elif attrs.get('type') == 'application/json' or attrs.get('id') == '__NEXT_DATA__':
                documents.append(json.loads(script))
            else:
                match = re.search(r'(?:window\.)?_ROUTER_DATA\s*=\s*', script)
                if match:
                    documents.append(json.JSONDecoder().raw_decode(script[match.end():])[0])
        except (ValueError, TypeError, RecursionError):
            continue
    candidates = []

    def add(value, method):
        if isinstance(value, dict):
            for key in ('url_list', 'urlList'):
                for item in value.get(key, []) if isinstance(value.get(key), list) else []:
                    add(item, method)
        elif isinstance(value, str):
            # Preserve the published path/query exactly, including watermark and
            # quality parameters. Only an explicitly supplied HTTPS URL is usable.
            try:
                p = urlsplit(value)
            except ValueError:
                return
            if p.scheme == 'https' and p.hostname and not p.username and not p.password:
                if all(value != old[0] for old in candidates):
                    candidates.append((value, method))

    def collect(video):
        if not isinstance(video, dict):
            return
        for key in ('play_addr', 'playAddr', 'download_addr', 'downloadAddr', 'play_api'):
            add(video.get(key), 'public_item_' + key)
        for key in ('bit_rate', 'bitRate'):
            for variant in video.get(key, []) if isinstance(video.get(key), list) else []:
                if isinstance(variant, dict):
                    for address in ('play_addr', 'playAddr'):
                        add(variant.get(address), 'public_item_' + key)

    # Iterative traversal also bounds deeply nested, untrusted JSON documents.
    queue, inspected = list(documents), 0
    while queue and inspected < 20000:
        node = queue.pop()
        inspected += 1
        if isinstance(node, dict):
            identity = next((str(node[k]) for k in ('aweme_id', 'awemeId', 'item_id', 'itemId', 'id') if k in node), None)
            if video_id == identity:
                collect(node.get('video'))
                collect(node)
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(node)
    # Public HTML media is accepted only when its own identity is explicit.
    for attrs in parser.videos:
        if attrs.get('data-video-id', attrs.get('data-aweme-id')) == video_id:
            add(attrs.get('src'), 'public_video_element')
    if _video_id(parser.metadata.get('og:url', '')) == video_id:
        for key in ('og:video:secure_url', 'og:video:url', 'og:video'):
            add(parser.metadata.get(key), 'public_open_graph')
    return candidates[:4]


def _browser_douyin_source(url, folder, video_id, cancelled=None, *, progress_callback=None):
    """Product-owned rendering fallback; metadata alone can never be a source."""
    from .browser_capture import capture_douyin
    _notify_progress(progress_callback, 'browser')
    last_browser_event = None

    def browser_progress(raw):
        nonlocal last_browser_event
        if not isinstance(raw, dict):
            return
        counters = {key: raw[key] for key in ('requests', 'downloaded_bytes', 'proxy_connections', 'proxy_bytes')
                    if _progress_count(raw.get(key))}
        responses = raw.get('response_status_counts')
        if isinstance(responses, dict) and all(_progress_count(value) for value in responses.values()):
            count = sum(responses.values())
            if _progress_count(count):
                counters['requests'] = count
        for source, target in (('observed_media', 'media_responses'), ('complete_media', 'complete_bound_media')):
            if _progress_count(raw.get(source)):
                counters[target] = raw[source]
        if not counters:
            return
        if counters != last_browser_event:
            _notify_progress(progress_callback, 'browser', **counters)
            last_browser_event = counters

    options = {'video_id': video_id, 'timeout': 180, 'max_bytes': MAX_UPLOAD, 'cancelled': cancelled}
    if progress_callback is not None:
        options['progress_callback'] = browser_progress
    captured=capture_douyin(f'https://www.douyin.com/video/{video_id}',folder,**options)
    if str(captured.get('observed_video_id'))!=str(video_id):
        raise AppError('douyin_browser_identity','自动采集的视频标识与分享链接不一致')
    resolved=captured.get('resolved_url','')
    if not _douyin_host(urlsplit(resolved).hostname or '') or _video_id(resolved)!=str(video_id):
        raise AppError('douyin_browser_identity','采集器未停留在指定抖音原视频')
    paths=captured.get('media_paths',[])
    if not isinstance(paths,list) or not paths:
        raise AppError('douyin_browser_material','自动采集仅取得页面信息，尚未取得完整原片')
    records={str(Path(item['path']).resolve()):item for item in captured.get('media_files',[])
        if isinstance(item,dict) and isinstance(item.get('path'),str)}
    root=folder.resolve(); assets=[]
    selected_paths=paths[:8]
    _notify_progress(progress_callback, 'validating_media', completed=0, total=len(selected_paths))
    for index,value in enumerate(selected_paths):
        try:
            path=Path(value)
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():continue
            size=path.stat().st_size
            if not 0<size<=MAX_UPLOAD:continue
            record=records.get(str(path.resolve()),{})
            if record.get('complete') is not True or record.get('size')!=size:continue
            if record.get('coverage')!=[[0,size]]:continue
            if str(record.get('video_id'))!=str(video_id):continue
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            if record.get('sha256')!=digest:continue
            probe=inspect_video(path)
            reported=captured.get('reported_duration')
            if isinstance(reported,(int,float)) and reported>0 and abs(probe['duration']-reported)>max(1,reported*.02):continue
            assets.append({'path':path,'size':size,'sha256':digest,'probe':probe})
        except AppError as exc:
            if exc.code == 'cancelled':
                raise
            continue
        except (OSError,ValueError,TypeError):continue
        finally:
            _notify_progress(progress_callback, 'validating_media', completed=index+1, total=len(selected_paths))
    complete=next((item for item in assets if item['probe']['video'] and item['probe']['audio']),None)
    components=[]
    part=folder/'douyin.mp4.part'
    try:
        if complete:
            _notify_progress(progress_callback, 'merging_media', completed=0, total=1)
            shutil.copyfile(complete['path'],part)
            _notify_progress(progress_callback, 'merging_media', completed=1, total=1)
            components=[complete['sha256']]
        else:
            video=next((item for item in assets if item['probe']['video']),None)
            audio=next((item for item in assets if item['probe']['audio'] and not item['probe']['video']),None)
            if not video:
                raise AppError('douyin_browser_material','自动采集未取得指定原片的完整视频轨')
            if audio:
                if abs(video['probe']['duration']-audio['probe']['duration'])>max(1,video['probe']['duration']*.02):
                    raise AppError('douyin_browser_material','采集音画时长不一致，未合成为原片分析')
                _notify_progress(progress_callback, 'merging_media', completed=0, total=1)
                run_media(['ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe',
                    '-format_whitelist','mov,matroska,webm,wav,mp3','-i',str(video['path']),
                    '-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska,webm,wav,mp3',
                    '-i',str(audio['path']),'-map','0:v:0','-map','1:a:0','-c','copy',
                    '-movflags','+faststart','-f','mp4',str(part)])
                _notify_progress(progress_callback, 'merging_media', completed=1, total=1)
                components=[video['sha256'],audio['sha256']]
            else:
                _notify_progress(progress_callback, 'merging_media', completed=0, total=1)
                shutil.copyfile(video['path'],part)
                _notify_progress(progress_callback, 'merging_media', completed=1, total=1)
                components=[video['sha256']]
        if part.stat().st_size>MAX_UPLOAD:
            raise AppError('douyin_browser_material','自动取得的原视频超过100MB限制')
        _notify_progress(progress_callback, 'validating_media', completed=0, total=1)
        probe=inspect_video(part)
        if not probe['video']:
            raise AppError('douyin_browser_material','未验证到原视频轨，不以页面信息作为成功')
        _notify_progress(progress_callback, 'validating_media', completed=1, total=1)
        final=folder/'douyin.mp4';part.replace(final)
        # Only explicitly safe counters/digests leave the transient renderer.
        meta=captured.get('acquisition',{})
        acquisition={key:meta[key] for key in ('requests','downloaded_bytes','blocked_requests',
            'renderer_version','identity_metadata_sha256','native_origin_tls','proxy_connections',
            'proxy_bytes') if key in meta}
        acquisition.update(method='project_browser',media_sha256=hashlib.sha256(final.read_bytes()).hexdigest())
        acquisition.update(media_bytes=final.stat().st_size,probe=probe,component_media_sha256=components)
        result={'platform':'douyin','url':url,'resolved_url':resolved,'video_id':str(video_id),
            'title':str(captured.get('title') or '抖音视频')[:500],'text':'','evidence':[],
            'media_path':str(final),'basis':'video_pending','files':[],'acquisition':acquisition}
        if not probe['audio']:result['limitations']=['采集到的视频未发现音轨，当前不能分析原音']
        return result
    finally:
        part.unlink(missing_ok=True)


def douyin_source(url, folder, *, cancelled=None, progress_callback=None):
    parsed=urlsplit(url)
    if not _douyin_host(parsed.hostname or ''):
        raise AppError('unsupported_source', '当前仅支持 GitHub、抖音链接和文件上传')
    try:port=parsed.port
    except ValueError:raise AppError('unsafe_url','抖音链接端口无效') from None
    if parsed.scheme not in ('http','https') or parsed.username or parsed.password or port not in (None,80,443):
        raise AppError('unsafe_url','请提供无认证信息的抖音分享链接')
    folder = Path(folder)
    pages, tried, video_id = [url], set(), _video_id(url)
    last_error = None
    responses = downloaded_bytes = 0
    _notify_progress(progress_callback, 'public_page', completed=0, requests=0, downloaded_bytes=0)
    for page_url in pages:
        if page_url in tried:
            continue
        tried.add(page_url)
        try:
            payload, final, _ = fetch_public(page_url, headers={'User-Agent': _MOBILE_UA})
            responses += 1
            downloaded_bytes += len(payload)
            _notify_progress(progress_callback, 'public_page', completed=responses,
                             requests=responses, downloaded_bytes=downloaded_bytes)
            if not _douyin_host(urlsplit(final).hostname or ''):
                raise AppError('douyin_material', '抖音分享页跳转到不支持的来源，请补传原视频')
            resolved_id = _video_id(final)
            if video_id and resolved_id and video_id != resolved_id:
                raise AppError('douyin_material', '公开页视频标识与请求不一致，请核对分享链接')
            video_id = video_id or resolved_id
            page = payload.decode('utf-8', 'replace')
            candidates = _published_media(page, video_id) if video_id else []
            if not candidates and video_id and len(pages) == 1:
                share = f'https://www.iesdouyin.com/share/video/{video_id}/'
                if urlsplit(final).hostname != 'www.iesdouyin.com':
                    pages.append(share)
            title = re.search(r'<title[^>]*>(.*?)</title>', page, re.S | re.I)
            title = html.unescape(title[1]).strip()[:500] if title else '抖音视频'
            for media_url, method in candidates:
                path = folder / 'douyin.mp4.part'
                try:
                    media, media_final, _ = fetch_public(media_url, MAX_UPLOAD, headers={'User-Agent': _MOBILE_UA})
                    responses += 1
                    downloaded_bytes += len(media)
                    _notify_progress(progress_callback, 'public_page', completed=responses,
                                     requests=responses, downloaded_bytes=downloaded_bytes)
                    folder.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(media)
                    _notify_progress(progress_callback, 'validating_media', completed=0, total=1)
                    probe = inspect_video(path)
                    if not probe['video']:
                        raise AppError('douyin_material', '公开媒体不是视频，不能替代原片分析')
                    _notify_progress(progress_callback, 'validating_media', completed=1, total=1)
                    final_path = folder / 'douyin.mp4'
                    path.replace(final_path)
                    clean_final = urlunsplit((*urlsplit(final)[:3], '', ''))
                    result = {'platform': 'douyin', 'url': url, 'resolved_url': clean_final,
                              'video_id': video_id, 'title': title, 'text': '', 'evidence': [],
                              'media_path': str(final_path), 'basis': 'video_pending', 'files': [],
                              'acquisition': {'method': method, 'page_sha256': hashlib.sha256(payload).hexdigest(),
                                  'media_sha256': hashlib.sha256(media).hexdigest(), 'media_bytes': len(media),
                                  'media_url_sha256': hashlib.sha256(media_url.encode()).hexdigest(),
                                  'media_host': urlsplit(media_final).hostname, 'probe': probe}}
                    if not probe['audio']:
                        result['limitations'] = ['原视频未发现音轨，不能进行原音转写']
                    return result
                except AppError as exc:
                    if exc.code == 'cancelled':
                        raise
                    last_error = exc
                except Exception:
                    last_error = AppError('douyin_fetch', '公开媒体下载未完成，请补传原视频', True)
                finally:
                    path.unlink(missing_ok=True)
        except AppError as exc:
            if exc.code == 'cancelled':
                raise
            last_error = exc
        except Exception:
            last_error = AppError('douyin_fetch', '抖音公开页获取未完成，请检查网络或补传视频', True)
    if video_id:
        if progress_callback is not None:
            return _browser_douyin_source(url,folder,video_id,cancelled=cancelled,progress_callback=progress_callback)
        return _browser_douyin_source(url,folder,video_id,cancelled=cancelled)
    if last_error:
        raise AppError('douyin_fetch', '项目未能解析此抖音原视频，请检查网络或平台访问限制；没有用网页摘要代替原片', last_error.retryable)
    raise AppError('douyin_material', '项目未能取得当前原视频标识；没有用网页标题或摘要代替原片')
