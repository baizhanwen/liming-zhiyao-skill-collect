"""Bounded, read-only GitHub source acquisition. Never executes repository content."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse

import httpx


class GithubError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


class GitHubConnector:
    MAX_FILE = 512 * 1024
    MAX_PACKAGE = 8 * 1024 * 1024
    MAX_PACKAGE_FILES = 100
    MAX_TREE = 30000

    def __init__(self, token: str = '', *, client: httpx.Client | None = None):
        self.token = token
        self.client = client or httpx.Client(timeout=httpx.Timeout(25, connect=10), follow_redirects=False)
        self._owns_client = client is None
        self._cache: dict = {}

    def close(self):
        if self._owns_client:self.client.close()

    def _request(self, path: str, params: dict | None = None, *, missing=False, limit=8 * 1024 * 1024):
        if not path.startswith('/') or path.startswith('//'):
            raise GithubError('invalid_path', 'GitHub API 路径不合法')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'liming-zhiyao-skill-collect/1.0'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        try:
            with self.client.stream('GET', 'https://api.github.com' + path, params=params, headers=headers) as response:
                status = response.status_code
                if missing and status in (404, 422):
                    return None
                if status >= 300:
                    if status == 401:
                        raise GithubError('authentication', 'GitHub Token 无效或已过期')
                    if status == 403 and response.headers.get('x-ratelimit-remaining') == '0':
                        raise GithubError('rate_limit', 'GitHub 请求限额已用完，请稍后重试或配置 Token', True)
                    if status in (403, 429):
                        raise GithubError('rate_limit' if status == 429 else 'permission', 'GitHub 请求受限，请检查权限或稍后重试', status == 429)
                    if status == 404:
                        raise GithubError('not_found', 'GitHub 仓库、文件或版本不存在，或 Token 没有读取权限')
                    raise GithubError('upstream', f'GitHub 返回 HTTP {status}', status >= 500)
                raw = bytearray()
                for block in response.iter_bytes():
                    raw.extend(block)
                    if len(raw) > limit:
                        raise GithubError('response_limit', 'GitHub 响应超过本地安全上限')
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise GithubError('protocol', 'GitHub 返回了无法解析的数据') from None
        except httpx.TimeoutException:
            raise GithubError('timeout', 'GitHub 请求超时', True) from None
        except httpx.HTTPError:
            raise GithubError('network', '无法连接 GitHub，请检查网络或代理设置', True) from None

    @staticmethod
    def _parse(url: str) -> dict:
        parts = urlparse(url.strip())
        try:
            port = parts.port
        except ValueError:
            raise GithubError('invalid_url', 'GitHub 链接端口不合法') from None
        if parts.scheme != 'https' or parts.hostname != 'github.com' or parts.username or parts.password or port not in (None, 443):
            raise GithubError('invalid_url', '请输入 https://github.com 上的仓库、目录或文件链接')
        segments = [unquote(x) for x in parts.path.strip('/').split('/') if x]
        if len(segments) < 2 or any(x in ('.', '..') or '\\' in x or '/' in x for x in segments):
            raise GithubError('invalid_url', 'GitHub 链接路径不合法')
        owner, repo = segments[:2]
        repo = repo.removesuffix('.git')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', owner) or not re.fullmatch(r'[A-Za-z0-9_.-]+', repo):
            raise GithubError('invalid_url', 'GitHub 仓库名称不合法')
        suffix = segments[2:]
        if suffix and (suffix[0] not in ('tree', 'blob') or len(suffix) < 2):
            raise GithubError('invalid_url', '仅支持仓库、tree 目录或 blob 文件链接')
        return {'repository': f'{owner}/{repo}', 'owner': owner, 'repo': repo,
                'kind': suffix[0] if suffix else 'repository', 'suffix': suffix[1:] if suffix else []}

    def normalize(self, url: str) -> dict:
        parsed = self._parse(url)
        repository = parsed['repository']
        repo = self._request(f'/repos/{repository}')
        if parsed['suffix']:
            # A Git ref may itself contain slashes. Resolve the longest valid ref first.
            suffix = parsed['suffix']
            commit = None
            for length in range(min(len(suffix), 10), 0, -1):
                ref = '/'.join(suffix[:length])
                commit = self._request(f'/repos/{repository}/commits/{quote(ref, safe="")}', missing=True)
                if commit:
                    path = '/'.join(suffix[length:])
                    break
            if not commit:
                raise GithubError('not_found', '链接中的 GitHub 分支、标签或提交不存在')
        else:
            ref = repo['default_branch']
            path = ''
            commit = self._request(f'/repos/{repository}/commits/{quote(ref, safe="")}')
        sha = commit.get('sha', '')
        if not re.fullmatch(r'[a-fA-F0-9]{40}', sha):
            raise GithubError('protocol', 'GitHub 未返回固定提交版本')
        result = {**parsed, 'ref': ref, 'path': path, 'version': sha,
                  'url': f'https://github.com/{repository}' + (f'/{parsed["kind"]}/{sha}/{quote(path)}' if path else ''),
                  'original_url': url, 'metadata': {'description': repo.get('description'),
                  'stars': repo.get('stargazers_count', 0), 'updated_at': repo.get('updated_at'),
                  'license': (repo.get('license') or {}).get('spdx_id', 'UNKNOWN'),
                  'default_branch': repo.get('default_branch'), 'html_url': f'https://github.com/{repository}'}}
        self._cache[(repository, sha)] = result
        return result

    def _tree(self, target: dict) -> tuple[list[dict], bool]:
        key = ('tree', target['repository'], target['version'])
        if key not in self._cache:
            data = self._request(f'/repos/{target["repository"]}/git/trees/{target["version"]}', {'recursive': '1'})
            tree = data.get('tree', [])
            self._cache[key] = (tree[:self.MAX_TREE], bool(data.get('truncated')) or len(tree) > self.MAX_TREE)
        return self._cache[key]

    @staticmethod
    def _inside(path: str, scope: str) -> bool:
        return not scope or path == scope or path.startswith(scope.rstrip('/') + '/')

    @staticmethod
    def _safe_path(path: str):
        p = PurePosixPath(path)
        if not path or p.is_absolute() or '..' in p.parts or '\\' in path or ':' in path or '\x00' in path:
            raise GithubError('unsafe_file', '仓库包含不安全的文件路径')
        return path

    def _blob(self, target: dict, item: dict, *, limit: int | None = None) -> bytes:
        limit = limit or self.MAX_FILE
        self._safe_path(item['path'])
        if item.get('mode') in ('120000', '160000') or item.get('type') != 'blob':
            raise GithubError('unsafe_file', '技能包中的符号链接和子模块不支持导入')
        if item.get('size', 0) > limit:
            raise GithubError('file_limit', f'文件超过大小上限：{item["path"]}')
        data = self._request(f'/repos/{target["repository"]}/git/blobs/{item["sha"]}', limit=limit * 2 + 4096)
        if data.get('encoding') != 'base64':
            raise GithubError('protocol', 'GitHub 文件内容编码不受支持')
        try:
            raw = base64.b64decode(data['content'], validate=False)
        except (ValueError, KeyError):
            raise GithubError('protocol', 'GitHub 文件内容损坏') from None
        if len(raw) > limit:
            raise GithubError('file_limit', '文件超过大小上限')
        expected = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
        if expected != item['sha']:
            raise GithubError('integrity', 'GitHub 文件哈希校验失败')
        return raw

    def _text_file(self, target: dict, item: dict) -> dict | None:
        raw = self._blob(target, item)
        try:
            content = raw.decode('utf-8')
        except UnicodeError:
            return None
        if '\x00' in content:
            return None
        return {'path': item['path'], 'url': f'https://github.com/{target["repository"]}/blob/{target["version"]}/{quote(item["path"])}',
                'sha': item['sha'], 'content': content, 'sha256': hashlib.sha256(raw).hexdigest()}

    def snapshot(self, url: str, depth: str = 'quick') -> dict:
        target = self.normalize(url)
        tree, truncated = self._tree(target)
        scope = target['path']
        blobs = [i for i in tree if i.get('type') == 'blob' and i.get('mode') not in ('120000', '160000')]
        if scope and not any(self._inside(i['path'], scope) for i in tree):
            raise GithubError('not_found', '指定文件或目录不在该提交中')
        if target['kind'] == 'blob':
            selected = [i for i in blobs if i['path'] == scope]
        else:
            candidates = [i for i in blobs if self._inside(i['path'], scope) and i.get('size', 0) <= self.MAX_FILE]
            def score(i):
                path = i['path'].lower()
                name = PurePosixPath(path).name
                if name.startswith('readme'):
                    rank = 0
                elif name.startswith(('license', 'copying', 'notice')):
                    rank = 1
                elif name in ('package.json', 'pyproject.toml', 'requirements.txt', 'cargo.toml', 'go.mod', 'dockerfile', 'skill.md'):
                    rank = 2
                elif path.endswith(('.md', '.rst')) and path.count('/') < 5:
                    rank = 3
                elif path.endswith(('.py', '.ts', '.tsx', '.js', '.go', '.rs')) and 'lock' not in name:
                    rank = 4
                else:
                    rank = 8
                return rank, path.count('/'), path
            candidates.sort(key=score)
            selected = [i for i in candidates if score(i)[0] < 8][:18 if depth == 'deep' else 8]
        if not selected:
            raise GithubError('no_content', '该范围没有可读取的文字文件')
        files, total = [], 0
        for item in selected:
            f = self._text_file(target, item)
            if f and total + len(f['content']) <= (150000 if depth == 'deep' else 60000):
                files.append(f)
                total += len(f['content'])
        if not files:
            raise GithubError('no_content', '该范围没有可读取的 UTF-8 文字文件')
        evidence = [{'id': f'G{i + 1}', 'path': f['path'], 'url': f['url'], 'excerpt': f['content'][:1200],
                     'kind': 'source_claim' if f['path'].lower().endswith(('.md', '.rst', '.txt')) else 'code_observation'}
                    for i, f in enumerate(files)]
        return {'platform': 'github', 'url': target['url'], 'original_url': url, 'repository': target['repository'],
                'version': target['version'], 'title': target['repository'], 'files': files, 'evidence': evidence,
                'text': '\n\n'.join(f'[{e["id"]}] {f["path"]}\n{f["content"]}' for e, f in zip(evidence, files)),
                'metadata': {**target['metadata'], 'tree_truncated': truncated, 'files_read': len(files),
                             'scope': scope, 'execution_verified': False}}

    @staticmethod
    def _frontmatter(content: str, fallback: str) -> tuple[str, str]:
        name, description = fallback, ''
        if content.startswith('---'):
            header = content.split('---', 2)[1] if content.count('---') >= 2 else ''
            for field in ('name', 'description'):
                match = re.search(r'^' + field + r'\s*:\s*(.+)$', header, re.M)
                if match:
                    value = match.group(1).strip().strip('\"\'')[:1000]
                    if value not in ('|', '>'):
                        if field == 'name':
                            name = value
                        else:
                            description = value
        return name, description

    def discover(self, url: str) -> dict:
        target = self.normalize(url)
        tree, truncated = self._tree(target)
        scope = target['path']
        if target['kind'] == 'blob' and PurePosixPath(scope).name == 'SKILL.md':
            scope = str(PurePosixPath(scope).parent)
            if scope == '.':
                scope = ''
        skill_items = [i for i in tree if i.get('type') == 'blob' and i.get('path', '').endswith('SKILL.md')
                       and PurePosixPath(i['path']).name == 'SKILL.md' and self._inside(i['path'], scope)]
        skills = []
        for item in skill_items[:40]:
            if item.get('mode') in ('120000', '160000') or item.get('size', 0) > self.MAX_FILE:
                continue
            f = self._text_file(target, item)
            if not f:
                continue
            directory = str(PurePosixPath(item['path']).parent)
            if directory == '.':
                directory = ''
            name, description = self._frontmatter(f['content'], PurePosixPath(directory).name or target['repo'])
            related = [i for i in tree if i.get('type') == 'blob' and self._inside(i['path'], directory)]
            skills.append({'path': item['path'], 'directory': directory, 'name': name, 'description': description,
                           'content': f['content'], 'url': f['url'], 'repository': target['repository'],
                           'version': target['version'], 'license': target['metadata']['license'],
                           'stars': target['metadata']['stars'], 'updated_at': target['metadata']['updated_at'],
                           'verification_status': 'not_executed', 'files': [{'path': i['path'], 'size': i.get('size'),
                           'mode': i.get('mode')} for i in related[:101]],
                           'package_complete': not truncated and len(related) <= self.MAX_PACKAGE_FILES})
        return {'repository': target['repository'], 'version': target['version'], 'skills': skills,
                'truncated': truncated or len(skill_items) > 40, 'total_found': len(skill_items),
                'metadata': target['metadata'], 'note': '仅发现所读 Git 树中的 SKILL.md；未执行附属脚本。'}

    def search(self, query: str, limit: int = 6) -> dict:
        query = query.strip()
        if not query or len(query) > 240:
            raise GithubError('query', '搜索词须为 1 到 240 个字符')
        limit = max(1, min(limit, 12))
        repos = self._request('/search/repositories', {'q': query + ' skill', 'per_page': limit, 'sort': 'updated'})
        results = []
        for item in repos.get('items', []):
            results.append({'repository': item['full_name'], 'name': item['full_name'], 'url': item['html_url'],
                            'description': item.get('description'), 'stars': item.get('stargazers_count', 0),
                            'updated_at': item.get('updated_at'), 'license': (item.get('license') or {}).get('spdx_id', 'UNKNOWN'),
                            'path': None, 'verification_status': 'not_inspected', 'kind': 'repository'})
        code_available = False
        notes = []
        if self.token:
            try:
                code = self._request('/search/code', {'q': query + ' filename:SKILL.md', 'per_page': limit})
                code_available = True
                for item in code.get('items', []):
                    results.append({'repository': item['repository']['full_name'], 'name': item['name'], 'path': item['path'],
                                    'url': item['html_url'], 'description': item['repository'].get('description'),
                                    'kind': 'skill', 'verification_status': 'not_inspected', 'license': 'UNKNOWN',
                                    'stars': None, 'updated_at': None})
            except GithubError as exc:
                notes.append('代码搜索不可用：' + exc.message + '；已保留仓库搜索结果。')
        else:
            notes.append('未配置 GitHub Token：使用真实仓库搜索。认证代码搜索不可用，进入仓库后可逐个发现技能。')
        return {'query': query, 'effective_query': query + ' skill', 'items': results,
                'total_count': repos.get('total_count'), 'incomplete_results': repos.get('incomplete_results', False),
                'code_search_available': code_available, 'notes': notes,
                'source': 'https://api.github.com/search/repositories', 'fetched_at': int(time.time()),
                'note': 'Stars 仅为仓库关注信息；搜索结果并不穷尽全部技能。'}

    def skill_package(self, url: str, path: str) -> bytes:
        target = self.normalize(url)
        path = self._safe_path(path)
        if PurePosixPath(path).name != 'SKILL.md':
            raise GithubError('skill_path', '请选择具体的 SKILL.md 文件')
        tree, truncated = self._tree(target)
        if truncated:
            raise GithubError('incomplete_tree', '仓库 Git 树被截断，无法保证技能包完整；请缩小仓库或使用本地完整包')
        directory = str(PurePosixPath(path).parent)
        if directory == '.':
            directory = ''
        if not any(i.get('path') == path and i.get('type') == 'blob' for i in tree):
            raise GithubError('not_found', '该提交中未找到所选 SKILL.md')
        selected = [i for i in tree if self._inside(i['path'], directory) and i.get('type') in ('blob', 'commit')]
        # Preserve ancestor license/notice files alongside the skill rather than silently losing their terms.
        ancestors = {''}
        parent = PurePosixPath(directory)
        while str(parent) not in ('', '.'):
            ancestors.add(str(parent))
            parent = parent.parent
        ancestor_licenses = [i for i in tree if i.get('type') == 'blob' and
                             (str(PurePosixPath(i['path']).parent) if str(PurePosixPath(i['path']).parent) != '.' else '') in ancestors and
                             PurePosixPath(i['path']).name.lower().startswith(('license', 'copying', 'notice'))]
        for item in ancestor_licenses:
            if item not in selected:
                selected.append(item)
        if len(selected) > self.MAX_PACKAGE_FILES or sum(i.get('size', 0) for i in selected) > self.MAX_PACKAGE:
            raise GithubError('package_limit', '完整技能包超过 100 个文件或 8 MiB，请使用本地 ZIP 导入')
        root = re.sub(r'[^a-zA-Z0-9_-]+', '-', PurePosixPath(directory).name or target['repo']).strip('-') or 'skill'
        source = {'platform': 'github', 'url': f'https://github.com/{target["repository"]}/blob/{target["version"]}/{quote(path)}',
                  'repository': target['repository'], 'path': path, 'version': target['version']}
        payloads, manifests, total = [], [], 0
        for item in selected:
            self._safe_path(item['path'])
            raw = self._blob(target, item)
            total += len(raw)
            if total > self.MAX_PACKAGE:
                raise GithubError('package_limit', '完整技能包超过 8 MiB')
            rel = item['path'][len(directory) + 1:] if directory and item['path'].startswith(directory + '/') else item['path']
            if rel == 'manifest.json':
                # Existing provenance remains as a reference; generated manifest describes the actual downloaded package.
                rel = 'references/original-manifest.json'
            if any(p == rel for p, _ in payloads):
                raise GithubError('package_collision', '技能包中出现重名文件')
            payloads.append((rel, raw))
            manifests.append({'path': rel, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)})
        if not any(p == 'SKILL.md' for p, _ in payloads):
            raise GithubError('skill_path', '技能包缺少根 SKILL.md')
        manifests.sort(key=lambda i: i['path'])
        content_hash = hashlib.sha256(json.dumps({p: hashlib.sha256(raw).hexdigest() for p, raw in sorted(payloads)},sort_keys=True).encode()).hexdigest()
        manifest = {'format_version': 1, 'name': root, 'version': target['version'], 'source': source,
                    'license': target['metadata']['license'], 'content_hash': content_hash, 'files': manifests,
                    'verification_status': 'not_executed'}
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for rel, raw in payloads:
                info = zipfile.ZipInfo(root + '/' + rel)
                info.external_attr = 0o100644 << 16
                archive.writestr(info, raw)
            archive.writestr(root + '/manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        return output.getvalue()
