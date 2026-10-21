#!/usr/bin/env python3
"""GitHub-hosted read-only cloud projection publisher; never runs a UI build."""
from __future__ import annotations
import argparse,hashlib,json,os,pathlib,re,shutil,sqlite3,subprocess,tempfile,time
from datetime import datetime,timezone
from history_cloud import from_environment,file_hash
from history_public import build_public,media_permitted,VERSION_PATH
from history_api import project,connect

REPO='xiuyuan-shi/xiuyuan-shi.github.io'
STATE='.github/cloud-history-state.json'

def utc():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def git(root,*args,check=True):
 r=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True)
 if check and r.returncode:raise RuntimeError('Git operation failed: '+args[0])
 return r.stdout.strip()
def load(path):return json.loads(path.read_text()) if path.is_file() else {}
def hydrate(client,store,checkout):
 db=connect(store)
 try:
  data=project(db)
  records={r['id']:json.loads(r['normalized_json']) for r in db.execute('SELECT id,normalized_json FROM record_revisions')}
  assets={r['id']:dict(r) for r in db.execute('SELECT * FROM assets')}
 finally:db.close()
 needed=set()
 for event in data['events']:
  allowed={m['asset_id'] for m in records[event['revision_id']]['media'] if media_permitted(m)}
  needed.update('AS-'+image['sha256'] for image in event['images'] if 'AS-'+image['sha256'] in allowed)
 for identity in sorted(needed):
  asset=assets[identity];target=(store/asset['path']).resolve()
  if not target.is_relative_to(store.resolve()):raise ValueError('Unsafe cloud asset path')
  target.parent.mkdir(parents=True,exist_ok=True)
  cached=next((p for p in (checkout/'api/history/assets').glob(identity+'.*') if p.is_file() and file_hash(p)==asset['sha256']),None)
  if cached:shutil.copyfile(cached,target)
  else:client.download(asset['path'],target,asset['sha256'])
 return len(needed)

def apply_release(root,release,prior):
 desired=release.files
 for name,raw in desired.items():
  if not re.fullmatch(r'api/history/(?:version\.json|snapshots/[a-f0-9]{64}\.json|assets/AS-[a-f0-9]{64}\.(?:jpg|png|webp))',name):raise ValueError('Unexpected public output')
  path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
 for path in (root/'api/history/assets').glob('*'):
  if path.is_file() and path.relative_to(root).as_posix() not in desired:path.unlink()
 current=release.manifest['url'].lstrip('/')
 previous=[p for p in prior.get('snapshots',[]) if p!=current and (root/p).is_file()]
 if len(previous)<2:
  previous+=sorted(p.relative_to(root).as_posix() for p in (root/'api/history/snapshots').glob('*.json') if p.relative_to(root).as_posix()!=current and p.relative_to(root).as_posix() not in previous)
 keep=[current,*previous[:2]]
 for path in (root/'api/history/snapshots').glob('*.json'):
  if path.relative_to(root).as_posix() not in keep:path.unlink()
 return keep

def decide_sync(cloud_version,prior,repo_revision,live_revision,today):
 return {'export':prior.get('cloud_version')!=cloud_version or bool(prior.get('public_revision') and repo_revision!=prior['public_revision']),
         'deploy':bool(repo_revision and repo_revision!=live_revision),
         'health':prior.get('last_checked_at','')[:10]!=today}

def read_live_revision():
 response=subprocess.run(['/usr/bin/curl','--fail','--silent','--show-error','--compressed','--max-time','30','https://xiuyuan-shi.github.io/api/history/version.json?cloud_verify='+str(time.time_ns())],capture_output=True,check=True)
 manifest=json.loads(response.stdout)
 revision=manifest.get('revision','')
 if not re.fullmatch('[a-f0-9]{64}',revision):raise ValueError('Invalid current public data revision')
 return revision

def write_outputs(values):
 print(json.dumps(values,ensure_ascii=False),flush=True)
 if os.environ.get('GITHUB_OUTPUT'):
  with open(os.environ['GITHUB_OUTPUT'],'a') as f:
   for k,v in values.items():f.write(k+'='+str(v).lower()+'\n')

def main():
 p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');args=p.parse_args()
 if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('GITHUB_REPOSITORY')!=REPO:raise RuntimeError('Publisher runs only in the configured GitHub-hosted repository')
 root=pathlib.Path(os.environ['GITHUB_WORKSPACE']).resolve();client=from_environment()
 initial=client.status()
 if not initial['ready']:write_outputs({'data_changed':False,'status':'cloud_not_ready'});return
 prior=load(root/STATE)
 if args.check:
  write_outputs({'needs_export':prior.get('cloud_version')!=initial['version'],'status':'checked'});return
 # This is a disposable Actions checkout, never the user's working tree.
 git(root,'fetch','origin','main');git(root,'checkout','-B','cloud-history-sync','origin/main')
 prior=load(root/STATE);release=None
 live_revision=read_live_revision()
 decision=decide_sync(initial['version'],prior,load(root/VERSION_PATH).get('revision'),live_revision,utc()[:10])
 if decision['export']:
  git(root,'sparse-checkout','disable')
  with tempfile.TemporaryDirectory(prefix='private-history-cloud-') as directory:
   store=pathlib.Path(directory)/'store'
   snapshot=client.snapshot(store)
   images=hydrate(client,store,root)
   release=build_public(store)
   if client.status()['version']!=snapshot['version']:raise RuntimeError('Cloud changed during projection; retry on next cloud run')
   version=snapshot['version']
 else:version=initial['version']
 old_public=load(root/VERSION_PATH)
 data_changed=bool(release and old_public.get('revision')!=release.data['revision'])
 if not release and not decision['health']:
  write_outputs({'data_changed':decision['deploy'],'status':'deployment_retry' if decision['deploy'] else 'unchanged','data_commit':git(root,'rev-parse','HEAD')});return
 public_revision=release.data['revision'] if release else prior.get('public_revision')
 files=release.files if release else None
 for attempt in range(3):
  git(root,'fetch','origin','main');git(root,'reset','--hard','origin/main')
  current=load(root/STATE)
  app=load(root/'public-release.json')
  if app.get('data_mode')!='live' or app.get('data_endpoint')!='/api/history/version.json':raise RuntimeError('Current frontend is not compatible with the versioned data API')
  if not release and load(root/VERSION_PATH).get('revision')!=current.get('public_revision'):raise RuntimeError('Repository data changed during health update; reconcile on next cloud run')
  snapshots=apply_release(root,release,current) if release else current.get('snapshots',[])
  state={'schema_version':'1.0.0','cloud_version':version,'public_revision':public_revision,'snapshots':snapshots,'last_checked_at':utc(),'source':'private-cloud-d1','frontend_app_commit':app['app_commit']}
  path=root/STATE;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(state,indent=2)+'\n')
  git(root,'add','--','api/history',STATE)
  changes=git(root,'diff','--cached','--name-only').splitlines()
  if not changes:
   pending=load(root/VERSION_PATH).get('revision')!=live_revision
   write_outputs({'data_changed':pending,'status':'deployment_retry' if pending else 'unchanged','data_commit':git(root,'rev-parse','HEAD')});return
  if any(not (n.startswith('api/history/') or n==STATE) for n in changes):raise RuntimeError('Cloud data publisher cannot change frontend files')
  data_changed=any(n.startswith('api/history/') for n in changes) or public_revision!=live_revision
  git(root,'config','user.name','github-actions[bot]');git(root,'config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
  message=('data: mirror cloud history '+str(public_revision)[:12]) if data_changed else 'chore: record cloud history sync health'
  git(root,'commit','-m',message)
  # gh reads the job-scoped token from GH_TOKEN; no account token is stored.
  r=subprocess.run(['git','-C',str(root),'-c','credential.helper=','-c','credential.helper=!gh auth git-credential','push','origin','HEAD:main'],capture_output=True,text=True)
  if r.returncode==0:
   write_outputs({'data_changed':data_changed,'status':'mirrored' if data_changed else 'healthy','data_commit':git(root,'rev-parse','HEAD'),'public_revision':public_revision,'frontend_app_commit':app['app_commit']});return
  time.sleep(2)
 raise RuntimeError('Concurrent repository changes prevented a safe update')
if __name__=='__main__':main()
