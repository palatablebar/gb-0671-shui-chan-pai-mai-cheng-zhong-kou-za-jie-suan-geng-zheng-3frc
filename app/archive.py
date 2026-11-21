import io, json, zipfile
from app.errors import BusinessError

def serialize(result, batch=None, comparisons=None):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        if batch is not None:
            package_evidence={
                'batch':batch['id'],'status':batch['status'],'sealed_run_id':batch['sealed_run_id'],
                'seal':batch['seal'],
                'packages':[{key:p[key] for key in ('id','label','run_id','digest','confirmed','rejected','reject_reason','adopted') if key in p}
                            for p in batch['packages']],
                'events':batch.get('events',[]),
                'comparisons':comparisons or [],
            }
            archive.writestr('batch.json',json.dumps(package_evidence,ensure_ascii=False,indent=2))
    return buffer.getvalue()
