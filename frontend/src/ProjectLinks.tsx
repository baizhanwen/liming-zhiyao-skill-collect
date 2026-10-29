import { formatDate } from './api';
import type { Doc } from './api';
import './ProjectLinks.css';

function repositoryUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.username || url.password || url.port || url.search || url.hash) return null;
    const parts = url.pathname.replace(/\/$/, '').split('/').filter(Boolean);
    if (parts.length !== 2 || parts.some(part => !/^[A-Za-z0-9_.-]+$/.test(part) || part === '.' || part === '..')) return null;
    const repository = parts[1].replace(/\.git$/, '');
    return repository && repository !== '.' && repository !== '..' ? `https://github.com/${parts[0]}/${repository}` : null;
  } catch { return null; }
}

const itemStates: Record<string, string> = {
  repository_verified: '明确链接核验结果', candidates: '名称检索候选', ambiguous: '多个候选，存在歧义',
  no_match: '暂未匹配', unavailable: '检索或核验受限', invalid: '线索或地址无效',
};

function sourcePresentation(basis: string): {title: string; note: string} {
  if (basis === 'transcript_and_frames') return {title: '转写与抽样画面提到的 GitHub 项目', note: '依据音频段转写及抽样画面。时间按来源记录显示，项目名称和链接仍需人工核对。'};
  if (basis === 'subtitles_and_frames') return {title: '字幕与抽样画面中的 GitHub 项目', note: '依据上传字幕及抽样画面；字幕时间与取样点不等于项目名称的精确出现秒数。'};
  if (basis === 'frames_only') return {title: '抽样画面中的 GitHub 项目', note: '仅依据抽样画面的视觉材料，没有音频转写；取样点不代表连续查看了完整视频。'};
  if (basis === 'subtitles') return {title: '字幕中提到的 GitHub 项目', note: '时间来自字幕文件，尚不能据此确认原片口述或画面。'};
  if (['audio_transcript', 'transcript'].includes(basis)) return {title: '转写中提到的 GitHub 项目', note: '仅依据音频转写。音频段范围不是项目名称的精确出现秒数，未分析视频画面。'};
  if (basis === 'text_only') return {title: '相关文本中的 GitHub 项目', note: '仅依据保存的文本。平台 AI 摘要只能作为文本线索，不能据此确认原片音画内容。'};
  return {title: '视频项目关联', note: '关联范围以实际取得的转写、字幕或画面证据为准。'};
}

function evidenceLabel(kind: string, basis: string): string {
  if (kind === 'user_input') return '用户输入线索（非原片证据）';
  if (kind === 'source_url') return '来源链接（非原片证据）';
  if (kind === 'model_frame_observation') return '抽样画面观察（模型输出）';
  if (kind === 'subtitle' || (kind === 'transcript' && ['subtitles', 'subtitles_and_frames'].includes(basis))) return '字幕片段';
  if (kind === 'transcript') return '音频转写（段范围）';
  return '文本片段';
}

export default function ProjectLinks({task, busy, searching, modelProvider, modelName, onAnalyze, onEvidence, onSearch}: {
  task: Doc; busy: boolean; searching: boolean; modelProvider: string; modelName: string;
  onAnalyze: (url: string) => void; onEvidence: (id: string) => void; onSearch: () => void;
}) {
  const source = task.source || {}, analysis = task.analysis || {}, report = task.project_links;
  const basis = source.basis || analysis.basis || report?.basis || '';
  if (basis === 'source_files' || source.platform === 'github') return null;
  const isMediaTask = report || basis || source.media_path || source.platform === 'douyin' || source.platform === 'upload'
    || /douyin\.com/i.test(task.input || '') || (task.uploads || []).length;
  if (!isMediaTask) return null;
  const presentation = sourcePresentation(basis);
  const evidence: Doc[] = Array.isArray(source.evidence) ? source.evidence : Array.isArray(analysis.evidence) ? analysis.evidence : [];
  const links: Doc[] = Array.isArray(report?.links) ? report.links : [];
  const stale = report?.source_snapshot_hash && source.snapshot_hash && report.source_snapshot_hash !== source.snapshot_hash;
  const inProgress = task.stage === 'project_links' && task.status === 'running';
  return <section className="project-links" aria-label="GitHub 项目关联">
    <div className="project-links-heading"><h3>{presentation.title}</h3><span className="badge">{stale || !report ? inProgress ? '关联中' : '未验证' : ({none: links.length ? '已检索，未匹配仓库' : '本次未提取到线索', found: '已取得关联结果', partial: '部分项目尚未匹配', unavailable: '关联受限'} as Doc)[report.status] || '未验证'}</span></div>
    <p className="project-links-note">{presentation.note}</p>
    <p className="project-links-boundary">仓库存在已核验，只表示链接指向可访问的仓库；名称搜索结果仍是候选，不代表已确认视频提到的就是它，也不代表代码经过运行验证。</p>
    <div className="project-lookup-actions"><button className="button" type="button" aria-busy={searching} onClick={onSearch} disabled={busy || task.status !== 'succeeded' || !Object.keys(source).length || !Object.keys(analysis).length}>{searching ? '正在检索项目…' : report ? '重新检索项目' : '检索项目'}</button><small>仅重新查询 GitHub，不重复调用转写、视觉或文字模型。{report?.checked_at && <>上次关联：{formatDate(report.checked_at)}</>}</small></div>
    {stale ? <p className="project-links-empty">关联记录与当前来源快照不一致，结果未验证。请在重新关联后查看。</p>
      : !report ? <p className="project-links-empty">{inProgress ? '正在关联 GitHub 项目，已保存的材料与分析结果不受影响。' : '此任务尚无项目关联记录，不能据此判断材料没有提到项目。'}</p>
      : <>
        {links.length === 0 && <p className="project-links-empty">{report.status === 'none' ? '本次未提取到可关联的 GitHub 项目线索，不代表材料中一定没有项目。' : '暂未取得可展示的项目关联结果。'}</p>}
        {links.map((item, index) => <article className="project-link-item" key={item.id || index}>
          <div className="project-item-heading"><h4>{item.name || '未命名项目线索'}</h4><span className="badge project-match-state">{itemStates[item.status] || '关联状态未验证'}</span></div>
          <div className="project-mention-evidence">
            {(Array.isArray(item.evidence) ? item.evidence : []).map((reference: Doc, i: number) => {
              const matches = evidence.filter(value => value.id === reference.id);
              const original = matches.length === 1 ? matches[0] : null;
              const supplemental = reference.kind === 'user_input' && reference.id === 'source-original_input' ? source.original_input
                : reference.kind === 'source_url' && reference.id === 'source-url' ? source.url : null;
              const excerpt = original?.excerpt || original?.text || supplemental || '';
              const located = (original || supplemental) && typeof reference.quote === 'string' && reference.quote.length > 0 && typeof excerpt === 'string' && excerpt.replace(/\s+/g, ' ').includes(reference.quote.replace(/\s+/g, ' '));
              if (!located) return <p className="project-evidence-missing" key={i}>证据引用 {reference.id || '未给出'} 无法在当前来源中定位，未作为原片证据。</p>;
              const time = original?.time ?? (original?.start != null ? `${original.start}s — ${original.end ?? '未知'}s` : null);
              return <div className="project-evidence" key={i}><div><span className="badge">{evidenceLabel(original?.kind || reference.kind, basis)}</span><code>{reference.id}</code></div>
                {supplemental ? <small>此线索来自提交的输入或来源链接，没有原片时间定位。</small> : time != null ? <small>{basis === 'text_only' ? '文本所载时间（未经原片核验）：' : '来源时间范围 / 取样点：'}{String(time)}</small> : <small>来源未提供时间，不编造精确秒数。</small>}
                <blockquote>{reference.quote}</blockquote>{original && <button className="text-button" type="button" onClick={() => onEvidence(reference.id)}>查看此处来源证据</button>}</div>;
            })}
            {!(item.evidence || []).length && <p className="project-evidence-missing">未提供可定位的来源证据，项目对应关系未验证。</p>}
          </div>
          {item.query && <p className="project-search-query">检索词：<code>{item.query}</code></p>}
          {item.status === 'ambiguous' && <p className="project-item-note">存在多个合理候选，请逐个核对；没有自动选定仓库。</p>}
          {item.status === 'no_match' && <p className="project-item-note">此次检索未找到候选，不代表项目不存在。</p>}
          {item.status === 'invalid' && <p className="project-item-note">当前线索不足或地址无效，暂不能据此关联仓库。</p>}
          {(Array.isArray(item.candidates) ? item.candidates : []).map((candidate: Doc, i: number) => {
            const url = repositoryUrl(candidate.url);
            const direct = candidate.match === 'explicit_url';
            const verified = !!url && direct && candidate.verification_status === 'repository_exists';
            return <div className="project-candidate" key={`${candidate.repository || ''}-${i}`}>
              <div className="project-candidate-heading"><strong>{candidate.repository || candidate.url || '仓库地址待核对'}</strong><span className="badge project-candidate-status">{verified ? '明确链接 · 仓库存在已核验' : direct ? '明确链接 · 尚未核验存在' : '名称检索候选 · 尚未确认'}</span></div>
              {candidate.description && <p>{candidate.description}</p>}
              {url ? <code className="project-repository-url">{url}</code> : <p className="project-evidence-missing">地址无效，暂不可打开或分析。</p>}
              {candidate.version && <small>仓库核验版本：<code>{candidate.version}</code>；新任务将另行保存实际读取的版本。</small>}
              <div className="project-candidate-actions">{url && <a className="button" href={url} target="_blank" rel="noreferrer">打开 GitHub</a>}
                <button className="button" type="button" disabled={busy || !url} onClick={() => { if (url) onAnalyze(url); }}>{verified ? '分析此仓库' : '分析此候选仓库'}</button></div>
            </div>;
          })}
          {item.error && <div className="project-link-error" role="note"><strong>关联受限{item.error.code ? `（${item.error.code}）` : ''}</strong><p>{item.error.message || '本次无法完成检索或核验，未把错误当作无匹配结果。'}</p></div>}
          {(Array.isArray(item.notes) ? item.notes : []).map((note: unknown, i: number) => typeof note === 'string' && <p className="project-item-note" key={i}>{note}</p>)}
        </article>)}
        {(Array.isArray(report.errors) ? report.errors : []).map((error: Doc, index: number) => <p className="project-link-error" key={index}>{error.message || error.code || '部分关联未完成，请查看逐项结果。'}</p>)}
        {report.limits?.truncated && <p className="project-item-note">已达到本次扫描或检索上限，关联结果可能不完整。</p>}
        {(Array.isArray(report.warnings) ? report.warnings : []).map((warning: unknown, index: number) => typeof warning === 'string' && <p className="project-item-note" key={index}>{warning}</p>)}
      </>}
    {!stale && links.some(item => (item.candidates || []).length > 0) && <p className="project-analysis-target">点击分析才会新建仓库任务。{modelProvider ? <>文字材料将发送到 <code>{modelProvider}</code>{modelName && <>（{modelName}）</>}。</> : '文字模型未配置，新任务会等待配置。'}候选仓库分析不等于确认项目对应关系。</p>}
  </section>;
}
