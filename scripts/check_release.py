"""Scan staged/current source and every reachable Git commit without printing secrets."""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
PATTERNS=[
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b'),
    re.compile(rb'\bAKIA[A-Z0-9]{16}\b'),
]


def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT)


def forbidden(path):
    parts=Path(path).parts
    return any(p in ('data','backups','.venv','node_modules','chat analysis','private','__pycache__') for p in parts) or Path(path).name in ('.secrets.key','secrets.enc','.env') or path.endswith(('.sqlite3','.db','.mp4','.mov','.wav','.mp3','.zip'))


def scan(history=True):
    findings=[];checked=set()
    paths=git('ls-files','--cached','--others','--exclude-standard','-z').decode().split('\0')
    for path in filter(None,paths):
        if forbidden(path):findings.append({'path':path,'reason':'private_or_runtime_file'});continue
        f=ROOT/path
        if f.is_symlink():findings.append({'path':path,'reason':'symlink'});continue
        if not f.is_file():continue
        data=f.read_bytes();checked.add(path)
        if len(data)>10*1024*1024:findings.append({'path':path,'reason':'file_over_10mb'})
        if any(p.search(data) for p in PATTERNS):findings.append({'path':path,'reason':'credential_pattern'})
    commits=[]
    if history:
        try:commits=git('rev-list','--all').decode().splitlines()
        except subprocess.CalledProcessError:pass
        seen=set()
        for commit in commits:
            for row in git('ls-tree','-r',commit).decode().splitlines():
                info,path=row.split('\t',1);mode,kind,sha=info.split()
                if forbidden(path):findings.append({'commit':commit,'path':path,'reason':'history_private_file'})
                if kind=='blob' and sha not in seen:
                    seen.add(sha)
                    if any(p.search(git('cat-file','blob',sha)) for p in PATTERNS):findings.append({'commit':commit,'path':path,'reason':'history_credential_pattern'})
    return {'status':'passed' if not findings else 'failed','source_files_scanned':len(checked),'reachable_commits_scanned':len(commits),'findings':findings,'scope':'known credential patterns, forbidden paths, source and all reachable history; not a guarantee against all secret formats'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output');parser.add_argument('--no-history',action='store_true');args=parser.parse_args()
    result=scan(not args.no_history);text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:Path(args.output).write_text(text+'\n')
    print(text);raise SystemExit(0 if result['status']=='passed' else 1)
