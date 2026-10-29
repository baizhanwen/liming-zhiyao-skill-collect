"""Explicit GitHub API/optional-association contracts; no real service calls."""
import json
import re

import httpx
import pytest

from backend.connectors.github import GitHubConnector, GithubError
from backend.project_links import resolve_project_links

SHA = 'a' * 40


def api(*, items=None, total=None, incomplete=False, failure=None):
    """Only API GETs are permitted; unexpected source/blob downloads fail."""
    calls = []
    items = items if items is not None else [{'full_name': 'example/Easel',
                                             'html_url': 'https://github.com/example/Easel',
                                             'description': '[契约样例] 同名候选，非真实项目确认',
                                             'stargazers_count': 2, 'license': {'spdx_id': 'MIT'}}]

    def respond(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'api.github.com'
        if failure:
            return failure(request)
        if request.url.path == '/search/repositories':
            return httpx.Response(200, json={'items': items, 'total_count': len(items) if total is None else total,
                                             'incomplete_results': incomplete})
        if re.fullmatch(r'/repos/[\w.-]+/[\w.-]+', request.url.path):
            return httpx.Response(200, json={'default_branch': 'main', 'description': '[契约样例] 来源明确链接',
                                             'stargazers_count': 3, 'license': {'spdx_id': 'MIT'}})
        if request.url.path.endswith('/commits/main'):
            return httpx.Response(200, json={'sha': SHA})
        pytest.fail('Optional association must not fetch source files: ' + request.url.path)

    class RecordingConnector(GitHubConnector):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    connector = RecordingConnector(client=httpx.Client(transport=httpx.MockTransport(respond)))
    return connector, calls


def source(text='视频介绍 Easel 项目。', *, kind='transcript', time='0—120s'):
    return {'platform': 'upload', 'basis': 'audio_transcript', 'snapshot_hash': 'fixture-hash',
            'evidence': [{'id': 'audio-0', 'kind': kind, 'time': time, 'excerpt': text}]}


def mention(name='Easel', quote='视频介绍 Easel 项目。', ids=None):
    return {'name': name, 'quote': quote, 'evidence_ids': ['audio-0'] if ids is None else ids}


def resolve(c, s=None, mentions=None):
    return resolve_project_links(source() if s is None else s,
                                 {'project_mentions': [mention()] if mentions is None else mentions}, lambda: c)


def test_general_repository_search_does_not_append_skill_or_use_code_search():
    c, calls = api()
    result = c.search_repositories('Easel in:name', limit=4)
    assert calls[0].url.params['q'] == 'Easel in:name'
    assert calls[0].url.params['per_page'] == '4'
    assert len(calls) == 1 and '/search/code' not in str(calls[0].url)
    assert result['effective_query'] == result['query'] == 'Easel in:name'
    assert result['items'][0]['verification_status'] == 'not_inspected'


def test_skill_discovery_search_keeps_its_existing_skill_expansion():
    c, calls = api()
    assert c.search('Easel')['effective_query'] == 'Easel skill'
    assert calls[0].url.params['q'] == 'Easel skill'


def test_search_hits_only_use_valid_repository_identity_not_response_html_url():
    c, _ = api(items=[{'full_name': 'example/project', 'html_url': 'https://evil.invalid/private'},
                      {'full_name': 'EXAMPLE/project'}, {'full_name': 'example/project/issues'},
                      {'full_name': '../private'}, {'full_name': 'example/.git'}, 'invalid'])
    result = c.search_repositories('project', limit=12)
    assert len(result['items']) == 1
    assert result['items'][0]['url'] == 'https://github.com/example/project'


@pytest.mark.parametrize('query', ['', ' ' * 20, 'x' * 241])
def test_invalid_general_search_queries_do_not_make_requests(query):
    c, calls = api()
    with pytest.raises(GithubError):
        c.search_repositories(query)
    assert calls == []


def test_explicit_evidence_url_verifies_root_and_fixed_commit_without_download():
    c, calls = api()
    s = source('画面显示 https://github.com/example/Easel/tree/feature/work/docs。',
               kind='model_frame_observation', time='1.91s, 5.74s')
    report = resolve(c, s, [])
    link = report['links'][0]
    assert report['status'] == 'found' and link['status'] == 'repository_verified'
    candidate = link['candidates'][0]
    assert candidate['version'] == SHA and candidate['verification_status'] == 'repository_exists'
    assert candidate['url'] == 'https://github.com/example/Easel'
    assert candidate['same_project_confirmed'] is False
    assert link['evidence'][0]['kind'] == 'model_frame_observation'
    assert link['evidence'][0]['time'] == '1.91s, 5.74s'
    assert link['explicit_urls'] == ['https://github.com/example/Easel/tree/feature/work/docs']
    assert [r.url.path for r in calls] == ['/repos/example/Easel', '/repos/example/Easel/commits/main']
    assert c.closed


@pytest.mark.parametrize('separator', ['，', '。', '；', '：', '！', '？', '、', '）', '】', '》'])
def test_chinese_sentence_punctuation_ends_source_url(separator):
    c, _ = api()
    report = resolve(c, source('链接 https://github.com/example/Easel' + separator + '随后是说明文字。'), [])
    assert report['links'][0]['status'] == 'repository_verified'
    assert report['links'][0]['explicit_urls'] == ['https://github.com/example/Easel']


def test_direct_source_and_user_input_urls_are_provenanced_and_deduplicated():
    c, calls = api()
    s = {'url': 'https://github.com/example/Easel?utm_source=private#readme',
         'original_input': '请核对 github.com/EXAMPLE/Easel.git。', 'evidence': []}
    report = resolve(c, s, [])
    assert len(report['links']) == 1 and len(calls) == 2
    assert {e['kind'] for e in report['links'][0]['evidence']} == {'source_url', 'user_input'}
    assert all('?' not in u and '#' not in u for u in report['links'][0]['explicit_urls'])


def test_does_not_trust_model_urls_or_unsourced_analysis_names():
    def forbidden_factory():
        pytest.fail('No grounded project clue: must not create a connector')
    report = resolve_project_links({'text': 'https://github.com/invented/repo', 'evidence': []},
                                   {'overview': 'Use https://github.com/invented/repo',
                                    'repo_url': 'https://github.com/invented/repo'}, forbidden_factory)
    assert report['status'] == 'none' and report['links'] == []


@pytest.mark.parametrize('text', ['https://example.invalid/path/github.com/invented/repo',
    'https://github.com.evil.invalid/invented/repo', 'https://example.invalid/?next=github.com/invented/repo',
    'https://example.invalid/https://github.com/invented/repo', 'https://user:password@github.com/invented/repo'])
def test_embedded_foreign_urls_and_credentials_are_not_reinterpreted_as_github(text):
    c, calls = api()
    assert resolve(c, source(text), [])['links'] == []
    assert calls == []


def test_unique_search_result_remains_unconfirmed_candidate():
    c, calls = api()
    report = resolve(c)
    link = report['links'][0]
    assert link['status'] == 'candidates' and report['status'] == 'found'
    assert calls[0].url.params['q'] == '"Easel" in:name,description'
    assert link['candidates'][0]['same_project_confirmed'] is False
    assert link['candidates'][0]['verification_status'] == 'search_result'
    assert 'version' not in link['candidates'][0]  # Search does not imply a read commit.
    assert link['evidence'][0] == {'id': 'audio-0', 'kind': 'transcript', 'quote': '视频介绍 Easel 项目。', 'time': '0—120s'}
    assert len(calls) == 1 and c.closed


@pytest.mark.parametrize('items,total,incomplete', [
    ([{'full_name': 'a/Easel'}, {'full_name': 'b/Easel'}], 2, False),
    ([{'full_name': 'a/Easel'}], 10, False),
    ([{'full_name': 'a/Easel'}], 1, True),
])
def test_name_ambiguity_and_incomplete_first_page_never_become_confirmation(items, total, incomplete):
    c, _ = api(items=items, total=total, incomplete=incomplete)
    report = resolve(c)
    assert report['links'][0]['status'] == 'ambiguous'
    assert all(item['same_project_confirmed'] is False for item in report['links'][0]['candidates'])
    assert report['queries'][0]['incomplete_results'] == incomplete


def test_no_match_is_visible_and_does_not_invent_owner_or_repository():
    c, calls = api(items=[])
    report = resolve(c)
    assert report['links'][0]['status'] == 'no_match'
    assert report['links'][0]['candidates'] == []
    assert report['status'] == 'none' and report['errors'] == []
    assert 'example' not in calls[0].url.params['q']


def test_partial_matches_remain_distinct_from_zero_search_results():
    c, _ = api(failure=lambda request: httpx.Response(200, json={'items': [], 'total_count': 0})
               if request.url.path == '/search/repositories'
               else httpx.Response(200, json={'sha': SHA} if '/commits/' in request.url.path else {'default_branch': 'main'}))
    report = resolve(c, source('Easel https://github.com/example/Easel'), [mention('Easel', 'Easel')])
    assert report['status'] == 'partial' and report['errors'] == []
    assert [link['status'] for link in report['links']] == ['repository_verified', 'no_match']


def test_candidate_metadata_is_bounded_scalar_data_even_for_malformed_api_metadata():
    c, _ = api(items=[{'full_name': 'example/Easel', 'description': {'unexpected': 'object'},
                      'stargazers_count': {'count': 10}, 'license': {'spdx_id': ['MIT']}, 'updated_at': []}])
    candidate = resolve(c)['links'][0]['candidates'][0]
    assert candidate['description'] is None and candidate['stars'] is None and candidate['updated_at'] is None
    assert candidate['license'] == 'UNKNOWN'
    c, _ = api(items=[{'full_name': 'example/Easel', 'description': 'x' * 5000,
                      'updated_at': 'y' * 1000, 'license': {'spdx_id': 'z' * 1000}}])
    candidate = resolve(c)['links'][0]['candidates'][0]
    assert len(candidate['description']) == 2000 and len(candidate['updated_at']) == 100 and len(candidate['license']) == 200


@pytest.mark.parametrize('value', [
    mention('Invented', '视频介绍 Easel 项目。'),
    mention('Easel', 'Easel 是另一个原文未说的项目'),
    mention(ids=['missing']), mention(ids=[]), mention(ids=['audio-0', 'missing']),
    mention('', '视频介绍 Easel 项目。'), mention('x' * 121),
    mention('Easel', ''), mention('Easel', 'x' * 601),
    mention('Easel', '视频介绍 EASEL 项目。'),  # A quote is not case-corrected.
    {'name': 'Easel', 'quote': 'Easel', 'evidence_ids': 'audio-0'}, None,
])
def test_ungrounded_or_malformed_mentions_are_nonfatal_and_never_queried(value):
    c, calls = api()
    report = resolve(c, mentions=[value])
    assert report['links'][0]['status'] == 'invalid'
    assert report['links'][0]['error']['code'] == 'invalid_evidence'
    assert calls == []
    assert not c.closed  # A connector was never created.


def test_name_case_and_quote_whitespace_are_normalized_without_asr_spelling_correction():
    c, calls = api()
    s = source('视频介绍\tEasel\n项目。')
    report = resolve(c, s, [mention('easel', '视频介绍 Easel 项目。')])
    assert report['links'][0]['status'] == 'candidates'
    assert len(calls) == 1
    c, calls = api()
    report = resolve(c, source('项目名是易搜。'), [mention('Easel', '项目名是易搜。')])
    assert report['links'][0]['status'] == 'invalid' and calls == []


def test_duplicate_evidence_ids_cannot_ground_a_name():
    c, calls = api()
    s = source()
    s['evidence'].append({'id': 'audio-0', 'kind': 'text', 'excerpt': 'Different source.'})
    assert resolve(c, s)['links'][0]['status'] == 'invalid'
    assert calls == []


def test_duplicate_names_merge_original_evidence_without_repeating_search():
    c, calls = api()
    s = source()
    s['evidence'].append({'id': 'frame', 'kind': 'model_frame_observation', 'excerpt': '屏幕写有 Easel。', 'time': '4.0s'})
    report = resolve(c, s, [mention(), mention('easel', '屏幕写有 Easel。', ['frame']), mention()])
    assert len(calls) == len(report['queries']) == len(report['links']) == 1
    assert {e['kind'] for e in report['links'][0]['evidence']} == {'transcript', 'model_frame_observation'}


def test_plain_text_basis_never_gets_a_fabricated_timestamp_or_audio_label():
    c, _ = api()
    s = {'basis': 'text_only', 'evidence': [{'id': 'text-1', 'kind': 'text', 'excerpt': '平台AI摘要提到 Easel。'}]}
    result = resolve(c, s, [mention('Easel', '平台AI摘要提到 Easel。', ['text-1'])])
    assert result['links'][0]['evidence'] == [{'id': 'text-1', 'kind': 'text', 'quote': '平台AI摘要提到 Easel。'}]


def test_literal_search_terms_do_not_allow_source_to_inject_github_qualifiers():
    c, calls = api()
    name = 'Easel" repo:secret/private OR other'
    resolve(c, source(name), [mention(name, name)])
    assert calls[0].url.params['q'] == '"Easel repo secret/private OR other" in:name,description'


@pytest.mark.parametrize('status,headers,code', [(403, {'x-ratelimit-remaining': '0'}, 'rate_limit'),
    (401, {}, 'authentication'), (403, {}, 'permission'), (404, {}, 'not_found'), (429, {}, 'rate_limit')])
def test_api_failures_are_visible_without_disclosing_response_or_failing_media(status, headers, code):
    c, calls = api(failure=lambda r: httpx.Response(status, headers=headers, json={'message': 'SYNTHETIC_PRIVATE_RESPONSE'}))
    s = source()
    before = json.dumps(s, sort_keys=True)
    report = resolve(c, s)
    assert report['status'] == 'unavailable'
    assert report['links'][0]['error']['code'] == code
    assert 'SYNTHETIC_PRIVATE_RESPONSE' not in json.dumps(report)
    assert json.dumps(s, sort_keys=True) == before
    assert len(calls) == 1 and c.closed


def test_one_external_failure_does_not_discard_other_verified_project():
    c, _ = api(failure=lambda request: httpx.Response(429) if request.url.path == '/search/repositories'
               else httpx.Response(200, json={'sha': SHA} if '/commits/' in request.url.path else {'default_branch': 'main'}))
    report = resolve(c, source('Easel https://github.com/example/Easel'), [mention('Easel', 'Easel')])
    assert report['status'] == 'partial'
    assert [link['status'] for link in report['links']] == ['repository_verified', 'unavailable']


def test_explicit_http_url_is_not_upgraded_or_requested():
    c, calls = api()
    report = resolve(c, source('http://github.com/example/Easel'), [])
    assert report['links'][0]['status'] == 'invalid' and calls == []


def test_request_query_candidate_and_scan_limits_are_visible(monkeypatch):
    import backend.project_links as module
    c, calls = api(items=[{'full_name': 'one/repo'}, {'full_name': 'two/repo'}, {'full_name': 'THREE/repo'}, {'full_name': 'three/repo'}])
    monkeypatch.setattr(module, 'MAX_CANDIDATES', 2)
    s = source('Alpha Beta Gamma Delta')
    report = resolve(c, s, [mention(name, name) for name in ('Alpha', 'Beta', 'Gamma', 'Delta')])
    assert len(calls) == len(report['queries']) == 3
    assert report['links'][-1]['status'] == 'unavailable' and report['links'][-1]['error']['code'] == 'budget'
    assert all(len(link['candidates']) <= 2 for link in report['links'])
    assert report['limits']['unique_candidates'] == 2
    assert report['limits']['truncated'] and report['warnings']
    c, calls = api()
    s = source(' '.join(f'https://github.com/owner/repo{i}' for i in range(6)))
    report = resolve(c, s, [])
    assert len(report['links']) == 4 and len(calls) == 8
    assert report['limits']['truncated']


def test_scan_and_reference_limits_do_not_grow_unbounded(monkeypatch):
    import backend.project_links as module
    c, calls = api()
    monkeypatch.setattr(module, 'MAX_SCAN_CHARS', 10)
    report = resolve(c, source('x' * 10 + ' https://github.com/example/Easel'), [])
    assert not report['links'] and not calls and report['limits']['truncated']
    monkeypatch.setattr(module, 'MAX_SCAN_CHARS', 200_000)
    s = {'evidence': [{'id': str(i), 'excerpt': 'github.com/example/Easel', 'kind': 'transcript'} for i in range(100)]}
    report = resolve(c, s, [])
    assert len(report['links'][0]['evidence']) == 8
    assert report['limits']['truncated'] and len(calls) == 2


def test_elapsed_optional_budget_does_not_start_a_network_request(monkeypatch):
    import backend.project_links as module
    c, calls = api()
    monkeypatch.setattr(module, 'DEADLINE_SECONDS', 0)
    report = resolve(c)
    assert report['links'][0]['error']['code'] == 'budget' and not calls


def test_connector_factory_and_close_errors_are_contained_without_exception_text():
    def fail():
        raise RuntimeError('SYNTHETIC_PRIVATE_EXCEPTION')
    report = resolve_project_links(source(), {'project_mentions': [mention()]}, fail)
    assert report['status'] == 'unavailable'
    assert 'SYNTHETIC_PRIVATE_EXCEPTION' not in json.dumps(report)
    c, _ = api()
    c.close = fail
    assert resolve(c)['status'] == 'found'


def test_resolver_does_not_mutate_saved_source_or_analysis():
    c, _ = api()
    s = source('Easel https://github.com/example/Easel')
    analysis = {'overview': 'Original analysis', 'project_mentions': [mention('Easel', 'Easel')]}
    before = json.dumps({'s': s, 'analysis': analysis}, sort_keys=True)
    resolve_project_links(s, analysis, lambda: c)
    assert json.dumps({'s': s, 'analysis': analysis}, sort_keys=True) == before
