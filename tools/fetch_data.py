"""Missing-only, verified OWON downloads. Standard library; never copies Pi home.

Completed runs only. CSV/legacy NPZ + metadata/session/events are retained.
Canonical local layout: KST-date_site/original-Pi-run-id. Existing raw copies in
earlier layouts are reused by run id, filename and SHA-256, not downloaded again.
"""
import argparse
import base64
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

RUN_RE = re.compile(r'\d{8}T\d{6}Z(?:_[0-9a-f]{8})?\Z')
REMOTE_ROOT = '/home/jjiwon/owon-rfi/data'
REMOTE = r'''
import base64, hashlib, json, os, re, sys, tarfile
from pathlib import Path
ROOT = Path('/home/jjiwon/owon-rfi/data')
RUN = re.compile(r'\d{8}T\d{6}Z(?:_[0-9a-f]{8})?\Z')
def allowed(name):
    return name in ('session.json','events.log','baseline.csv') or bool(re.fullmatch(r'(?:raw_[A-Za-z0-9_]+\.csv|window_[A-Za-z0-9_]+\.npz|metadata_[A-Za-z0-9_]+\.json)',name))
def check_run(run):
    if not RUN.fullmatch(run): raise ValueError('Invalid run id')
    folder=ROOT/run
    if folder.is_symlink() or folder.resolve().parent != ROOT.resolve(): raise ValueError('Unsafe run path')
    path=folder/'session.json'
    if path.is_symlink(): raise ValueError('Symlink session')
    s=json.loads(path.read_text())
    if s.get('complete') is not True: raise ValueError('Run is incomplete: '+run)
    return folder,s
def main(args):
    if ROOT.is_symlink(): raise ValueError('Data root must not be a symlink')
    if args['action']=='inventory':
        result={'runs':[],'skipped':[]}
        for folder in sorted(ROOT.iterdir()):
            if not RUN.fullmatch(folder.name) or not folder.is_dir(): continue
            try: folder,s=check_run(folder.name)
            except (ValueError,OSError) as e:
                result['skipped'].append({'run':folder.name,'reason':str(e)}); continue
            files=[]
            for path in sorted(folder.iterdir()):
                if path.is_symlink() or not path.is_file() or not allowed(path.name): continue
                before=path.stat()
                digest=hashlib.sha256()
                with path.open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''): digest.update(block)
                after=path.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns): raise ValueError('File changed during inventory')
                files.append({'name':path.name,'size':after.st_size,'sha256':digest.hexdigest()})
            result['runs'].append({'id':folder.name,'start_utc_ns':s['start_utc_ns'],'site':s['config'].get('site','UNSET'),'files':files})
        print(json.dumps(result))
    elif args['action']=='download':
        with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
            for item in args['files']:
                folder,s=check_run(item['run'])
                if not allowed(item['name']): raise ValueError('Not an observation file')
                path=folder/item['name']
                if path.is_symlink() or path.resolve().parent!=folder.resolve(): raise ValueError('Unsafe file path')
                archive.add(path,arcname=item['run']+'/'+item['name'],recursive=False)
    else: raise ValueError('Unknown action')
'''


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def safe_destination(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Local path escapes destination: {relative}')
    # Do not follow junctions/symlinks inside the selected destination.
    for candidate in (path, *path.parents):
        if candidate == root.parent: break
        if candidate.exists() or candidate.is_symlink():
            st = candidate.lstat()
            if candidate.is_symlink() or getattr(st,'st_file_attributes',0) & 0x400:
                raise ValueError(f'Symlink/reparse point is not an allowed destination: {candidate}')
        if candidate == root: break
    return path


def local_relative(run):
    if not RUN_RE.fullmatch(run['id']): raise ValueError('Invalid remote run id')
    stamp = datetime.fromtimestamp(int(run['start_utc_ns'])//10**9, timezone(timedelta(hours=9)))
    site = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(run['site'])).strip(' .') or 'UNSET'
    site = site[:80]
    return Path(f'{stamp:%Y-%m-%d}_{site}') / run['id']


def local_copies(root):
    """Inspect observation session files only, never .ssh/browser/editor caches."""
    result = {}
    patterns = ['*/*/session.json', '*/*/raw/session.json', '*/session.json',
                '_commissioning/*/session.json', '_incoming/data/*/session.json',
                '_incoming/*/session.json', 'owon-rfi/data/*/session.json', 'data/*/session.json']
    for pattern in patterns:
        for path in root.glob(pattern):
            try:
                safe_destination(root,path.relative_to(root))
                session = json.loads(path.read_text(encoding='utf-8'))
                run_id = PurePosixPath(session['directory']).name
                if RUN_RE.fullmatch(run_id):
                    result.setdefault(run_id, set()).add(path.parent)
            except (ValueError,KeyError,OSError):
                continue
    return result


def selected_files(inventory, run_filter=None, file_filter=None):
    if run_filter and not RUN_RE.fullmatch(run_filter): raise ValueError('Run must be a UTC run folder name')
    if file_filter:
        bits = file_filter.replace('\\','/').split('/')
        if len(bits)==2:
            if run_filter and run_filter!=bits[0]: raise ValueError('Run and File disagree')
            run_filter,file_filter=bits
        elif len(bits)!=1: raise ValueError('File must be filename or run-id/filename')
        if not run_filter or not RUN_RE.fullmatch(run_filter) or file_filter in ('','.','..'):
            raise ValueError('Use -File run-id/filename, or -Run run-id -File filename')
    selected=[]
    seen=set()
    for run in inventory['runs']:
        if run_filter and run['id']!=run_filter: continue
        relative=local_relative(run)
        for file in run['files']:
            name=file['name']
            if '/' in name or '\\' in name or name in ('.','..') or not re.fullmatch(r'[A-Za-z0-9_.-]+',name):
                raise ValueError('Unsafe remote filename')
            if not re.fullmatch(r'[a-f0-9]{64}',file['sha256']) or file['size']<0: raise ValueError('Invalid inventory hash/size')
            if file_filter and name!=file_filter: continue
            target=relative/name
            # Windows paths are case-insensitive: never merge two runs accidentally.
            key=str(target).casefold()
            if key in seen: raise ValueError(f'Two remote files map to one local path: {target}')
            seen.add(key)
            selected.append(dict(file,run=run['id'],relative=target))
    if (file_filter or run_filter) and not selected: raise ValueError('No matching completed observation file/run')
    return selected


def plan_files(root, selected):
    copies=local_copies(root)
    plan=[]
    for item in selected:
        target=safe_destination(root,item['relative'])
        # Protect a pre-existing canonical directory even for single-file mode.
        session_path=target.parent/'session.json'
        if session_path.exists():
            session=json.loads(session_path.read_text(encoding='utf-8'))
            if PurePosixPath(session['directory']).name != item['run']:
                raise ValueError(f'Local run identity conflict: {target.parent}')
        if target.exists():
            if not target.is_file() or target.stat().st_size!=item['size'] or digest_file(target)!=item['sha256']:
                raise ValueError(f'Existing file differs; not overwritten: {target}')
            plan.append(dict(item,action='SKIP',target=target)); continue
        reuse=None
        for folder in sorted(copies.get(item['run'],set())):
            candidate=safe_destination(root,(folder/item['name']).relative_to(root))
            if candidate.is_file() and candidate.stat().st_size==item['size'] and digest_file(candidate)==item['sha256']:
                reuse=candidate; break
        plan.append(dict(item,action='LOCAL' if reuse else 'DOWNLOAD',target=target,source=reuse))
    return plan


def publish(stream, item, root):
    target=safe_destination(root,item['relative'])
    target.parent.mkdir(parents=True,exist_ok=True)
    safe_destination(root,item['relative'])
    fd, temporary=tempfile.mkstemp(prefix='.'+target.name+'.fetch-',suffix='.part',dir=target.parent)
    part=Path(temporary)
    try:
        h=hashlib.sha256(); count=0
        with os.fdopen(fd,'wb') as output:
            for block in iter(lambda:stream.read(1024*1024),b''):
                output.write(block); h.update(block); count+=len(block)
            output.flush(); os.fsync(output.fileno())
        if count!=item['size'] or h.hexdigest()!=item['sha256']:
            raise ValueError(f'Transfer hash/size mismatch: {item["name"]}')
        # Atomic no-overwrite commit on NTFS/Linux; keep an existing file safe.
        os.link(part,target)
    finally:
        part.unlink(missing_ok=True)


def ssh_command(args, payload):
    request=base64.b64encode(json.dumps(payload).encode()).decode()
    source=REMOTE + '\nmain(json.loads(base64.b64decode("'+request+'")))\n'
    code=base64.b64encode(source.encode()).decode()
    command="python3 -c 'import base64;exec(base64.b64decode(\""+code+"\"))'"
    return ['ssh','-i',args.key,'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=8','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',args.target,command]


def fetch_remote(args, items, root):
    if not items: return
    # Bound command size for Windows CreateProcess; each batch is one SSH stream.
    for begin in range(0,len(items),40):
        batch=items[begin:begin+40]
        payload={'action':'download','files':[{'run':i['run'],'name':i['name']} for i in batch]}
        expected={i['run']+'/'+i['name']:i for i in batch}
        proc=subprocess.Popen(ssh_command(args,payload),stdout=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=proc.stdout,mode='r|') as archive:
                for member in archive:
                    if not member.isfile() or member.name not in expected: raise ValueError('Unexpected archive member')
                    item=expected.pop(member.name)
                    if member.size!=item['size']: raise ValueError('Remote file size changed')
                    with archive.extractfile(member) as stream: publish(stream,item,root)
                    print('DOWNLOADED',item['target'],flush=True)
            proc.stdout.close()
            if proc.wait(timeout=60)!=0 or expected: raise ValueError('SSH transfer incomplete; rerun to fetch remaining files')
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            if proc.stdout: proc.stdout.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',default='jjiwon@10.12.194.1')
    p.add_argument('--key',required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--run'); p.add_argument('--file'); p.add_argument('--list-only',action='store_true')
    args=p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+',args.target): p.error('Invalid user@host')
    root=args.destination.absolute()
    reply=subprocess.run(ssh_command(args,{'action':'inventory'}),capture_output=True,text=True,encoding='utf-8',timeout=120)
    if reply.returncode: raise RuntimeError(reply.stderr.strip() or 'SSH inventory failed')
    inventory=json.loads(reply.stdout)
    for skipped in inventory['skipped']: print('SKIP RUN',skipped['run'],skipped['reason'])
    selected=selected_files(inventory,args.run,args.file)
    plan=plan_files(root,selected)
    counts={action:sum(i['action']==action for i in plan) for action in ('SKIP','LOCAL','DOWNLOAD')}
    print('Remote scope:',REMOTE_ROOT,'(completed observation runs only)')
    print('Plan:',json.dumps(counts),'| missing download bytes:',sum(i['size'] for i in plan if i['action']=='DOWNLOAD'))
    for item in plan:
        if args.list_only or item['action']!='SKIP': print(item['action'],item['run']+'/'+item['name'],'->',item['target'])
    if args.list_only:
        print('ListOnly: no files or directories created.'); return
    for item in plan:
        if item['action']=='LOCAL':
            with item['source'].open('rb') as stream: publish(stream,item,root)
    fetch_remote(args,[i for i in plan if i['action']=='DOWNLOAD'],root)
    print('Done. SHA-256 verified; existing files and Pi source data were not modified.')
    if args.file: print('Single-file mode: companion session/metadata files are NOT fetched; use -Run for notebook-ready data.')


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError,tarfile.TarError) as exc:
        print('ERROR:',exc,file=sys.stderr); sys.exit(1)
