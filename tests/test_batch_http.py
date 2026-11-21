from tests.test_batch import BASE, add_package, confirm, edit, make_client, open_batch


def test_pages_show_batch_panel(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    c = TestClient(main.app)
    with c:
        run = c.post('/api/runs', json={'csv_text': BASE, 'batch_id': 'B1', 'label': '首版'}).json()
        package = add_package(c, edit(BASE, '00002', '102', '112'))
        html = c.get('/archive?run_id=' + run['manifest']['run_id']).text
        assert '日结批次 B1' in html
        assert '收市补发' in html
        assert '确认内容' in html
        assert '采用此版' not in html  # 未确认前不能切换
        confirm(c, package)
        html = c.get('/archive?run_id=' + run['manifest']['run_id']).text
        assert '采用此版' in html
        assert '00002' in html
        for key in main.MODULES:
            assert c.get('/' + key + '?run_id=' + run['manifest']['run_id']).status_code == 200


def test_batch_import_endpoint_creates_single_batch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    c = TestClient(main.app)
    with c:
        first = c.post('/api/batches/B2/import', json={'csv_text': BASE, 'label': '原始包'}).json()
        assert first['adopted'] is True and first['confirmed'] == 1
        second = c.post('/api/batches/B2/import', json={'csv_text': edit(BASE, '00002', '102', '112'),
                                                       'label': '收市补发'}).json()
        assert second['adopted'] is False and second['created'] is True
        assert second['confirmed'] == 0
        again = c.post('/api/batches/B2/import', json={'csv_text': edit(BASE, '00002', '102', '112'),
                                                      'label': '收市补发'}).json()
        assert again['package_id'] == second['package_id'] and again['created'] is False
        assert len(c.get('/api/batches/B2').json()['packages']) == 2
        assert c.post('/api/batches/B2/import', json={'csv_text': BASE}).status_code == 422
        assert c.get('/api/batches').status_code == 200
        assert [b['id'] for b in c.get('/api/batches').json()][0] == 'B2'


def test_second_supplement_after_adopt_is_rechecked(tmp_path, monkeypatch):
    """采用第一份补发后，第二份补发仍要与新采用版逐票对应才能封存。"""
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    c = TestClient(main.app)
    with c:
        open_batch(c)
        first = add_package(c, edit(BASE, '00002', '102', '112'))
        confirm(c, first)
        detail = c.get('/api/batches/B1').json()
        detail = c.post(f'/api/batches/B1/packages/{first["package_id"]}/adopt',
                        json={'revision': detail['revision']}).json()
        second = add_package(c, edit(BASE, '00003', '103', '113'))
        confirm(c, second)
        detail = c.get('/api/batches/B1').json()
        comparison = detail['comparisons'][-1]['comparison']
        assert comparison['held'] == []
        # 第二份补发以原始包为基础：相对已采用的 00002=112 会回退，同时带 00003 的变化。
        assert [row['ticket'] for row in comparison['changed']] == ['00002', '00003']
        changed = {row['ticket']: (row['amount_before'], row['amount_after']) for row in comparison['changed']}
        assert changed['00002'][0] != changed['00002'][1]
        # 第二份补发已确认但尚未裁决：先不封存，避免把已核对的旧金额当成最新日结。
        seal = c.post('/api/batches/B1/seal', json={'revision': detail['revision']})
        assert seal.status_code == 409 and seal.json()['code'] == 'PACKAGE_PENDING'
        assert seal.json()['data']['blockers'][0]['changed'] == ['00002', '00003']
        # 采用第二份补发后，封存落在最新一版。
        adopted = c.post(f'/api/batches/B1/packages/{second["package_id"]}/adopt',
                         json={'revision': detail['revision']}).json()
        seal = c.post('/api/batches/B1/seal', json={'revision': adopted['revision']})
        assert seal.status_code == 200, seal.text
        assert seal.json()['sealed_run_id'] == second['run_id']
        # 已采用版本的原始证据仍在。
        assert c.get('/api/runs/' + second['run_id']).json()['counts']['input'] == 42
        assert c.get('/api/export/' + second['run_id']).status_code == 200
        assert c.get('/api/export/' + first['run_id']).status_code == 409


def test_second_supplement_with_lost_ticket_blocks_seal(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    c = TestClient(main.app)
    with c:
        open_batch(c)
        first = add_package(c, edit(BASE, '00002', '102', '112'))
        confirm(c, first)
        detail = c.get('/api/batches/B1').json()
        detail = c.post(f'/api/batches/B1/packages/{first["package_id"]}/adopt',
                        json={'revision': detail['revision']}).json()
        broken = '\n'.join(line for line in BASE.splitlines() if not line.startswith('00020'))
        second = add_package(c, broken + '\n')
        confirm(c, second)
        detail = c.get('/api/batches/B1').json()
        seal = c.post('/api/batches/B1/seal', json={'revision': detail['revision']})
        assert seal.status_code == 409 and seal.json()['code'] == 'TICKET_UNMATCHED'
        assert seal.json()['data']['blockers'][0]['held'][0]['ticket'] == '00020'
        assert c.get('/api/batches/B1').json()['sealed'] is False

