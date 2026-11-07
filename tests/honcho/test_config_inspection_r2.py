"""R2 real main emission policy; all IO is isolated from production."""
import json
from dataclasses import replace
import pytest
from honcho_ops import monitor as hm
from honcho_ops.config_inspection import KEYS
from test_config_inspection import wire
from test_honcho_report_evidence import _healthy_snapshot


@pytest.mark.parametrize('container', ['api', 'deriver'])
@pytest.mark.parametrize('index', range(4))
def test_main_emits_observed_missing_input(container, index, monkeypatch, capsys):
    payload = json.loads(wire())
    payload['containers'][container]['present'][index] = False
    receipt = hm.collect_config_inspection(lambda *a, **k: json.dumps(payload), now=100)
    snapshot = replace(_healthy_snapshot({'skipped': True}), inspected_config=receipt)
    run_main(snapshot, monkeypatch)
    output = capsys.readouterr().out
    assert 'Configuration warning: observed required input missing' in output
    assert container in output and KEYS[index] in output
    assert 'effective precedence UNVERIFIED' in output
    assert 'Service/processing checks: nominal' in output
    assert 'private-secret' not in output


def run_main(snapshot, monkeypatch):
    monkeypatch.delenv('HONCHO_MONITOR_ALWAYS_PRINT', raising=False)
    monkeypatch.setattr(hm, 'collect_snapshot', lambda: (snapshot, {}))
    monkeypatch.setattr(hm, 'load_state', lambda *a: {})
    monkeypatch.setattr(hm, 'save_state', lambda *a: None)
    assert hm.main() == 0


@pytest.mark.parametrize('payload', [wire(), wire(0), '{}', '__SSH_ERROR__ private-secret'])
def test_main_healthy_or_unobservable_silent(payload, monkeypatch, capsys):
    receipt = hm.collect_config_inspection(lambda *a, **k: payload, now=100)
    snapshot = replace(_healthy_snapshot({'skipped': True}), inspected_config=receipt)
    run_main(snapshot, monkeypatch)
    assert capsys.readouterr().out == ''
    if receipt['status'] == 'unavailable':
        assert 'UNVERIFIED' in hm.format_report(snapshot)
        assert 'observed required input missing' not in hm.format_report(snapshot)


def test_main_timeout_preserves_real_fault(monkeypatch, capsys):
    def timeout(*a, **k):
        raise TimeoutError('private-secret')
    receipt = hm.collect_config_inspection(timeout, now=100)
    snapshot = replace(_healthy_snapshot({'ok': False}), inspected_config=receipt)
    run_main(snapshot, monkeypatch)
    output = capsys.readouterr().out
    assert 'spark-goat chat failed' in output
    assert 'observed required input missing' not in output
    assert 'private-secret' not in output
