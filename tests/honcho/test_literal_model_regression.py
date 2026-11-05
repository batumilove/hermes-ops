"""Reviewer-owned literal string model IDs; no external calls or state."""
import pytest
from honcho_ops import monitor as hm

@pytest.mark.parametrize('model', ['true', 'false', '1e3', 'null'])
@pytest.mark.parametrize('prefix', ['DERIVER_MODEL_CONFIG', 'DREAM_INDUCTION_MODEL_CONFIG', 'SUMMARY_MODEL_CONFIG__FALLBACK'])
def test_leaf_model_id_remains_literal(model, prefix):
    raw = prefix + '__MODEL=' + model + '\n' + prefix + '__OVERRIDES__BASE_URL=http://192.168.10.211:8001/v1'
    pipeline = hm.parse_pipeline_env(raw)
    assert hm.select_spark_model(pipeline) == model
