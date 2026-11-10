"""Real SQLite collector -> classifier -> renderer -> main, no live IO."""
import json
import sqlite3
from test_alert_noise import snapshot, run
from honcho_ops import receipts
from honcho_ops.caller_receipt_probe import collect


def test_natural_receipt_shape_full_path(tmp_path,monkeypatch,capsys):
    routes=tmp_path/'routes.sqlite3'
    with sqlite3.connect(routes) as c:
        c.execute('CREATE TABLE routes(id,started,finished,status,error_class,operation,model,endpoint,caller)')
        c.execute('CREATE TABLE correlations(route_id,stage,request_id)')
        c.executemany('INSERT INTO routes VALUES(?,?,?,?,?,?,?,?,?)',[
            ('timeout',100,220,None,'TimeoutError','chat','m','host','honcho'),
            ('retry',220.5,256,200,None,'chat','m','host','honcho')])
        c.executemany('INSERT INTO correlations VALUES(?,?,?)', [('timeout','deriver','request'),('retry','deriver','request')])
    for name in ('api','deriver'):
        with sqlite3.connect(tmp_path/(name+'.sqlite3')) as c:
            c.execute('CREATE TABLE requests(id,started,finished,stage,purpose,model,endpoint,outcome,error_class,http_status)')
            if name=='deriver':
                c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',('request',99.9,256.1,'deriver','representation','m','host','success',None,None))
    proxy=receipts.routing_receipts(tmp_path,path=routes,now=300)
    stores=collect(tmp_path,now=300,lookup_ids=proxy['request_ids'])
    caller=receipts.caller_receipts(lambda *a,**k:json.dumps(stores),proxy,now=300)
    route=receipts.reconcile_proxy_terminals(proxy,caller)
    snap=snapshot(route); snap.ingestion['caller_receipts']=caller
    assert route['failed']==route['recovered_attempts']==1
    assert route['status']=='recovered'
    assert run(monkeypatch,capsys,snap,{})[0]==''
