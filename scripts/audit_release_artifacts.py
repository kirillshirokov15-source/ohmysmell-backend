"""Read-only comparison of deployed application bytes with local Git archives."""
import base64
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from scripts.staging_order_desk_audit import cli, PROJECT, ENVIRONMENT


def included(name):
    return (name.startswith(('app/', 'alembic/')) or name in {'requirements.txt','alembic.ini'}) and '__pycache__' not in name and not name.endswith('.pyc')


def git_manifest(revision, crlf=False):
    data = subprocess.check_output(['git','archive',revision])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {m.name:hashlib.sha256(archive.extractfile(m).read().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n') if crlf else archive.extractfile(m).read()).hexdigest()
                for m in archive if m.isfile() and included(m.name)}


def main():
    preflight = json.loads(Path('.staging-artifacts/warehouse-release-preflight.json').read_text())
    revisions = subprocess.check_output(['git','rev-list','-40','HEAD'],text=True).splitlines()
    candidates = {r:git_manifest(r) for r in revisions}
    windows_candidates = {r:git_manifest(r,crlf=True) for r in revisions}
    code = """import json,hashlib,pathlib
p=pathlib.Path('/app'); result={}
for folder in ('app','alembic'):
 for f in (p/folder).rglob('*'):
  if f.is_file() and '__pycache__' not in f.parts and f.suffix!='.pyc':
   result[f.relative_to(p).as_posix()]=hashlib.sha256(f.read_bytes()).hexdigest()
for name in ('requirements.txt','alembic.ini'):
 f=p/name
 if f.is_file(): result[name]=hashlib.sha256(f.read_bytes()).hexdigest()
print('OMS_MANIFEST:'+json.dumps(result,sort_keys=True))
import io,tarfile,base64,importlib.metadata
b=io.BytesIO()
with tarfile.open(fileobj=b,mode='w') as tar:
 for name in sorted(result):
  tar.add(p/name,arcname=name,recursive=False)
print('OMS_BUNDLE:'+base64.b64encode(b.getvalue()).decode())
print('OMS_PACKAGES:'+json.dumps({d.metadata['Name']:d.version for d in importlib.metadata.distributions()}))
"""
    encoded = base64.b64encode(code.encode()).decode()
    def audit(service):
        result = {'service':service['name'],'deployment_id':service['deployment_id']}
        try:
            raw=cli('ssh','--project',PROJECT,'--environment',ENVIRONMENT,'--service',service['id'],'--',
                    "python -c 'import base64;exec(base64.b64decode(\""+encoded+"\"))'")
            manifest=json.loads(next(x.split(':',1)[1] for x in raw.splitlines() if x.startswith('OMS_MANIFEST:')))
            result['matching_commits']=[r for r,m in candidates.items() if m==manifest]
            result['matching_crlf_commits']=[r for r,m in windows_candidates.items() if m==manifest]
            result['files']=len(manifest)
            result['source_reproducible']=bool(result['matching_commits'] or result['matching_crlf_commits'])
            result['image_rebuild_verified']=False
            result['manifest']=manifest
            bundle=base64.b64decode(next(x.split(':',1)[1] for x in raw.splitlines() if x.startswith('OMS_BUNDLE:')))
            with tarfile.open(fileobj=io.BytesIO(bundle)) as tar:
                actual={m.name:hashlib.sha256(tar.extractfile(m).read()).hexdigest() for m in tar
                        if m.isfile() and included(m.name)}
                if actual!=manifest or any(not m.isfile() or not included(m.name) for m in tar):
                    raise ValueError('Runtime bundle manifest mismatch')
            folder=Path('.staging-artifacts/rollback'); folder.mkdir(exist_ok=True)
            target=folder/(service['deployment_id']+'.tar')
            if not target.exists(): target.write_bytes(bundle)
            with tarfile.open(target) as saved:
                stored={m.name:hashlib.sha256(saved.extractfile(m).read()).hexdigest() for m in saved
                        if m.isfile() and included(m.name)}
                if stored!=manifest or any(not m.isfile() or not included(m.name) for m in saved):
                    raise ValueError('Previously captured rollback archive differs from runtime')
            result['captured_source_archive']=str(target)
            result['archive_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
            result['runtime_packages']=json.loads(next(x.split(':',1)[1] for x in raw.splitlines() if x.startswith('OMS_PACKAGES:')))
            result['captured_source_verified']=True
        except Exception as error:
            result.update(source_reproducible=False,error_type=type(error).__name__)
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        report=list(pool.map(audit,[s for s in preflight['services'] if s['name']!='Postgres']))
    Path('.staging-artifacts/rollback-artifacts.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k!='manifest'} for r in report],indent=2))


if __name__=='__main__':
    main()
