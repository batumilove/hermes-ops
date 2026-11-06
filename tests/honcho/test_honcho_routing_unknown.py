"""Protected real-parser uncertainty, precedence and bounded-input contracts."""
import json
from dataclasses import replace
import pytest
from honcho_ops import monitor as hm


def test_missing_target_model_never_invents_historical_model():
    assert hm.select_spark_model({'summary': {'base_url': SPARK}}) == ''

SPARK = 'http://192.168.10.211:8001/v1'

@pytest.mark.parametrize('raw', ['', '__SSH_ERROR__ secret', 'DERIVER_MODEL_CONFIG={bad',
    'DERIVER_MODEL_CONFIG=[]', 'CONFIG_FILE=/hidden/settings.toml',
    'DERIVER_MODEL_CONFIG__MODEL=cloud'])
def test_unresolved_config_is_explicit_unknown(raw):
    pipeline = hm.parse_pipeline_env(raw)
    assert pipeline['_routing']['status'] == 'unknown'
    assert 'secret' not in json.dumps(pipeline)


def test_env_only_never_proves_toml_defaults_or_worker_config():
    pipeline = hm.parse_pipeline_env('DERIVER_MODEL_CONFIG__MODEL=cloud\nDERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL=https://cloud.example/v1')
    assert pipeline['_routing']['status'] == 'unknown'
    assert pipeline['_routing']['reason'] == 'effective config sources unverified'

@pytest.mark.parametrize('reverse', [False, True])
def test_nested_leaf_wins_parent_independent_of_env_order(reverse):
    lines = ['DERIVER_MODEL_CONFIG='+json.dumps({'model':'parent','overrides':{'base_url':SPARK}}),
             'DERIVER_MODEL_CONFIG__MODEL=leaf',
             'DERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL=https://cloud.example/v1']
    pipeline = hm.parse_pipeline_env('\n'.join(reversed(lines) if reverse else lines))
    assert not any(hm.is_spark_goat_chat_url(x.get('base_url')) for x in pipeline.values())
    assert pipeline['deriver']['model'] == 'leaf'


def test_nested_fallback_list_keeps_each_model():
    pipeline = hm.parse_pipeline_env('DERIVER_MODEL_CONFIG='+json.dumps({'fallback':[{'model':'one','overrides':{'base_url':SPARK}}, {'model':'two','overrides':{'base_url':SPARK}}]}))
    assert {x['model'] for x in pipeline.values() if hm.is_spark_goat_chat_url(x.get('base_url'))} == {'one','two'}

@pytest.mark.parametrize('raw', ['X='+'x'*1100000, 'DERIVER_MODEL_CONFIG='+('['*80)+(']'*80)], ids=['oversize','deep'])
def test_oversize_or_deep_input_is_bounded_unknown(raw):
    assert hm.parse_pipeline_env(raw)['_routing']['status'] == 'unknown'

@pytest.mark.parametrize('url', ['https://evil.example/192.168.10.211:8001', 'http://192.168.10.211:8001.evil.example/v1', 'http://192.168.10.211:80010/v1'])
def test_host_alias_substrings_are_not_target_identity(url):
    assert not hm.is_spark_goat_chat_url(url)


def test_unknown_report_separates_routing_advisory_from_service_health():
    snapshot = hm.HonchoSnapshot(services={'api_ok':True,'deriver_up':True,'db_ok':True,'redis_ok':True},
        pipeline=hm.parse_pipeline_env(''), db={},queue={},queue_by_type={},errors={},
        spark_goat={'skipped':True,'routing_unknown':True},deriver={})
    alerts=[]
    hm._spark_alerts(snapshot,None,alerts)
    assert alerts == []
    report=hm.format_report(snapshot)
    assert 'Inspected configuration: UNKNOWN' in report
    assert 'Actual request routing: UNKNOWN / UNVERIFIED' in report
    assert 'not evidence of outage' in report
    assert 'not routed' not in report
    assert 'All nominal' not in report
