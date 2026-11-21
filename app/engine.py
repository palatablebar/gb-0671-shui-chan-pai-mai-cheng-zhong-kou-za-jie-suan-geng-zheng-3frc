from importlib import import_module
from copy import deepcopy
import json, time, base64, hashlib
from app.config import ROOT, MODULES, MAX_BYTES, RULE_VERSION
from app.errors import BusinessError
from app.manifest import digest, make_manifest

def _context(payload, snapshot):
    if snapshot is not None:
        context=deepcopy(snapshot['context'])
    elif 'context' in payload:
        context=deepcopy(payload['context'])
    else:
        context=json.loads((ROOT/'sample_data/context.json').read_text())
    context.update(deepcopy(payload.get('overrides',{})))
    return context

def _raw(payload, snapshot, dataset):
    if snapshot is not None:
        return base64.b64decode(snapshot['bytes_b64'],validate=True)
    if 'csv_text' in payload:
        return str(payload['csv_text']).encode()
    return (ROOT/'sample_data'/f'{dataset}.csv').read_bytes()

def evaluate(payload=None, until=7, snapshot=None):
    payload=payload or {}
    dataset=payload.get('dataset','dirty')
    if dataset not in {'dirty','clean','legacy'}: raise BusinessError('BAD_DATASET','样例名称无效')
    if not isinstance(payload.get('overrides',{}),dict): raise BusinessError('BAD_CONTEXT','参数须为对象')
    if 'context' in payload and not isinstance(payload['context'],dict): raise BusinessError('BAD_CONTEXT','参数须为对象')
    raw=_raw(payload,snapshot,dataset)
    if len(raw)>MAX_BYTES: raise BusinessError('TOO_LARGE','文件超过2MB',413)
    context=_context(payload,snapshot)
    start=time.perf_counter()
    state={'raw_bytes':raw,'context':context,'records':[],'quarantine':[], 'counts':{},'views':{},'source':dataset}
    for index,key in enumerate(MODULES,1):
        if index>until: break
        import_module(f'app.modules.{key}.rules').apply(state)
    state['input_snapshot']={'bytes_b64':base64.b64encode(raw).decode(),'context':deepcopy(context)}
    state.pop('raw_bytes',None)
    # 运行标识只由“原始字节 + 上下文 + 规则版本”决定；重跑同输入不新增结算。
    state['manifest']=make_manifest(digest(raw,context),dataset,time.perf_counter()-start)
    return state
