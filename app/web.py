from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def render(request:Request,key):
    from app.main import DB_PATH, default_batch_for
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
        if not store.batch_for_run(DB_PATH,state['manifest']['run_id']):
            store.ensure_batch(DB_PATH,default_batch_for(state['source']),state['manifest']['run_id'],
                               state['manifest']['sha256'],label='原始包',source=state.get('source'))
    batch=store.batch_for_run(DB_PATH,state['manifest']['run_id'])
    comparisons=store.batch_comparisons(DB_PATH,batch['id']) if batch else []
    return templates.TemplateResponse(request=request,name=f'{key}.html',
        context={'meta':META,'key':key,'state':state,'view':state['views'][key],
                 'batch':batch,'comparisons':comparisons})
