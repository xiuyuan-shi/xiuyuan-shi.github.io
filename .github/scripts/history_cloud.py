#!/usr/bin/env python3
"""Private cloud history store client. Public projection stays a separate read API."""
from __future__ import annotations
import argparse, gzip, hashlib, json, os, pathlib, sqlite3, subprocess, sys, tempfile, time
from urllib.parse import urlencode

ORIGIN='https://wanguo-history-data-20261021.lucas865260.chatgpt.site'

class CloudError(RuntimeError):
 def __init__(self,status,message='Cloud request failed'):
  super().__init__(message+' (HTTP '+str(status)+')');self.status=status;self.private_detail=None

class CloudClient:
 def __init__(self,access_token,sync_token=None,admin_token=None,origin=ORIGIN):
  if origin!=ORIGIN:raise ValueError('Cloud credential destination does not match the configured Site')
  if not access_token:raise ValueError('Cloud service access is missing')
  self.origin=origin;self.access=access_token;self.sync=sync_token;self.admin=admin_token
 def request(self,route,data=None,method=None,upload=None,output=None,extra_headers=None):
  if not route.startswith('/') or '\n' in route or '\r' in route:raise ValueError('Invalid API path')
  with tempfile.TemporaryDirectory(prefix='wanguo-cloud-') as temporary:
   temp=pathlib.Path(temporary);target=pathlib.Path(output) if output else temp/'response'
   target.parent.mkdir(parents=True,exist_ok=True)
   config=['url = '+json.dumps(self.origin+'/api'+route),'header = '+json.dumps('OAI-Sites-Authorization: Bearer '+self.access)]
   if self.sync:config.append('header = '+json.dumps('X-History-Sync-Token: '+self.sync))
   if self.admin:config.append('header = '+json.dumps('Authorization: Bearer '+self.admin))
   for name,header_value in (extra_headers or {}).items():config.append('header = '+json.dumps(name+': '+str(header_value)))
   args=['/usr/bin/curl','--disable','--config','-','--silent','--show-error','--compressed','--connect-timeout','20','--max-time','240','--output',str(target),'--write-out','%{http_code}']
   if data is not None:
    payload=temp/'payload.json';payload.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')))
    config.append('header = "Content-Type: application/json"');args+=['--data-binary','@'+str(payload)]
   if upload:args+=['--upload-file',str(upload)]
   if method=='HEAD':args+=['--head']
   elif method:args+=['--request',method]
   result=subprocess.run(args,input=('\n'.join(config)+'\n').encode(),capture_output=True,timeout=250)
   if result.returncode:raise CloudError(0,'Cloud HTTPS request did not complete; reconcile before retrying a write')
   status=int(result.stdout.decode() or 0)
   if not 200<=status<300:
    error=CloudError(status)
    try:
     detail=json.loads(target.read_text()).get('error')
     if isinstance(detail,str):error.private_detail=detail[:1000]
    except (ValueError,AttributeError):pass
    raise error
   if output:return target
   if method=='HEAD':
    headers={line.split(':',1)[0].lower():line.split(':',1)[1].strip() for line in target.read_text().splitlines() if ':' in line}
    return {'bytes':int(headers.get('content-length',0)),'sha256':headers.get('x-asset-sha256','')}
   return json.loads(target.read_text())
 def status(self):return self.request('/status')
 def schema(self):return self.request('/schema')['tables']
 def rows(self,table,limit=100):
  after=0
  while True:
   with tempfile.TemporaryDirectory(prefix='private-history-table-') as temporary:
    path=pathlib.Path(temporary)/'rows.jsonl'
    self.request('/table-export?'+urlencode({'table':table,'after':after}),output=path)
    footer=None
    with path.open() as source:
     for line in source:
      row=json.loads(line)
      if set(row)=={'_history_export'}:footer=row['_history_export']
      else:yield row
    if footer is None:raise ValueError('Cloud table export was incomplete')
    if footer['after'] is None:return
    if footer['after']<=after:raise ValueError('Cloud export cursor did not advance')
    after=footer['after']
 def upload(self,key,sha,path,check_existing=True):
  if file_hash(path)!=sha:raise ValueError('Local asset checksum mismatch')
  path=pathlib.Path(path)
  if check_existing:
   try:
    info=self.request('/blob?'+urlencode({'key':key}),method='HEAD')
    if info['sha256']==sha and info['bytes']==path.stat().st_size:return {'stored':True,'existing':True,**info}
   except CloudError as error:
    if error.status!=404:raise
  with tempfile.TemporaryDirectory(prefix='wanguo-upload-') as temporary:
   compressed=pathlib.Path(temporary)/'asset.gz'
   with open(path,'rb') as source,open(compressed,'wb') as target:
    with gzip.GzipFile(fileobj=target,mode='wb',compresslevel=1,mtime=0) as stream:
     import shutil
     shutil.copyfileobj(source,stream,1024*1024)
   if compressed.stat().st_size<path.stat().st_size*0.88:
    return self.request('/blob?'+urlencode({'key':key,'sha256':sha}),method='PUT',upload=compressed,extra_headers={'Content-Encoding':'gzip','X-History-Raw-Bytes':path.stat().st_size})
   return self.request('/blob?'+urlencode({'key':key,'sha256':sha}),method='PUT',upload=path)
 def download(self,key,target,sha=None):
  self.request('/blob?'+urlencode({'key':key}),output=target)
  if sha and file_hash(target)!=sha:raise ValueError('Cloud asset checksum mismatch')
  return pathlib.Path(target)
 def transaction(self,operations,version=None):
  version=version or self.status()['version']
  try:return self.request('/transaction',{'version':version,'operations':operations})
  except CloudError as error:
   if error.status not in (0,409):raise
   # A lost response can follow a committed transaction. Read back each exact
   # primary key before considering another mutation; never repeat blindly.
   tables=self.schema()
   for op in operations:
    key=op.get('where') or {k:op['row'][k] for k in tables[op['table']]['primary_key']}
    result=self.request('/query',{'table':op['table'],'primary_key':key})['rows']
    if len(result)!=1 or any(result[0].get(k)!=v for k,v in op['row'].items()):raise error
   return {'idempotent':True,'version':self.status()['version']}
 def snapshot(self,directory,require_ready=True):
  directory=pathlib.Path(directory);directory.mkdir(parents=True,exist_ok=True)
  initial=self.status()
  if require_ready and not initial['ready']:raise ValueError('Cloud migration is not sealed')
  schema=self.schema();path=directory/'history.sqlite3'
  if path.exists():raise ValueError('Snapshot destination is not empty')
  db=sqlite3.connect(path)
  try:
   db.execute('PRAGMA foreign_keys=OFF')
   for name,spec in schema.items():db.execute(spec['sql'])
   for name,spec in schema.items():
    names=spec['columns'];sql='INSERT INTO "'+name+'" ('+','.join('"'+c+'"' for c in names)+') VALUES ('+','.join('?' for _ in names)+')'
    for row in self.rows(name):db.execute(sql,[row[c] for c in names])
   if self.status()['version']!=initial['version']:raise ValueError('Cloud changed during export; discard and retry')
   errors=db.execute('PRAGMA foreign_key_check').fetchall()
   if errors:raise ValueError('Cloud export has foreign key violations')
   db.commit()
  except BaseException:
   db.close();path.unlink(missing_ok=True);raise
  else:db.close()
  return {'version':initial['version'],'directory':str(directory),'schema':schema}

def file_hash(path):
 digest=hashlib.sha256()
 with open(path,'rb') as file:
  for block in iter(lambda:file.read(1024*1024),b''):digest.update(block)
 return digest.hexdigest()

def from_environment(admin=False):
 access=os.environ.get('HISTORY_CLOUD_ACCESS_TOKEN')
 sync=os.environ.get('HISTORY_CLOUD_SYNC_TOKEN')
 key=os.environ.get('HISTORY_CLOUD_ADMIN_TOKEN') if admin else None
 return CloudClient(access,sync,key)

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['status','snapshot','transaction']);p.add_argument('--output',type=pathlib.Path);p.add_argument('--input',type=pathlib.Path);args=p.parse_args()
 c=from_environment(admin=args.command=='transaction')
 if args.command=='status':result=c.status()
 elif args.command=='snapshot':
  if not args.output:p.error('--output is required')
  result=c.snapshot(args.output)
 else:
  if not args.input:p.error('--input is required')
  data=json.loads(args.input.read_text());result=c.transaction(data['operations'],data.get('version'))
 print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()


def freeze_local_store(directory):
 """Make the retired archive reject writes even from already-open connections."""
 directory=pathlib.Path(directory).resolve()
 db=sqlite3.connect(directory/'history.sqlite3',timeout=60)
 try:
  db.execute('BEGIN IMMEDIATE')
  names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
  for name in names:
   quoted='"'+name.replace('"','""')+'"'
   for operation in ('INSERT','UPDATE','DELETE'):
    trigger='"cloud_readonly_'+name.replace('"','""')+'_'+operation.lower()+'"'
    db.execute('CREATE TRIGGER IF NOT EXISTS '+trigger+' BEFORE '+operation+' ON '+quoted+" BEGIN SELECT RAISE(ABORT,'Primary history database moved to the cloud; local archive is read-only'); END")
  db.commit()
 finally:db.close()
 return {'protected_tables':len(names),'local_write_authority':False}
