import json
import sqlite3
from pathlib import Path
from honcho_ops import receipts
from honcho_ops.caller_receipt_probe import collect


def test_exact_terminal_outside_window_is_queried(tmp_path):
    for name in ('api', 'deriver'):
        with sqlite3.connect(tmp_path / (name + '.sqlite3')) as c:
            c.execute('CREATE TABLE requests(id,started,finished,stage,purpose,model,endpoint,outcome,error_class,http_status)')
            if name == 'deriver':
                c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)', ('request', 1, 12, 'deriver', 'representation', 'm', 'https://host', 'success', None, 200))
    result = collect(tmp_path, now=10000, lookup_ids=['request'])
    assert result['deriver']['rows'][0]['finished'] == 12


def test_terminal_reconciles_without_fabricating_proxy_success():
    proxy = {'status':'delayed','successful':2,'failed':0,'pending':1,'delayed':1,
             'stage_coverage':{'deriver':'delayed'},
             '_unfinished':[{'request_id':'request','started':1,'stage':'deriver','delayed':True}]}
    caller = {'_terminals':[{'id':'request','started':1,'finished':12,'stage':'deriver','outcome':'success'}]}
    result = receipts.reconcile_proxy_terminals(proxy, caller)
    assert result['status'] == 'observed'
    assert result['pending'] == result['delayed'] == 0
    assert result['successful'] == 2
    assert result['historical_terminal_missing'] == 1
    assert result['stage_coverage']['deriver'] == 'historical_unknown'
    assert '_unfinished' not in result


def test_live_or_ambiguous_requests_still_alert():
    for terminal in (None, {'id':'other','started':1,'finished':12,'stage':'deriver','outcome':'success'}, {'id':'request','started':50,'finished':60,'stage':'deriver','outcome':'success'}):
        proxy = {'status':'delayed','successful':0,'failed':0,'pending':1,'delayed':1,'stage_coverage':{'deriver':'delayed'},'_unfinished':[{'request_id':'request','started':1,'stage':'deriver','delayed':True}]}
        result = receipts.reconcile_proxy_terminals(proxy, {'_terminals': [terminal] if terminal else []})
        assert result['status'] == 'delayed'
        assert result['delayed'] == 1


def test_failed_proxy_is_never_suppressed():
    result = receipts.reconcile_proxy_terminals({'status':'failed','failed':1,'pending':0,'delayed':0,'stage_coverage':{},'_unfinished':[]}, {})
    assert result['status'] == 'failed'
