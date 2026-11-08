import json
import sqlite3
import pytest
from honcho_ops import receipts
from honcho_ops.caller_receipt_probe import collect


def databases(root, entries):
    for name in ('api', 'deriver'):
        with sqlite3.connect(root / (name + '.sqlite3')) as c:
            c.execute('CREATE TABLE requests(id,started,finished,stage,purpose,model,endpoint,outcome,error_class,http_status)')
            c.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)', entries.get(name, []))


def row(start=100, finish=110):
    return ('same', start, finish, 'deriver', 'representation', 'm', 'host', 'success' if finish else None, None, 200)


def proxy():
    return {'status':'delayed','successful':0,'failed':0,'pending':1,'delayed':1,'stage_coverage':{'deriver':'delayed'},'_unfinished':[{'request_id':'same','started':100,'stage':'deriver','delayed':True}]}


@pytest.mark.parametrize('entries', [
    {'api':[row(),row(102,120)]},
    {'api':[row()], 'deriver':[row(102,120)]},
    {'api':[row(),row(102,None)]},
])
def test_collision_retains_pending(tmp_path, entries):
    databases(tmp_path, entries)
    stores = collect(tmp_path, now=10000, lookup_ids=['same'])
    caller = receipts.caller_receipts(lambda *a, **k: json.dumps(stores), proxy(), now=10000)
    result = receipts.reconcile_proxy_terminals(proxy(), caller)
    assert result['pending'] == result['delayed'] == 1
    assert 'same' in caller['_collisions']


@pytest.mark.parametrize('start,finish', [(96,97),(101,110),(100,float('nan'))])
def test_invalid_temporal_window_retains_pending(start, finish):
    caller = {'_terminals':[dict(id='same',started=start,finished=finish,stage='deriver',outcome='success')]}
    assert receipts.reconcile_proxy_terminals(proxy(), caller)['pending'] == 1


def test_literal_100_lookup_limit(tmp_path):
    databases(tmp_path, {})
    stores = collect(tmp_path, lookup_ids=[str(i) for i in range(101)])
    assert all(s.get('unavailable') for s in stores.values())


@pytest.mark.parametrize('kind,emits', [('orphan',False),('delayed',True),('failed',True)])
def test_main_orphan_quiet_preserves_genuine_alerts(monkeypatch, capsys, kind, emits):
    from honcho_ops import monitor as hm
    route = proxy()
    if kind == 'failed':
        route['failed'] = 1
    caller = {'_terminals':[dict(id='same',started=100,finished=110,stage='deriver',outcome='success')]} if kind != 'delayed' else {}
    reconciled = receipts.reconcile_proxy_terminals(route, caller)
    snapshot = hm.HonchoSnapshot(services={'api_ok':True,'deriver_up':True,'db_ok':True,'redis_ok':True},pipeline={'embedding':{'model':'local','base_url':'http://local','vector_dimensions':'3'}},db={},queue={},queue_by_type={},errors={},spark_goat={'ok':True},deriver={},ingestion={'routing_receipts':reconciled})
    monkeypatch.setattr(hm, 'collect_snapshot', lambda: (snapshot, {}))
    monkeypatch.setattr(hm, 'load_state', lambda _: {})
    monkeypatch.setattr(hm, 'save_state', lambda *a: None)
    assert hm.main() == 0
    assert bool(capsys.readouterr().out) is emits
    assert reconciled['successful'] == 0


def test_literal_100_total_rows(tmp_path):
    databases(tmp_path, {'api':[row(i,i+1) for i in range(80)], 'deriver':[row(i,i+1) for i in range(80)]})
    stores = collect(tmp_path, now=100)
    assert sum(len(s.get('rows',[])) for s in stores.values()) <= 100
    assert any(s.get('truncated') for s in stores.values())
