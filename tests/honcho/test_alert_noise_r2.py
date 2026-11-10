from test_alert_noise import snapshot, run
from honcho_ops import monitor as hm


def test_replaced_stale_work_emits(monkeypatch, capsys):
    snap = snapshot({'status': 'observed'})
    snap.deriver.update(active_count=1, active_oldest_age_s=1000,
                        active_oldest_work_unit_key='representation:ID-A')
    out, state = run(monkeypatch, capsys, snap, {})
    assert out
    assert run(monkeypatch, capsys, snap, state)[0] == ''
    snap.deriver['active_oldest_work_unit_key'] = 'representation:ID-B'
    assert run(monkeypatch, capsys, snap, state)[0]


def test_dimensions_material_change_emits(monkeypatch, capsys):
    snap = snapshot({'status': 'observed'})
    snap.db['documents_dims'] = 4
    out, state = run(monkeypatch, capsys, snap, {})
    assert out
    snap.db['documents_dims'] = 5
    assert run(monkeypatch, capsys, snap, state)[0]


def test_disk_worsening_emits(monkeypatch, capsys):
    snap = snapshot({'status': 'observed'})
    snap.observer.update(disk_free_bytes=1000000000, disk_used_percent=95)
    out, state = run(monkeypatch, capsys, snap, {})
    assert out
    snap.observer.update(disk_free_bytes=100000000, disk_used_percent=99)
    assert run(monkeypatch, capsys, snap, state)[0]


def test_stale_age_bucket_and_reminder(monkeypatch, capsys):
    clock = [10000.0]
    monkeypatch.setattr(hm.time, 'time', lambda: clock[0])
    snap = snapshot({'status': 'observed'})
    snap.deriver.update(active_count=1, active_oldest_age_s=1000,
                        active_oldest_work_unit_key='representation:ID-A')
    out, state = run(monkeypatch, capsys, snap, {})
    assert out
    snap.deriver['active_oldest_age_s'] = 1100
    assert run(monkeypatch, capsys, snap, state)[0] == ''
    snap.deriver['active_oldest_age_s'] = 2500
    out, state = run(monkeypatch, capsys, snap, state)
    assert out
    clock[0] += 14399
    assert run(monkeypatch, capsys, snap, state)[0] == ''
    clock[0] += 1
    assert run(monkeypatch, capsys, snap, state)[0]
