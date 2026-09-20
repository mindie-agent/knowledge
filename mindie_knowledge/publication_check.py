"""Validate Git publication data using the installed, trusted runtime.

CI should invoke this with ``python -I -m mindie_knowledge.publication_check``
from a pinned wheel. Candidate files are read as Git blobs, never imported or
executed. An empty publication is valid.
"""
from __future__ import annotations
import argparse,json,os,re,time
from pathlib import Path
from .community.batch import check_path,validate_feedback
from .community.common import CommunityError,run_argv
from .loop.documents import MAX_FILE_BYTES,parse_entry
from .redact import scan_text

MAX_TREE_BYTES=1024*1024
MAX_PUBLIC_BYTES=32*1024*1024
MAX_PUBLIC_FILES=1024

def _git(repo,args,maximum,deadline):
    remaining=deadline-time.monotonic()
    if remaining<=0:raise ValueError("Publication validation deadline exceeded")
    result=run_argv(['git','-C',str(repo),*args],timeout=min(25,remaining),max_output=maximum,input_bytes=b'',
                    env={'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_COUNT':'1',
                         'GIT_CONFIG_KEY_0':'core.hooksPath','GIT_CONFIG_VALUE_0':os.devnull})
    if result.timed_out or result.code:
        raise ValueError('Cannot read the candidate Git publication')
    return result.out

def validate(repo,revision,domain):
    if not re.fullmatch(r'[0-9a-f]{40}',revision):
        raise ValueError('revision must be a full immutable Git commit SHA')
    deadline=time.monotonic()+30
    if _git(repo,['cat-file','-t',revision],4096,deadline).strip()!=b'commit':
        raise ValueError('revision must identify a commit')
    listing=_git(repo,['ls-tree','-r','-z','--long',revision],MAX_TREE_BYTES,deadline)
    entries={};feedback=[];total=0;count=0
    for raw in listing.split(b'\0'):
        if not raw:continue
        head,path=raw.decode('utf-8','strict').split('\t',1)
        mode,kind,sha,size=head.split()
        if not path.startswith(('cases/','topics/','feedback/')):
            if path.startswith(('corpus/','generations/')):
                raise ValueError('Retired knowledge layout is not supported')
            continue
        check_path(path)
        if mode!='100644' or kind!='blob' or not size.isdigit():
            raise ValueError(f'{path}: publication requires a plain data blob')
        count+=1;total+=int(size)
        if count>MAX_PUBLIC_FILES or int(size)>MAX_FILE_BYTES or total>MAX_PUBLIC_BYTES:
            raise ValueError('Publication exceeds bounded file or byte limits')
        text=_git(repo,['cat-file','blob',sha],MAX_FILE_BYTES+4096,deadline).decode('utf-8','strict')
        if '\r' in text:raise ValueError(f'{path}: canonical LF bytes required')
        if scan_text(text):raise ValueError(f'{path}: public data fails the privacy scan')
        if path.startswith('feedback/'):
            feedback.append(validate_feedback(text,path));continue
        doc=parse_entry(text)
        if doc['domain']!=domain:raise ValueError(f'{path}: wrong domain')
        expected='topics/' if doc['kind']=='knowledge' else 'cases/'
        if not path.startswith(expected):raise ValueError(f'{path}: kind does not match directory')
        if doc['entry_id'] in entries:raise ValueError('Duplicate canonical entry identity')
        entries[doc['entry_id']]=doc
    return {'commit':revision,'entries':len(entries),'feedback_files':len(feedback),'bytes':total}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--domain',default='vllm-ascend')
    args=parser.parse_args(argv)
    try:result=validate(args.repo,args.revision,args.domain)
    except (ValueError,CommunityError,OSError,UnicodeError) as exc:
        parser.exit(1,f'Invalid publication: {str(exc)[:500]}\n')
    print(json.dumps(result,sort_keys=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
