import sqlite3, json, hashlib
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核');
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  adopted_run_id TEXT,
  sealed_run_id TEXT,
  seal TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT '采集中',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS packages (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  digest TEXT NOT NULL,
  label TEXT NOT NULL,
  source TEXT,
  confirmed INTEGER NOT NULL DEFAULT 0,
  confirmed_sha TEXT,
  rejected INTEGER NOT NULL DEFAULT 0,
  reject_reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(batch_id, digest));
CREATE TABLE IF NOT EXISTS batch_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  action TEXT NOT NULL,
  at TEXT NOT NULL,
  payload TEXT NOT NULL);
"""

def _now():
    return datetime.now(timezone.utc).isoformat()

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

# ---------------------------------------------------------------- batches

def package_id(batch_id, digest):
    return 'pkg-'+hashlib.sha256(f'{batch_id}:{digest}'.encode()).hexdigest()[:12]

def _event(con, batch_id, action, payload):
    con.execute('INSERT INTO batch_events(batch_id,action,at,payload) VALUES(?,?,?,?)',
                (batch_id,action,_now(),json.dumps(payload,ensure_ascii=False,sort_keys=True)))

def _batch_row(con, batch_id):
    return con.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()

def _package_row(con, batch_id, pid):
    return con.execute('SELECT * FROM packages WHERE batch_id=? AND id=?',(batch_id,pid)).fetchone()

def ensure_batch(path, batch_id, run_id, digest, label='原始包', source=None):
    """登记批次并把首个源包设为采用版；已存在则不改动。"""
    with connect(path) as con:
        if _batch_row(con, batch_id) is None:
            now=_now()
            con.execute('INSERT INTO batches(id,adopted_run_id,revision,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                        (batch_id,run_id,1,'采集中',now,now))
            con.execute('INSERT OR IGNORE INTO packages(id,batch_id,run_id,digest,label,source,confirmed,confirmed_sha,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        (package_id(batch_id,digest),batch_id,run_id,digest,label,source,1,digest,_now()))
            _event(con,batch_id,'batch.created',{'run_id':run_id,'digest':digest,'label':label})
    return batch_detail(path,batch_id)

def register_package(path, batch_id, run_id, digest, label, source=None):
    """登记补发源包。同内容重试返回同一包，不新增结算。"""
    with connect(path) as con:
        batch=_batch_row(con,batch_id)
        if batch is None:
            raise BusinessError('NOT_FOUND','批次不存在',404)
        pid=package_id(batch_id,digest)
        if _package_row(con,batch_id,pid) is not None:
            return batch_detail(path,batch_id),pid,False
        if batch['sealed_run_id'] is not None:
            raise BusinessError('BATCH_SEALED','批次已封存，不能再登记补发包',409)
        con.execute('INSERT INTO packages(id,batch_id,run_id,digest,label,source,confirmed,confirmed_sha,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (pid,batch_id,run_id,digest,label,source,0,None,_now()))
        _event(con,batch_id,'package.registered',{'package_id':pid,'run_id':run_id,'digest':digest,'label':label})
    return batch_detail(path,batch_id),pid,True

def confirm_package(path, batch_id, pid, digest):
    """确认补发包内容。摘要不符则拒绝，批次不发生变化。"""
    with connect(path) as con:
        row=_package_row(con,batch_id,pid)
        if row is None: raise BusinessError('PACKAGE_NOT_FOUND','源包不存在',404)
        batch=_batch_row(con,batch_id)
        if batch['sealed_run_id'] is not None:
            raise BusinessError('BATCH_SEALED','批次已封存，不能再改候选包',409)
        if row['rejected']:
            raise BusinessError('PACKAGE_REJECTED','补发包已退回，不能确认',409)
        if row['digest']!=digest:
            raise BusinessError('CONFIRM_MISMATCH','确认摘要与源包内容不一致',409)
        if not row['confirmed']:
            con.execute('UPDATE packages SET confirmed=1,confirmed_sha=? WHERE batch_id=? AND id=?',(digest,batch_id,pid))
            _event(con,batch_id,'package.confirmed',{'package_id':pid,'digest':digest})
    return batch_detail(path,batch_id)

def reject_package(path, batch_id, pid, reason):
    """保留证据但不再把该候选包纳入封存门槛。"""
    with connect(path) as con:
        row=_package_row(con,batch_id,pid)
        if row is None: raise BusinessError('PACKAGE_NOT_FOUND','源包不存在',404)
        batch=_batch_row(con,batch_id)
        if batch['sealed_run_id'] is not None:
            raise BusinessError('BATCH_SEALED','批次已封存，不能再改候选包',409)
        if row['run_id']==batch['adopted_run_id']:
            raise BusinessError('PACKAGE_ADOPTED','采用版不能撤销',409)
        con.execute('UPDATE packages SET rejected=1,reject_reason=? WHERE batch_id=? AND id=?',(reason,batch_id,pid))
        _event(con,batch_id,'package.rejected',{'package_id':pid,'reason':reason})
    return batch_detail(path,batch_id)

def _swap_adopted(con, path, batch, package, revision):
    from app.close import compare
    if batch['sealed_run_id'] is not None:
        raise BusinessError('BATCH_SEALED','批次已封存，不能切换源包',409)
    if package['run_id']==batch['adopted_run_id']:
        return {'changed':[],'held':[],'ready':True,'reasons':[],'unchanged':True}
    if batch['revision']!=revision:
        raise BusinessError('STALE_VERSION','批次版本已更新，请刷新',409)
    if not package['confirmed']:
        raise BusinessError('PACKAGE_UNCONFIRMED','补发包内容未确认，不能切换',409)
    if package['rejected']:
        raise BusinessError('PACKAGE_REJECTED','补发包已被退回，不能切换',409)
    base=get(path,batch['adopted_run_id'])
    candidate=get(path,package['run_id'])
    result=compare(base,candidate)
    if not result['ready']:
        raise BusinessError('PACKAGE_NOT_READY','补发包暂不可封存：'+'；'.join(result['reasons']),409,
                            data={'comparison':result})
    previous=batch['adopted_run_id']
    cur=con.execute('UPDATE batches SET adopted_run_id=?,revision=revision+1,status=?,updated_at=? WHERE id=? AND revision=? AND adopted_run_id=? AND sealed_run_id IS NULL',
                    (package['run_id'],'采集中',_now(),batch['id'],revision,previous))
    if cur.rowcount!=1:
        raise BusinessError('STALE_VERSION','批次版本已更新，请刷新',409)
    _event(con,batch['id'],'package.adopted',{'package_id':package['id'],'from_run_id':previous,
                                             'to_run_id':package['run_id'],'changed':len(result['changed']),
                                             'held':len(result['held'])})
    return result

def adopt_package(path, batch_id, pid, revision):
    """原子切换采用版。失败时保持旧采用版与已封存结果不变。"""
    con=connect(path)
    try:
        con.execute('BEGIN IMMEDIATE')
        batch=_batch_row(con,batch_id)
        if batch is None: raise BusinessError('NOT_FOUND','批次不存在',404)
        package=_package_row(con,batch_id,pid)
        if package is None: raise BusinessError('PACKAGE_NOT_FOUND','源包不存在',404)
        _swap_adopted(con,path,batch,package,revision)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return batch_detail(path,batch_id)

def _seal_blockers(path, con, batch):
    """封存前的暂缓条件。

    - 任何未确认且未退回的补发包：内容未确认，暂缓。
    - 晚于采用版登记的、已确认补发包：与采用版逐票比对，
      有无法对应的票或自身不可归档时暂缓。
    早于采用版的源包已被切换证据覆盖，不再阻断。
    """
    from app.close import compare
    adopted=get(path,batch['adopted_run_id'])
    blockers=[]
    rows=con.execute("SELECT rowid AS _seq,* FROM packages WHERE batch_id=? ORDER BY rowid",
                     (batch['id'],)).fetchall()
    adopted_seq=next((r['_seq'] for r in rows if r['run_id']==batch['adopted_run_id']),None)
    for row in rows:
        if row['run_id']==batch['adopted_run_id'] or row['rejected']:
            continue
        if not row['confirmed']:
            blockers.append({'package_id':row['id'],'code':'PACKAGE_UNCONFIRMED',
                             'detail':'补发包内容未确认'})
            continue
        if adopted_seq is not None and row['_seq']<adopted_seq:
            continue
        result=compare(adopted,get(path,row['run_id']))
        if result['held']:
            blockers.append({'package_id':row['id'],'code':'TICKET_UNMATCHED',
                             'detail':'同一票在版本间无法对应','held':result['held']})
        elif not result['ready']:
            blockers.append({'package_id':row['id'],'code':'PACKAGE_NOT_READY',
                             'detail':'补发包暂不可封存：'+'；'.join(result['reasons']),
                             'reasons':result['reasons']})
        elif result['changed']:
            blockers.append({'package_id':row['id'],'code':'PACKAGE_PENDING',
                             'detail':'已确认补发包与采用版存在票值差异，需先采用或退回',
                             'changed':[c['ticket'] for c in result['changed']]})
    return blockers

def seal_batch(path, batch_id, revision):
    """封存当前采用版。同采用版重试返回同一封存结果，不新增结算。"""
    con=connect(path)
    try:
        con.execute('BEGIN IMMEDIATE')
        batch=_batch_row(con,batch_id)
        if batch is None: raise BusinessError('NOT_FOUND','批次不存在',404)
        if batch['sealed_run_id'] is not None:
            if batch['sealed_run_id']==batch['adopted_run_id']:
                con.commit()
                return batch_detail(path,batch_id)
            raise BusinessError('BATCH_SEALED','批次已按另一版封存',409)
        if batch['revision']!=revision:
            raise BusinessError('STALE_VERSION','批次版本已更新，请刷新',409)
        blockers=_seal_blockers(path,con,batch)
        if blockers:
            codes={b['code'] for b in blockers}
            if 'PACKAGE_UNCONFIRMED' in codes:
                code,message='PACKAGE_UNCONFIRMED','补发包内容未确认，暂缓封存'
            elif 'PACKAGE_PENDING' in codes:
                code,message='PACKAGE_PENDING','补发包已确认但尚未裁决，暂缓封存'
            else:
                code,message='TICKET_UNMATCHED','同一票在版本间无法对应，暂缓封存'
            con.rollback()
            raise BusinessError(code,message,409,data={'blockers':blockers})
        adopted=get(path,batch['adopted_run_id'])
        archive=adopted.get('archive',{})
        if archive.get('status')!='可归档':
            con.rollback()
            raise BusinessError('NOT_ARCHIVABLE','采用版尚未通过归档校验，不能封存',409)
        seal=archive['seal']
        cur=con.execute('UPDATE batches SET sealed_run_id=?,seal=?,status=?,revision=revision+1,updated_at=? WHERE id=? AND revision=? AND sealed_run_id IS NULL',
                        (batch['adopted_run_id'],seal,'已封存',_now(),batch_id,revision))
        if cur.rowcount!=1:
            raise BusinessError('STALE_VERSION','批次版本已更新，请刷新',409)
        _event(con,batch_id,'batch.sealed',{'run_id':batch['adopted_run_id'],'seal':seal})
        con.commit()
    except BusinessError:
        con.rollback()
        raise
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return batch_detail(path,batch_id)

def batch_detail(path, batch_id):
    with connect(path) as con:
        batch=_batch_row(con,batch_id)
        if batch is None: raise BusinessError('NOT_FOUND','批次不存在',404)
        packages=[dict(r) for r in con.execute('SELECT id,batch_id,run_id,digest,label,source,confirmed,confirmed_sha,rejected,reject_reason,created_at FROM packages WHERE batch_id=? ORDER BY created_at,id',(batch_id,))]
        events=[dict(r) for r in con.execute('SELECT seq,action,at,payload FROM batch_events WHERE batch_id=? ORDER BY seq',(batch_id,))]
    adopted=get(path,batch['adopted_run_id'])
    for package in packages:
        package['adopted']=package['run_id']==batch['adopted_run_id']
        package['digest_matches']=(package['digest']==adopted.get('manifest',{}).get('sha256')) if package['adopted'] else None
    for event in events:
        try: event['payload']=json.loads(event['payload'])
        except ValueError: pass
    return {'id':batch_id,'adopted_run_id':batch['adopted_run_id'],'sealed_run_id':batch['sealed_run_id'],
            'seal':batch['seal'],'revision':batch['revision'],'status':batch['status'],
            'packages':packages,'events':events,'adopted':adopted,
            'sealed':batch['sealed_run_id'] is not None,
            'adopted_ready':adopted.get('archive',{}).get('status')=='可归档'}

def batch_list(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,adopted_run_id,sealed_run_id,seal,revision,status,updated_at FROM batches ORDER BY created_at DESC LIMIT 100')]

def batch_exists(path, batch_id):
    with connect(path) as con:
        return _batch_row(con,batch_id) is not None

def batch_for_run(path, run_id):
    with connect(path) as con:
        row=con.execute('SELECT batch_id FROM packages WHERE run_id=? ORDER BY created_at LIMIT 1',(run_id,)).fetchone()
        if row is None: return None
    return batch_detail(path,row['batch_id'])

def batch_comparisons(path, batch_id):
    from app.close import compare
    detail=batch_detail(path,batch_id)
    adopted=detail['adopted']
    comparisons=[]
    for package in detail['packages']:
        if package['adopted']: continue
        candidate=get(path,package['run_id'])
        comparisons.append({'package_id':package['id'],'label':package['label'],
                            'confirmed':bool(package['confirmed']),'rejected':bool(package['rejected']),
                            'comparison':compare(adopted,candidate)})
    return comparisons
