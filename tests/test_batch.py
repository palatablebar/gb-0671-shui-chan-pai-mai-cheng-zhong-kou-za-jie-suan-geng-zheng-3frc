import json, zipfile, io
import pytest
from fastapi.testclient import TestClient
from app import main, store

BASE = open('sample_data/clean.csv', encoding='utf-8').read()


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


def edit(text, ticket, old, new):
    marker = f'{ticket}'
    lines = text.splitlines()
    out = []
    replaced = 0
    for line in lines:
        cells = line.split(',')
        if len(cells) > 6 and cells[1] == marker and cells[2] == 'gross' and cells[3] == old:
            cells[3] = new
            line = ','.join(cells)
            replaced += 1
        out.append(line)
    assert replaced == 1, f'{ticket}/{old} 未找到唯一毛重行'
    return '\n'.join(out) + '\n'


def open_batch(client, text=BASE, batch='B1', label='首版'):
    response = client.post('/api/runs', json={'csv_text': text, 'batch_id': batch, 'label': label})
    assert response.status_code == 200, response.text
    return response.json()


def add_package(client, text, batch='B1', label='收市补发'):
    response = client.post(f'/api/batches/{batch}/packages', json={'csv_text': text, 'label': label})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, package, batch='B1'):
    response = client.post(f'/api/batches/{batch}/packages/{package["package_id"]}/confirm',
                           json={'digest': package['digest']})
    assert response.status_code == 200, response.text
    return response.json()


def test_original_package_kept_and_adopted(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    base = open_batch(c)
    detail = c.get('/api/batches/B1').json()
    assert detail['adopted_run_id'] == base['manifest']['run_id']
    assert len(detail['packages']) == 1
    assert c.get('/api/runs/' + base['manifest']['run_id']).json()['input_snapshot']


def test_same_digest_resend_adds_no_settlement(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    open_batch(c)
    first = add_package(c, BASE)
    second = add_package(c, BASE)
    assert first['package_id'] == second['package_id']
    assert second['created'] is False
    assert len(c.get('/api/batches/B1').json()['packages']) == 1
    assert len(c.get('/api/runs').json()) == 1


def test_unconfirmed_package_blocks_seal_and_switch(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    open_batch(c)
    package = add_package(c, edit(BASE, '00002', '102', '112'))
    detail = c.get('/api/batches/B1').json()
    seal = c.post('/api/batches/B1/seal', json={'revision': detail['revision']})
    assert seal.status_code == 409 and seal.json()['code'] == 'PACKAGE_UNCONFIRMED'
    adopt = c.post(f'/api/batches/B1/packages/{package["package_id"]}/adopt', json={'revision': detail['revision']})
    assert adopt.status_code == 409 and adopt.json()['code'] == 'PACKAGE_UNCONFIRMED'
    assert c.get('/api/batches/B1').json()['adopted_run_id'] == detail['adopted_run_id']


def test_confirm_digest_mismatch_changes_nothing(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    open_batch(c)
    package = add_package(c, edit(BASE, '00002', '102', '112'))
    bad = c.post(f'/api/batches/B1/packages/{package["package_id"]}/confirm', json={'digest': '0' * 64})
    assert bad.status_code == 409 and bad.json()['code'] == 'CONFIRM_MISMATCH'
    assert c.get('/api/batches/B1').json()['packages'][1]['confirmed'] == 0


def test_stale_switch_keeps_old_version(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    base = open_batch(c)
    package = add_package(c, edit(BASE, '00002', '102', '112'))
    confirm(c, package)
    stale = c.post(f'/api/batches/B1/packages/{package["package_id"]}/adopt', json={'revision': 999})
    assert stale.status_code == 409 and stale.json()['code'] == 'STALE_VERSION'
    detail = c.get('/api/batches/B1').json()
    assert detail['adopted_run_id'] == base['manifest']['run_id']
    assert detail['revision'] == 1


def test_unmatched_ticket_blocks_switch(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    base = open_batch(c)
    dropped = '\n'.join(line for line in BASE.splitlines() if not line.startswith('00020'))
    package = add_package(c, dropped + '\n')
    confirm(c, package)
    detail = c.get('/api/batches/B1').json()
    response = c.post(f'/api/batches/B1/packages/{package["package_id"]}/adopt', json={'revision': detail['revision']})
    assert response.status_code == 409
    assert response.json()['code'] == 'PACKAGE_NOT_READY'
    held = response.json()['data']['comparison']['held']
    assert held[0]['ticket'] == '00020' and held[0]['code'] == 'TICKET_UNMATCHED'
    assert c.get('/api/batches/B1').json()['adopted_run_id'] == base['manifest']['run_id']


def test_added_ticket_is_held(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    open_batch(c)
    extra = BASE + '00021gross,00021,gross,101,kg,黄鱼,12.30,0.05,1,,0\n00021tare,00021,tare,10,kg,黄鱼,12.30,0.05,1,,0\n'
    package = add_package(c, extra)
    confirm(c, package)
    detail = c.get('/api/batches/B1').json()
    comparison = detail['comparisons'][0]['comparison']
    assert comparison['added'] == ['00021']
    assert [h['code'] for h in comparison['held']] == ['TICKET_UNMATCHED']
    response = c.post(f'/api/batches/B1/seal', json={'revision': detail['revision']})
    assert response.status_code == 409 and response.json()['code'] == 'TICKET_UNMATCHED'


def test_lost_settlement_is_held(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    open_batch(c)
    broken = BASE.replace('00002tare,00002,tare,10,kg,黄鱼,12.30,0.05,1,,0',
                          '00002tare,00002,tare,NULL,kg,黄鱼,12.30,0.05,1,,0')
    package = add_package(c, broken)
    confirm(c, package)
    detail = c.get('/api/batches/B1').json()
    held = detail['comparisons'][0]['comparison']['held']
    assert held[0]['ticket'] == '00002' and held[0]['code'] == 'TICKET_LOST'


def test_repaired_quarantined_ticket_is_legal_change(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    broken = BASE.replace('00002tare,00002,tare,10,kg,黄鱼,12.30,0.05,1,,0',
                          '00002tare,00002,tare,NULL,kg,黄鱼,12.30,0.05,1,,0')
    open_batch(c, broken)
    package = add_package(c, BASE, label='修复补发')
    confirm(c, package)
    detail = c.get('/api/batches/B1').json()
    comparison = detail['comparisons'][0]['comparison']
    assert comparison['held'] == []
    assert [row['ticket'] for row in comparison['changed']] == ['00002']
    assert comparison['changed'][0]['repaired'] is True
    response = c.post(f'/api/batches/B1/packages/{package["package_id"]}/adopt',
                      json={'revision': detail['revision']})
    assert response.status_code == 200, response.text


def test_switch_then_seal_single_result(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    base = open_batch(c)
    package = add_package(c, edit(BASE, '00002', '102', '112'))
    confirm(c, package)
    detail = c.get('/api/batches/B1').json()
    changed = detail['comparisons'][0]['comparison']['changed']
    assert [row['ticket'] for row in changed] == ['00002']
    adopted = c.post(f'/api/batches/B1/packages/{package["package_id"]}/adopt', json={'revision': detail['revision']})
    assert adopted.status_code == 200, adopted.text
    detail = adopted.json()
    assert detail['adopted_run_id'] == package['run_id']
    assert detail['packages'][0]['id'] == store.package_id('B1', base['manifest']['sha256'])
    sealed = c.post('/api/batches/B1/seal', json={'revision': detail['revision']})
    assert sealed.status_code == 200, sealed.text
    body = sealed.json()
    assert body['sealed_run_id'] == package['run_id']
    again = c.post('/api/batches/B1/seal', json={'revision': body['revision']})
    assert again.status_code == 200
    assert again.json()['sealed_run_id'] == body['sealed_run_id']
    assert again.json()['seal'] == body['seal']
    assert len(c.get('/api/runs').json()) == 2
    export = c.get('/api/export/' + package['run_id'])
    assert export.status_code == 200
    bundle = zipfile.ZipFile(io.BytesIO(export.content))
    assert json.loads(bundle.read('manifest.json'))['run_id'] == package['run_id']
    evidence = json.loads(bundle.read('batch.json'))
    assert evidence['sealed_run_id'] == package['run_id']
    assert [p['run_id'] for p in evidence['packages']] == [base['manifest']['run_id'], package['run_id']]
    assert evidence['packages'][0]['digest'] == base['manifest']['sha256']
    assert evidence['comparisons'][0]['comparison']['changed'][0]['ticket'] == '00002'
    # 非封存版不能经下载入口取走，避免产生第二份可付账。
    assert c.get('/api/export/' + base['manifest']['run_id']).status_code == 409


def test_switch_after_seal_refused(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    open_batch(c)
    first = add_package(c, edit(BASE, '00002', '102', '112'))
    confirm(c, first)
    detail = c.get('/api/batches/B1').json()
    detail = c.post(f'/api/batches/B1/packages/{first["package_id"]}/adopt', json={'revision': detail['revision']}).json()
    detail = c.post('/api/batches/B1/seal', json={'revision': detail['revision']}).json()
    assert detail['sealed'] is True
    late = c.post('/api/batches/B1/packages', json={'csv_text': edit(BASE, '00003', '103', '113'),
                                                   'label': '封账后补发'})
    assert late.status_code == 409 and late.json()['code'] == 'BATCH_SEALED'
    stale = c.post(f'/api/batches/B1/packages/{first["package_id"]}/adopt',
                   json={'revision': detail['revision']})
    assert stale.status_code == 409
    assert c.get('/api/batches/B1').json()['sealed_run_id'] == detail['sealed_run_id']


def test_reject_keeps_package_evidence(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    open_batch(c)
    bad = add_package(c, BASE + '00021gross,00021,gross,101,kg,黄鱼,12.30,0.05,1,,0\n')
    rejected = c.post(f'/api/batches/B1/packages/{bad["package_id"]}/reject', json={'reason': '上游确认误发'})
    assert rejected.status_code == 200
    detail = c.get('/api/batches/B1').json()
    assert detail['packages'][1]['rejected'] == 1
    assert detail['packages'][1]['reject_reason'] == '上游确认误发'
    seal = c.post('/api/batches/B1/seal', json={'revision': detail['revision']})
    assert seal.status_code == 200, seal.text
    assert seal.json()['status'] == '已封存'


def test_batch_revision_guard(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    open_batch(c)
    assert c.post('/api/batches/B1/revision', json={'revision': 1}).status_code == 200
    assert c.post('/api/batches/B1/revision', json={'revision': 1}).status_code == 409
    assert c.post('/api/batches/missing/revision', json={'revision': 1}).status_code == 404


def test_export_before_seal_refused(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    run = open_batch(c)
    assert c.get('/api/export/' + run['manifest']['run_id']).status_code == 409
    assert c.get('/api/export/missing').status_code == 404


def test_original_evidence_untouched_after_switch_and_seal(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    base = open_batch(c)
    before = c.get('/api/runs/' + base['manifest']['run_id']).json()
    package = add_package(c, edit(BASE, '00002', '102', '112'))
    confirm(c, package)
    detail = c.get('/api/batches/B1').json()
    sign = c.post(f'/api/batches/B1/packages/{package["package_id"]}/adopt',
                  json={'revision': detail['revision']}).json()
    c.post('/api/batches/B1/seal', json={'revision': sign['revision']})
    after = c.get('/api/runs/' + base['manifest']['run_id']).json()
    for key in ('counts', 'records', 'pairs', 'settlements', 'final', 'balance', 'quarantine',
                'input_snapshot', 'manifest'):
        assert before[key] == after[key]
    assert before['input_snapshot']['bytes_b64'] == after['input_snapshot']['bytes_b64']


def test_runs_endpoint_with_batch_registers_pending_package(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch)
    base = open_batch(c)
    body = c.post('/api/runs', json={'csv_text': edit(BASE, '00002', '102', '112'),
                                     'batch_id': 'B1', 'label': '收市补发'}).json()
    assert body['package']['created'] is True
    assert body['package']['adopted'] is False
    assert body['package']['confirmed'] == 0
    assert body['manifest']['run_id'] == body['package']['run_id']
    detail = c.get('/api/batches/B1').json()
    assert detail['adopted_run_id'] == base['manifest']['run_id']
    assert len(detail['packages']) == 2
    assert len(c.get('/api/runs').json()) == 2
