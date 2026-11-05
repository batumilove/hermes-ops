"""Protected review witnesses: exercise real parser, never mock its output."""
from unittest.mock import Mock
import pytest
from honcho_ops import monitor as hm

@pytest.mark.parametrize('key', [
'DREAM_INDUCTION_MODEL_CONFIG__OVERRIDES__BASE_URL',
'DIALECTIC_LEVELS__minimal__MODEL_CONFIG__OVERRIDES__BASE_URL',
'DIALECTIC_LEVELS__medium__MODEL_CONFIG__OVERRIDES__BASE_URL',
'DIALECTIC_LEVELS__high__MODEL_CONFIG__OVERRIDES__BASE_URL',
'DIALECTIC_LEVELS__max__MODEL_CONFIG__OVERRIDES__BASE_URL',
'DERIVER_MODEL_CONFIG__FALLBACK__OVERRIDES__BASE_URL',
'SUMMARY_MODEL_CONFIG__FALLBACK__OVERRIDES__BASE_URL',
])
def test_real_parser_preserves_every_routed_spark_dependency(key):
    pipeline=hm.parse_pipeline_env(key+'=http://192.168.10.211:8001/v1')
    assert any(hm.is_spark_goat_chat_url(x.get('base_url')) for x in pipeline.values())

@pytest.mark.parametrize('key', ['DERIVER_MODEL_CONFIG','DREAM_INDUCTION_MODEL_CONFIG','DIALECTIC_LEVELS'])
def test_json_parent_routes_are_not_lost(key):
    raw=key+'={"fallback":{"model":"routed-model","overrides":{"base_url":"http://192.168.10.211:8001/v1"}}}'
    pipeline=hm.parse_pipeline_env(raw)
    assert any(hm.is_spark_goat_chat_url(x.get('base_url')) for x in pipeline.values())


def test_non_deriver_routed_model_is_selected():
    assert hm.select_spark_model({'summary':{'base_url':'http://192.168.10.211:8001/v1','model':'actual-served'}})=='actual-served'
