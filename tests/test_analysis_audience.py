"""Audience-shape compatibility uses explicit mock replies; no supplier calls."""
import copy
import json

import httpx
import pytest

from backend.models import ModelService
from backend.security import AppError, Vault
from backend.store import Store


SOURCE = {'title': '[契约] 资料', 'text': '材料仅描述核对来源。', 'basis': 'subtitles',
          'snapshot_hash': 'audience-contract-snapshot',
          'evidence': [{'id': 'source-1', 'kind': 'transcript', 'excerpt': '核对来源'}]}


def configured(tmp_path, audience, claim=None):
    store = Store(tmp_path)
    vault = Vault(tmp_path)
    store.put('model_configs', {'id': 'text', 'base_url': 'https://contract.invalid/v1', 'model': 'audience-contract'})
    output = {'overview': '[契约] 核对来源', 'purpose': '测试确定性兼容', 'audience': audience,
              'steps': ['核对来源'], 'dependencies': [], 'limitations': ['未实际运行'], 'methods': ['保留引用'],
              'claims': [claim or {'text': '材料描述核对来源', 'evidence_ids': ['source-1'], 'kind': 'source_claim'}]}
    raw = json.dumps(output, ensure_ascii=False, indent=2)
    calls = []

    def reply(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == 'https://contract.invalid/v1/chat/completions'
        return httpx.Response(200, json={'model': 'audience-contract',
            'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 31}})

    models = ModelService(store, vault, httpx.Client(transport=httpx.MockTransport(reply)))
    return store, models, raw, calls


@pytest.mark.parametrize('audience', [
    ['独立开发者'],
    ['内容创作者', '  本地软件维护者  ', '使用\n已有工作台的人'],
    [f'对象 {index}' for index in range(8)],
])
def test_string_list_is_normalized_once_without_paid_repair_or_raw_changes(tmp_path, audience):
    original_audience = copy.deepcopy(audience)
    source = copy.deepcopy(SOURCE)
    store, models, raw, calls = configured(tmp_path, audience)
    result = models.analyze(source)  # Default repair budget remains available but unnecessary.
    assert len(calls) == 1
    assert result['audience'] == '\n'.join(original_audience)
    assert result['output_normalizations'] == ['audience_string_list']
    assert result['evidence'] == SOURCE['evidence'] and result['claims'][0]['evidence_ids'] == ['source-1']
    assert result['usage'] == {'total_tokens': 31}
    assert source == SOURCE and audience == original_audience
    runs = store.list('model_runs')
    assert len(runs) == 1
    run = store.get('model_runs', result['model_run_id'])
    assert run['response'] == raw
    assert json.loads(run['response'])['audience'] == original_audience
    assert run['validation'] == 'passed' and run['output_normalizations'] == ['audience_string_list']
    prompt = json.loads(calls[0]['messages'][1]['content'])
    assert prompt['output_schema']['properties']['audience']['type'] == 'string'


def test_original_string_remains_unchanged_and_unmarked(tmp_path):
    audience = '  原有字符串\n完整保留  '
    store, models, raw, calls = configured(tmp_path, audience)
    result = models.analyze(copy.deepcopy(SOURCE))
    assert len(calls) == 1 and result['audience'] == audience
    assert result['output_normalizations'] == []
    run = store.get('model_runs', result['model_run_id'])
    assert run['response'] == raw and run['output_normalizations'] == []


@pytest.mark.parametrize('audience', [
    [], [''], [' \t\n'], ['valid', ''], ['valid', None], ['valid', 3],
    ['valid', False], ['valid', ['nested']], [{'nested': 'value'}],
    [f'对象 {index}' for index in range(9)],
])
def test_invalid_lists_still_fail_schema_validation_and_keep_raw(tmp_path, audience):
    store, models, raw, calls = configured(tmp_path, audience)
    with pytest.raises(AppError) as caught:
        models.analyze(copy.deepcopy(SOURCE), max_repairs=0)
    assert caught.value.code == 'output_format' and len(calls) == 1
    run = store.get('model_runs', caught.value.diagnostic_id)
    assert run['response'] == raw and run['validation'] == 'failed'
    assert run['output_normalizations'] == []
    assert any(item.get('field') == 'audience' for item in run['validation_errors'])


@pytest.mark.parametrize('claim', [
    {'text': '未知证据', 'evidence_ids': ['not-in-source'], 'kind': 'source_claim'},
    {'text': '缺证据', 'evidence_ids': [], 'kind': 'source_claim'},
    {'text': '非法类型', 'evidence_ids': ['source-1'], 'kind': 'executed'},
])
def test_audience_compatibility_does_not_relax_claim_or_evidence_checks(tmp_path, claim):
    store, models, raw, calls = configured(tmp_path, ['维护者'], claim)
    with pytest.raises(AppError) as caught:
        models.analyze(copy.deepcopy(SOURCE), max_repairs=0)
    assert caught.value.code == 'output_format' and len(calls) == 1
    run = store.get('model_runs', caught.value.diagnostic_id)
    assert run['response'] == raw and run['validation'] == 'failed'
    assert run['output_normalizations'] == ['audience_string_list']
    assert run['validation_errors']


def test_invalid_list_keeps_the_existing_bounded_repair_policy(tmp_path):
    store, models, raw, calls = configured(tmp_path, ['valid', None])
    with pytest.raises(AppError) as caught:
        models.analyze(copy.deepcopy(SOURCE))
    assert caught.value.code == 'output_format' and len(calls) == 2
    assert len(calls[1]['messages']) == len(calls[0]['messages']) + 2
    assert len(store.list('model_runs')) == 2
    assert all(run['response'] == raw and run['validation'] == 'failed' for run in store.list('model_runs'))
