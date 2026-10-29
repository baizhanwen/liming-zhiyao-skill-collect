"""Evidence-grounded, optional video-to-GitHub association.

No model calls, code execution, persistence, or application-login discovery live
here. The caller supplies the same source/analysis and its configured GitHub
factory. Every search hit remains a candidate, including a single exact-name hit.
"""
from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import urlsplit, urlunsplit

from .connectors.github import GitHubConnector, GithubError

MAX_EXPLICIT = 4
MAX_QUERIES = 3
MAX_CANDIDATES = 12
MAX_PER_QUERY = 4
MAX_EVIDENCE = 80
MAX_SCAN_CHARS = 200_000
MAX_MENTIONS = 12
MAX_REFERENCES = 8
DEADLINE_SECONDS = 60
_URL = re.compile(r'(?<![\w@./:=?&%+-])(?:https?://)?(?:www\.)?github\.com/[^\s<>"`，。；：！？、）】》「」『』〈〉]+', re.I)
_TRAILING = '.,;:!?)]}，。；：！？）】》、'
_ERRORS = {
    'rate_limit': 'GitHub 搜索或核验已限流；可稍后重试，或在本地设置 GitHub Token。',
    'authentication': '本地配置的 GitHub Token 无效或已过期。',
    'permission': 'GitHub 拒绝访问；请检查本地配置的权限。',
    'not_found': '未核验到该仓库，或当前凭证没有读取权限。',
    'network': '暂时无法连接 GitHub，媒体主分析仍保留。',
    'timeout': 'GitHub 请求超时，媒体主分析仍保留。',
    'upstream': 'GitHub 服务暂时返回错误，媒体主分析仍保留。',
    'response_limit': 'GitHub 响应超过安全大小上限，未继续读取。',
    'configuration': 'GitHub 连接配置不可用，请在本地设置中检查。',
    'budget': '自动关联已达到本次请求或时间上限；可手动打开 GitHub 继续查找。',
    'protocol': 'GitHub 返回的仓库数据无法安全核验。',
    'invalid_evidence': '项目名称或引文未被所引来源原文支持，未发起搜索。',
    'invalid_url': '来源中的 GitHub 链接不是受支持的 HTTPS 仓库链接，未请求该地址。',
    'internal': 'GitHub 关联暂时不可用；媒体主分析仍保留。',
}


def _whitespace(value):
    return ' '.join(value.split())


def _normalized(value):
    return _whitespace(value).casefold()


def _error(code, retryable=False):
    # Do not echo arbitrary exception/upstream text, credentials or request URLs.
    return {'code': code if code in _ERRORS else 'internal',
            'message': _ERRORS.get(code, _ERRORS['internal']), 'retryable': bool(retryable)}


def _reference(item, quote):
    result = {'id': item['id'], 'kind': item.get('kind') if isinstance(item.get('kind'), str) else 'unknown',
              'quote': quote[:600]}
    if isinstance(item.get('time'), (str, int, float)):
        result['time'] = item['time']
    return result


def _merge_refs(target, refs):
    for ref in refs:
        if ref not in target and len(target) < MAX_REFERENCES:
            target.append(ref)


def _link(key, name, refs):
    return {'id': 'project-' + hashlib.sha256(key.encode()).hexdigest()[:12],
            'name': name[:120], 'status': 'pending', 'evidence': refs,
            'explicit_urls': [], 'candidates': [], 'notes': []}


def _repository(value):
    if not isinstance(value, str):
        raise GithubError('protocol', 'Invalid repository identity')
    parsed = GitHubConnector._parse('https://github.com/' + value)
    if parsed['kind'] != 'repository' or parsed['repository'] != value:
        raise GithubError('protocol', 'Invalid repository identity')
    return parsed['repository']


def resolve_project_links(source: dict, analysis: dict, github_factory) -> dict:
    """Return a bounded association report; external failures never escape.

    Explicit links come only from actual source evidence or user/source URLs.
    Analysis contributes names only through grounded ``project_mentions`` with
    ``name``, ``quote`` and ``evidence_ids``. No repository URL from a model is
    trusted. Quotes and names are rechecked with case/whitespace normalization.
    """
    report = {'schema_version': 1, 'status': 'none', 'links': [], 'queries': [],
              'errors': [], 'warnings': [], 'limits': {
                  'max_explicit_urls': MAX_EXPLICIT, 'max_queries': MAX_QUERIES,
                  'max_candidates': MAX_CANDIDATES, 'max_candidates_per_query': MAX_PER_QUERY,
                  'max_evidence': MAX_EVIDENCE, 'max_scan_characters': MAX_SCAN_CHARS,
                  'max_references_per_link': MAX_REFERENCES,
                  'deadline_seconds': DEADLINE_SECONDS, 'deadline_checked_between_operations': True,
                  'truncated': False},
              'note': '仓库存在及搜索排名不证明与视频项目相同；未下载、执行或验证项目。'}
    connector = None
    deadline = time.monotonic() + DEADLINE_SECONDS
    candidate_keys = set()
    try:
        evidence = {}
        duplicate_ids = set()
        raw_evidence = source.get('evidence', [])
        if not isinstance(raw_evidence, list):
            raw_evidence = []
        if len(raw_evidence) > MAX_EVIDENCE:
            report['limits']['truncated'] = True
        scan = []
        remaining = MAX_SCAN_CHARS
        for item in raw_evidence[:MAX_EVIDENCE]:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('excerpt'), str):
                continue
            text = item['excerpt'][:remaining]
            if item['id'] in evidence:
                duplicate_ids.add(item['id'])
            else:
                evidence[item['id']] = {**item, 'excerpt': text}
            if len(text) != len(item['excerpt']):
                report['limits']['truncated'] = True
            remaining -= len(text)
            if text:
                scan.append((item, text))
        for field, kind in (('url', 'source_url'), ('original_input', 'user_input')):
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                text = value[:min(remaining, 8000)]
                remaining -= len(text)
                scan.append(({'id': 'source-' + field, 'kind': kind}, text))
                if len(value) > len(text):
                    report['limits']['truncated'] = True

        explicit = {}
        for item, text in scan:
            for match in _URL.finditer(text):
                raw = match[0].rstrip(_TRAILING)
                url = raw if '://' in raw else 'https://' + raw
                parts = urlsplit(url)
                # Query/fragment parameters are irrelevant for repository
                # identity, and may contain private tracking information.
                url = urlunsplit((parts.scheme, 'github.com' if parts.hostname == 'www.github.com' else parts.netloc,
                                  parts.path, '', ''))
                ref = _reference(item, text[max(0, match.start() - 100):match.end() + 100])
                try:
                    parsed = GitHubConnector._parse(url)
                except (GithubError, ValueError):
                    if len(explicit) < MAX_EXPLICIT:
                        key = 'invalid:' + url
                        if key not in explicit:
                            record = _link(key, '无法核验的 GitHub 链接', [ref])
                            record.update(status='invalid', error=_error('invalid_url'))
                            explicit[key] = record
                    continue
                key = parsed['repository'].casefold()
                if key not in explicit:
                    if len(explicit) >= MAX_EXPLICIT:
                        report['limits']['truncated'] = True
                        continue
                    explicit[key] = _link(key, parsed['repository'], [])
                    explicit[key]['repository'] = parsed['repository']
                _merge_refs(explicit[key]['evidence'], [ref])
                if url not in explicit[key]['explicit_urls'] and len(explicit[key]['explicit_urls']) < MAX_REFERENCES:
                    explicit[key]['explicit_urls'].append(url)
        report['links'].extend(explicit.values())

        mentions = analysis.get('project_mentions', [])
        if not isinstance(mentions, list):
            mentions = []
        if len(mentions) > MAX_MENTIONS:
            report['limits']['truncated'] = True
        names = {}
        for index, mention in enumerate(mentions[:MAX_MENTIONS]):
            valid = isinstance(mention, dict)
            name, quote, ids = (mention.get('name'), mention.get('quote'), mention.get('evidence_ids')) if valid else (None, None, None)
            valid = (isinstance(name, str) and 0 < len(name.strip()) <= 120
                     and isinstance(quote, str) and 0 < len(quote.strip()) <= 600
                     and isinstance(ids, list) and 0 < len(ids) <= 20
                     and all(isinstance(id, str) and id in evidence and id not in duplicate_ids for id in ids))
            if valid:
                valid = (_normalized(name) in _normalized(quote)
                         and all(_whitespace(quote) in _whitespace(evidence[id]['excerpt']) for id in ids))
            if not valid:
                record = _link('invalid-mention-' + str(index), name if isinstance(name, str) else '无有效来源的项目线索', [])
                record.update(status='invalid', error=_error('invalid_evidence'))
                report['links'].append(record)
                continue
            name = name.strip()
            key = _normalized(name)
            refs = [_reference(evidence[id], quote.strip()) for id in dict.fromkeys(ids)]
            if key in names:
                _merge_refs(names[key]['evidence'], refs)
                continue
            record = _link('name:' + key, name, refs)
            record['evidence'] = record['evidence'][:MAX_REFERENCES]
            # Literal quoted terms prevent source text from introducing GitHub
            # qualifiers/operators or a fabricated owner/repository filter.
            terms = re.sub(r'[^\w .+/-]', ' ', name, flags=re.UNICODE)
            terms = ' '.join(terms.split())
            if not terms or not any(c.isalnum() for c in terms):
                record.update(status='invalid', error=_error('invalid_evidence'))
            else:
                record['query'] = '"' + terms + '" in:name,description'
            names[key] = record
            report['links'].append(record)

        def get_connector():
            nonlocal connector
            if time.monotonic() >= deadline:
                raise GithubError('budget', 'Association deadline reached')
            if connector is None:
                connector = github_factory()
            return connector

        def add_candidate(record, value, match):
            repository = _repository(value.get('repository'))
            key = repository.casefold()
            if any(c['repository'].casefold() == key for c in record['candidates']):
                return
            if key not in candidate_keys and len(candidate_keys) >= MAX_CANDIDATES:
                report['limits']['truncated'] = True
                return
            description, stars = value.get('description'), value.get('stars')
            license_name, updated_at = value.get('license'), value.get('updated_at')
            candidate = {'repository': repository, 'url': 'https://github.com/' + repository,
                         'description': description[:2000] if isinstance(description, str) else None,
                         'stars': stars if type(stars) is int and stars >= 0 else None,
                         'license': license_name[:200] if isinstance(license_name, str) else 'UNKNOWN',
                         'updated_at': updated_at[:100] if isinstance(updated_at, str) else None,
                         'match': match, 'same_project_confirmed': False,
                         'verification_status': 'repository_exists' if match == 'explicit_url' else 'search_result'}
            if match == 'explicit_url':
                if not isinstance(value.get('version'), str) or not re.fullmatch(r'[a-fA-F0-9]{40}', value['version']):
                    raise GithubError('protocol', 'Missing fixed commit')
                candidate['version'] = value['version']
            candidate_keys.add(key)
            record['candidates'].append(candidate)

        for record in explicit.values():
            if record['status'] != 'pending':
                continue
            try:
                target = get_connector().normalize('https://github.com/' + record['repository'])
                add_candidate(record, {'repository': target['repository'], 'version': target['version'],
                                       **target.get('metadata', {})}, 'explicit_url')
                record['status'] = 'repository_verified'
                record['notes'].append('仅核验来源明确写出的仓库存在及当前提交；没有独立确证视频与仓库的对应关系。')
            except Exception as exc:
                record.update(status='unavailable', error=_error(getattr(exc, 'code', 'internal'), getattr(exc, 'retryable', False)))

        for record in names.values():
            if record['status'] != 'pending':
                continue
            if len(report['queries']) >= MAX_QUERIES:
                record.update(status='unavailable', error=_error('budget'))
                report['limits']['truncated'] = True
                continue
            query = {'query': record['query'], 'status': 'pending'}
            report['queries'].append(query)
            try:
                result = get_connector().search_repositories(record['query'], limit=MAX_PER_QUERY)
                if not isinstance(result, dict) or not isinstance(result.get('items'), list):
                    raise GithubError('protocol', 'Invalid search response')
                for item in result['items'][:MAX_PER_QUERY]:
                    if not isinstance(item, dict):
                        raise GithubError('protocol', 'Invalid search item')
                    add_candidate(record, item, 'search_candidate')
                count = len(record['candidates'])
                # Even one result stays a candidate. total_count/incomplete
                # also matter: a bounded first page cannot prove uniqueness.
                total = result.get('total_count')
                ambiguous = count > 1 or isinstance(total, int) and total > 1 or result.get('incomplete_results')
                record['status'] = ('ambiguous' if ambiguous else 'candidates') if count else 'no_match'
                notes = result.get('notes', [])
                if isinstance(notes, list):
                    record['notes'].extend(note[:1000] for note in notes[:4] if isinstance(note, str))
                record['notes'].append('搜索候选需要人工核对；名称相同、关注数或排名不能确认是视频中同一项目。')
                if not count:
                    record['notes'].append('本次有界搜索未得到候选，不代表 GitHub 上不存在该项目。')
                if result.get('incomplete_results'):
                    record['notes'].append('GitHub 标记搜索结果不完整。')
                query.update(status='succeeded', total_count=total, incomplete_results=bool(result.get('incomplete_results')))
            except Exception as exc:
                error = _error(getattr(exc, 'code', 'internal'), getattr(exc, 'retryable', False))
                record.update(status='unavailable', error=error)
                query.update(status='failed', error=error)
        report['limits'].update(queries_used=len(report['queries']), unique_candidates=len(candidate_keys))
        positives = any(link['candidates'] for link in report['links'])
        problems = any(link['status'] in ('invalid', 'no_match', 'unavailable') for link in report['links'])
        failures = any(link['status'] in ('invalid', 'unavailable') for link in report['links'])
        report['status'] = ('partial' if problems or report['limits']['truncated'] else 'found') if positives else ('unavailable' if failures else 'none')
        report['errors'] = [{'link_id': link['id'], **link['error']} for link in report['links'] if 'error' in link]
        if report['limits']['truncated']:
            report['warnings'].append('本次自动关联已限量；未扫描或未查询的线索不代表不存在对应项目。')
    except Exception:
        # A malformed optional record/factory must not fail the media analysis.
        report['status'] = 'partial' if any(link['candidates'] for link in report['links']) else 'unavailable'
        report['errors'].append(_error('internal'))
    finally:
        if connector is not None and hasattr(connector, 'close'):
            try:
                connector.close()
            except Exception:
                pass
    return report
