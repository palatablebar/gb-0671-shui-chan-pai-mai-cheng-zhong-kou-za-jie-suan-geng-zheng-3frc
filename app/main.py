from contextlib import asynccontextmanager
from importlib import import_module
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate
from app import store
from app.archive import serialize

def default_batch_for(dataset):
    """样例切换各自成批，避免把演示数据混进同一日结。"""
    return META['batch'] if dataset=='dirty' else f"{META['batch']}-{dataset}"

def _error(error:BusinessError, status:int=None):
    body={'code':error.code,'detail':error.message}
    if error.data is not None: body['data']=error.data
    return JSONResponse(body,status_code=status or error.status)

@asynccontextmanager
async def lifespan(app):
    boot=store.save(DB_PATH,evaluate())
    store.ensure_batch(DB_PATH,META['batch'],boot['manifest']['run_id'],boot['manifest']['sha256'],
                       label='原始包',source=boot.get('source'))
    yield
app=FastAPI(title=META['title'],lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'app/static')),name='static')
@app.exception_handler(BusinessError)
async def business_error(request:Request,error:BusinessError):
    return _error(error)
@app.get('/')
def home():
    return RedirectResponse('/'+MODULES[0],status_code=303)
@app.get('/healthz')
def health():
    return {'status':'ok','project':META['short']}

@app.post('/api/runs')
def create(payload:dict=Body(default={})):
    result=store.save(DB_PATH,evaluate(payload))
    if 'batch_id' not in payload:
        return result
    package=_register_package(payload,result)
    body=dict(result)
    body['package']={key:package[key] for key in ('package_id','created','adopted','confirmed','digest','run_id')}
    body['batch']={'id':package['batch'],'revision':package['revision'],
                   'adopted_run_id':store.batch_detail(DB_PATH,package['batch'])['adopted_run_id']}
    return body

def _register_package(payload,result):
    """新批次直接采用首包；已有批次则登记为待确认补发包。绝不产生第二个批次。"""
    batch_id=payload.get('batch_id')
    if not isinstance(batch_id,str) or not batch_id.strip():
        raise BusinessError('BAD_BATCH','批次号必须为非空字符串')
    batch_id=batch_id.strip()
    manifest=result['manifest']
    label=payload.get('label') or ''
    if not isinstance(label,str):
        raise BusinessError('BAD_LABEL','源包标签必须为文本')
    label=label.strip()
    if not store.batch_exists(DB_PATH,batch_id):
        detail=store.ensure_batch(DB_PATH,batch_id,manifest['run_id'],manifest['sha256'],
                                  label=label or '原始包',source=result.get('source'))
        return {'batch':detail['id'],'package_id':detail['packages'][0]['id'],'created':True,
                'adopted':True,'digest':manifest['sha256'],'run_id':manifest['run_id'],
                'confirmed':1,'revision':detail['revision']}
    if not label:
        raise BusinessError('BAD_LABEL','补发包需要填写来源标签')
    detail,pid,created=store.register_package(DB_PATH,batch_id,manifest['run_id'],manifest['sha256'],
                                              label,source=result.get('source'))
    return {'batch':detail['id'],'package_id':pid,'created':created,'adopted':False,
            'digest':manifest['sha256'],'run_id':manifest['run_id'],
            'confirmed':next(p['confirmed'] for p in detail['packages'] if p['id']==pid),
            'revision':detail['revision']}

@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id:str):
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    return store.save(DB_PATH,evaluate({'dataset':previous['source']},snapshot=previous['input_snapshot']))
@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)
@app.get('/api/runs/{run_id}')
def run(run_id:str):
    return store.get(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/review')
def review(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.review(DB_PATH,run_id,revision)

# ------------------------------------------------------------ 批次日结

@app.get('/api/batches')
def batches():
    return store.batch_list(DB_PATH)

@app.get('/api/batches/{batch_id}')
def batch(batch_id:str):
    detail=store.batch_detail(DB_PATH,batch_id)
    detail['comparisons']=store.batch_comparisons(DB_PATH,batch_id)
    return detail

@app.post('/api/batches/{batch_id}/packages')
def register(batch_id:str,payload:dict=Body(default={})):
    """导入补发包：先按不可变输入算出独立运行，再登记为候选源包。"""
    result=store.save(DB_PATH,evaluate(payload))
    label=payload.get('label') or '补发包'
    if not isinstance(label,str) or not label.strip():
        raise BusinessError('BAD_LABEL','源包标签必须为非空字符串')
    detail,pid,created=store.register_package(DB_PATH,batch_id,result['manifest']['run_id'],
                                              result['manifest']['sha256'],label.strip(),
                                              source=result.get('source'))
    return {'batch':detail['id'],'package_id':pid,'created':created,
            'digest':result['manifest']['sha256'],'run_id':result['manifest']['run_id'],
            'confirmed':next(p['confirmed'] for p in detail['packages'] if p['id']==pid)}

@app.post('/api/batches/{batch_id}/import')
def import_into_batch(batch_id:str,payload:dict=Body(default={})):
    """与 POST /api/runs 携带 batch_id 等价，便于在批次页直接导入。"""
    if not isinstance(batch_id,str) or not batch_id.strip():
        raise BusinessError('BAD_BATCH','批次号必须为非空字符串')
    result=store.save(DB_PATH,evaluate(payload))
    package=_register_package({**payload,'batch_id':batch_id},result)
    package.pop('run',None)
    return package

@app.post('/api/batches/{batch_id}/packages/{pid}/confirm')
def confirm(batch_id:str,pid:str,payload:dict=Body(default={})):
    digest=payload.get('digest')
    if not isinstance(digest,str) or len(digest)!=64:
        raise BusinessError('BAD_DIGEST','确认摘要须为64位十六进制')
    return store.confirm_package(DB_PATH,batch_id,pid,digest)

@app.post('/api/batches/{batch_id}/packages/{pid}/adopt')
def adopt(batch_id:str,pid:str,payload:dict=Body(default={})):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.adopt_package(DB_PATH,batch_id,pid,revision)

@app.post('/api/batches/{batch_id}/packages/{pid}/reject')
def reject(batch_id:str,pid:str,payload:dict=Body(default={})):
    reason=payload.get('reason') or '结算员退回'
    if not isinstance(reason,str): raise BusinessError('BAD_REASON','退回原因须为文本')
    return store.reject_package(DB_PATH,batch_id,pid,reason.strip())

@app.post('/api/batches/{batch_id}/seal')
def seal(batch_id:str,payload:dict=Body(default={})):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.seal_batch(DB_PATH,batch_id,revision)

@app.post('/api/batches/{batch_id}/revision')
def batch_revision(batch_id:str,payload:dict=Body(...)):
    """延续批次版本号：复核批次视图后旧版本提交返回409。"""
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    with store.connect(DB_PATH) as con:
        cur=con.execute('UPDATE batches SET revision=revision+1 WHERE id=? AND revision=?',
                        (batch_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM batches WHERE id=?',(batch_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','批次版本已更新，请刷新',409)
    return store.batch_detail(DB_PATH,batch_id)

@app.get('/api/export/{run_id}')
def export(run_id:str):
    """唯一封存下载入口：属于批次的运行只允许下载已封存的那一版。"""
    result=store.get(DB_PATH,run_id)
    batch=store.batch_for_run(DB_PATH,run_id)
    if batch is not None:
        if not batch['sealed']:
            raise BusinessError('NOT_SEALED','批次尚未封存，无可下载日结',409)
        if batch['sealed_run_id']!=run_id:
            raise BusinessError('NOT_SEALED_RUN','该运行不是批次封存版，请从日结页面下载',409)
    comparisons=store.batch_comparisons(DB_PATH,batch['id']) if batch else None
    bundle=serialize(result,batch=batch,comparisons=comparisons)
    return Response(bundle,media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
