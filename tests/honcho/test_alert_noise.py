"""Frozen content-free shape from November 10 natural timeout/retry receipts."""
import copy
import pytest
from honcho_ops import receipts, monitor as hm


def proxy():
    return dict(status='failed', failed=1, successful=1, delayed=0, pending=0,
                stage_coverage={'deriver':'failed'}, _unfinished=[],
                _failed_attempts=[dict(id='attempt-a',request_id='caller-a',started=100,finished=220,stage='deriver')])


def caller(outcome='success'):
    return dict(_terminals=[dict(id='caller-a',started=99.9,finished=256,stage='deriver',outcome=outcome)])


def snapshot(route):
    return hm.HonchoSnapshot(services={'api_ok':True,'deriver_up':True,'db_ok':True,'redis_ok':True},pipeline={'embedding':{'model':'local','base_url':'http://local','vector_dimensions':'3'}},db={},queue={},queue_by_type={},errors={},spark_goat={'ok':True},deriver={},ingestion={'routing_receipts':route})


def run(monkeypatch,capsys,snap,state):
    saved=[]
    monkeypatch.setattr(hm,'collect_snapshot',lambda:(snap,{}))
    monkeypatch.setattr(hm,'load_state',lambda _:copy.deepcopy(state))
    monkeypatch.setattr(hm,'save_state',lambda _,s:saved.append(copy.deepcopy(s)))
    assert hm.main()==0
    return capsys.readouterr().out,saved[0]


def test_recovered_main_silent(monkeypatch,capsys):
    result=receipts.reconcile_proxy_terminals(proxy(),caller())
    assert result.get('recovered_attempts')==1
    assert result['failed']==1  # preserve raw evidence
    assert run(monkeypatch,capsys,snapshot(result),{})[0]==''


@pytest.mark.parametrize('change',[{'outcome':'error'},{'finished':200},{'started':101},{'stage':'embedding'}])
def test_non_recovery_emits(monkeypatch,capsys,change):
    evidence=caller(); evidence['_terminals'][0].update(change)
    result=receipts.reconcile_proxy_terminals(proxy(),evidence)
    assert run(monkeypatch,capsys,snapshot(result),{})[0]


def test_unknown_collision_not_quiet(monkeypatch,capsys):
    evidence=caller(); evidence['_collisions']=['caller-a']
    result=receipts.reconcile_proxy_terminals(proxy(),evidence)
    assert run(monkeypatch,capsys,snapshot(result),{})[0]


def test_dedup_new_fault_reminder_recovery(monkeypatch,capsys):
    clock=[10000.0]; monkeypatch.setattr(hm.time,'time',lambda:clock[0])
    route=receipts.reconcile_proxy_terminals(proxy(),caller('error'))
    snap=snapshot(route)
    out,state=run(monkeypatch,capsys,snap,{})
    assert out
    clock[0]+=1800
    out,state=run(monkeypatch,capsys,snap,state)
    assert out==''
    clock[0]+=14400
    out,state=run(monkeypatch,capsys,snap,state)
    assert out
    changed=copy.deepcopy(snap); changed.ingestion['routing_receipts']['fault_ids']=['new-attempt']
    out,state=run(monkeypatch,capsys,changed,state)
    assert out
    healthy=snapshot({'status':'observed'})
    out,state=run(monkeypatch,capsys,healthy,state)
    assert 'no longer observed' in out.lower()
    assert run(monkeypatch,capsys,healthy,state)[0]==''
