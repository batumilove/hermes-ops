"""Read-only inspection receipts do not attest effective configuration."""
import json
import subprocess
import sys
from dataclasses import replace

from honcho_ops import monitor as hm
from test_honcho_report_evidence import _healthy_snapshot


def wire(now=100):
    return json.dumps({'version': 1, 'collected_at': now, 'containers': {
        name: {'present': [True]*4, 'config_source_sha256': 'a'*64,
               'toml_present': False, 'dotenv_present': False}
        for name in ('api', 'deriver')}})


def test_collects_both_containers_without_values():
    calls=[]
    def read(command, timeout):
        calls.append((command,timeout))
        return wire()
    result=hm.collect_config_inspection(read, now=100)
    assert result['status']=='inspected'
    assert result['effective_status']=='unverified'
    assert result['containers']['api']['present']==[True]*4
    assert len(calls)==1 and calls[0][1]<=20
    assert "'docker','exec'" in __import__('shlex').split(calls[0][0])[-1]
    assert 'honcho-api-1' in calls[0][0] and 'honcho-deriver-1' in calls[0][0]


def test_rejects_stale_partial_and_untrusted_payloads():
    for payload in (wire(0), wire(101), '{}', '__SSH_ERROR__ secret',
                    wire().replace('true','1'), wire()+'secret',
                    wire().replace('"deriver"','"unknown"')):
        result=hm.collect_config_inspection(lambda *a, **k:payload, now=100)
        assert result['status']=='unavailable'
        assert 'secret' not in json.dumps(result)


def test_missing_inputs_not_configuration_pass():
    payload=json.loads(wire()); payload['containers']['api']['present'][0]=False
    result=hm.collect_config_inspection(lambda *a,**k:json.dumps(payload),now=100)
    assert result['status']=='incomplete'
    assert result['effective_status']=='unverified'


def test_receipt_report_is_scoped_and_not_cached_routing_proof():
    snapshot=replace(_healthy_snapshot({'skipped':True}),
                     inspected_config=hm.collect_config_inspection(lambda *a,**k:wire(),now=100))
    report=hm.format_report(snapshot)
    assert 'Inspected configuration: INSPECTED' in report
    assert 'effective precedence UNVERIFIED' in report
    assert 'Actual request routing: UNKNOWN / UNVERIFIED' in report
    assert hm.build_alerts(snapshot)==hm.build_alerts(replace(snapshot,inspected_config={}))


def test_remote_payload_executes_without_settings_import_or_value_leak(tmp_path,monkeypatch):
    # Real generated program, inert Docker executable; never production SSH.
    docker=tmp_path/'docker'
    docker.write_text('#!'+sys.executable+'\nimport sys,subprocess\np=sys.argv.index("-c")\nr=subprocess.run([sys.executable,"-c",sys.argv[p+1]],capture_output=True)\nsys.stdout.buffer.write(r.stdout)\nsys.exit(r.returncode)\n')
    docker.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path)+':/usr/bin:/bin')
    monkeypatch.setenv('EMBEDDING_MODEL_CONFIG__MODEL','private-secret-value')
    result=subprocess.run(['/bin/sh','-c',hm.config_inspection_command()],capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert 'private-secret-value' not in result.stdout+result.stderr
    payload=json.loads(result.stdout)
    assert set(payload['containers'])=={'api','deriver'}
    assert payload['containers']['api']['present'][0] is True
