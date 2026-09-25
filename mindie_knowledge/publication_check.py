"""Validate Git publication data using the installed, trusted runtime.

CI should invoke this with ``python -I -m mindie_knowledge.publication_check``
from a pinned wheel. Candidate files are read as Git blobs, never imported or
executed. An empty publication is valid.

There is no whole-publication file-count or total-byte business cap and no
fixed whole-tree metadata output cap: the tree listing streams to a scratch
file parsed in bounded chunks, and each blob is validated against the real
per-file platform envelope as it is read through one bounded persistent
``git cat-file --batch`` process (at most one body in memory at a time).

One normal call advances in bounded slices and never restarts a large fixed
commit at item zero: fully verified blobs are checkpointed per PATH (content
checks are path-scoped, so the same blob under another path is re-examined)
bound to the exact commit and the exact validation context (domain, pinned
validator version, privacy-rule set, schemas) — a changed commit, domain or
validator never trusts stale entries, and candidate input never supplies
trusted cache entries. The checkpoint lives in the candidate repo's Git
metadata dir by default (``--state FILE`` overrides), so an interrupted or
slow first run resumes where it stopped inside the same invocation and
across invocations, without the host rerunning anything; the host's own job
budget still bounds the total. Content checks keep whole-record semantics —
the privacy scanner runs on the complete file text, preserving cross-line
detection exactly.
"""
from __future__ import annotations
import argparse,importlib.metadata,json,os,re,tempfile,time
from pathlib import Path
from .community.batch import check_path,validate_feedback
from .community.common import SCHEMA_FEEDBACK,CommunityError,run_argv,sha256_text
from .gitread import CatFileBatch,iter_file_records,run_stdout_to_file,with_windows_longpaths
from .loop import documents
from .loop.documents import MAX_FILE_BYTES,parse_entry
from .redact import scan_text

def _git_env():
    return with_windows_longpaths({'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_COUNT':'1',
        'GIT_CONFIG_KEY_0':'core.hooksPath','GIT_CONFIG_VALUE_0':os.devnull})

# One bounded validation slice; the host (CI job, CLI caller) bounds the
# total across automatically continued slices.
SLICE_SECONDS=25
_CHECKPOINT_EVERY=64
_STATE_NAME='mindie-validator-state.json'

class _SliceExhausted(Exception):
    """The bounded slice expired; verified progress is already persisted."""

# The trusted validation implementation whose checkpoints may be reused:
# the installed package's own Python sources (never read from the candidate
# repository — candidate content must never choose the execution identity).
_PACKAGE_ROOT = Path(__file__).resolve().parent
_TRUSTED_SOURCES = (
    'redact.py',
    'gitread.py',
    'publication_check.py',
    'loop/documents.py',
    'community/batch.py',
    'community/common.py',
)


def _trusted_source_fingerprint():
    """sha256 over the installed trusted validator's own Python sources.

    Binds the checkpoint to the actual implementation — a changed finder
    body, helper, parse or validation function invalidates old checkpoints
    even when version, profile, rule ids and patterns stay the same. None
    when any source is unreadable; an unverifiable identity must never claim
    an old cache is trustworthy."""
    import hashlib

    digest = hashlib.sha256()
    for rel in _TRUSTED_SOURCES:
        try:
            digest.update((_PACKAGE_ROOT / rel).read_bytes())
        except OSError:
            return None
    return digest.hexdigest()


def _validator_context(domain):
    """Fingerprint of the exact validation context: domain, the installed
    validator version, the redaction profile, and the actual trusted
    implementation bytes. A tightened rule under the same id/version — or a
    changed finder body — invalidates prior checkpoints. Candidate files
    never supply this identity."""
    from mindie_knowledge import redact as _redact

    try:
        version = importlib.metadata.version('mindie-knowledge')
    except Exception:
        version = 'unknown'
    return {
        'domain': domain,
        'validator': version,
        'redaction_profile': _redact.REDACTION_PROFILE,
        'sources': _trusted_source_fingerprint(),
        'entry_schema': documents.SCHEMA,
        'feedback_schema': SCHEMA_FEEDBACK,
    }

def _default_state_path(repo):
    repo=Path(repo)
    gitdir=repo if (repo/'objects').is_dir() else repo/'.git'
    if gitdir.is_dir():
        return gitdir/_STATE_NAME
    return Path(tempfile.gettempdir())/f'mindie-validator-{sha256_text(str(repo))}.json'

def _load_checkpoint(path,revision,context):
    if path is None:return {}
    if not isinstance(context,dict) or context.get('sources') is None:
        return {}  # an unverifiable validator identity never trusts a cache
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {}
    if not isinstance(data,dict):
        return {}
    if data.get('commit')!=revision or data.get('context')!=context:
        return {}
    verified=data.get('verified')
    if not isinstance(verified,dict):return {}
    clean={}
    for path,value in verified.items():
        if not isinstance(path,str):continue
        if value is True:
            clean[path]=True
        elif (isinstance(value,dict) and isinstance(value.get('id'),str)
              and value.get('kind') in ('knowledge','experience')
              and isinstance(value.get('sha'),str)):
            clean[path]={'id':value['id'],'kind':value['kind'],'sha':value['sha']}
    return clean

def _save_checkpoint(path,revision,context,verified):
    if path is None:return
    target=Path(path);tmp=target.with_name(target.name+'.tmp')
    tmp.write_text(json.dumps({'commit':revision,'context':context,
                               'verified':verified},sort_keys=True),encoding='utf-8')
    os.replace(tmp,target)

def _git(repo,args,maximum,deadline):
    remaining=deadline-time.monotonic()
    if remaining<=0:raise _SliceExhausted()
    result=run_argv(['git','-C',str(repo),*args],timeout=min(25,remaining),max_output=maximum,input_bytes=b'',
                    env=_git_env())
    if result.timed_out:raise _SliceExhausted()
    if result.code:raise ValueError('Cannot read the candidate Git publication')
    return result.out

def _validate_slice(repo,revision,domain,state_path,context,verified,stats):
    """One bounded slice over the candidate tree; resumes from ``verified``.

    Cheap per-path checks (layout, path allowlist, mode, size, directory/kind
    consistency, duplicate identity) always run against the current listing,
    even for checkpointed paths; only the expensive body read/parse/scan is
    skipped for a path already verified under the exact same commit and
    validation context."""
    deadline=time.monotonic()+SLICE_SECONDS
    fd,listing_name=tempfile.mkstemp(prefix='mindie-ls-tree-')
    os.close(fd)  # the name is reopened by path; the descriptor never leaks
    listing_file=Path(listing_name)
    try:
        run_stdout_to_file(
            ['git','-C',str(repo),'ls-tree','-r','-z','--long',revision],
            listing_file,timeout=min(25,max(1.0,deadline-time.monotonic())),
            env=_git_env())
        reader=None
        pending=0
        try:
            for raw in iter_file_records(listing_file,separator=b'\0'):
                if not raw:continue
                head,path=raw.decode('utf-8','strict').split('\t',1)
                mode,kind,sha,size=head.split()
                if not path.startswith(('cases/','topics/','feedback/')):
                    if path.startswith(('corpus/','generations/')):
                        raise ValueError('Retired knowledge layout is not supported')
                    continue
                if path in stats['done_paths']:
                    continue  # fully processed in an earlier slice of this call
                check_path(path)
                if mode!='100644' or kind!='blob' or not size.isdigit():
                    raise ValueError(f'{path}: publication requires a plain data blob')
                if int(size)>MAX_FILE_BYTES:
                    raise ValueError(f'{path}: exceeds the per-file platform envelope')
                prior=verified.get(path)
                if prior is not None and (prior is True or prior.get('sha')==sha):
                    stats['count']+=1;stats['bytes']+=int(size)
                    if prior is True:
                        stats['feedback']+=1
                    else:
                        expected='topics/' if prior['kind']=='knowledge' else 'cases/'
                        if not path.startswith(expected):
                            raise ValueError(f'{path}: kind does not match directory')
                        if prior['id'] in stats['entries']:
                            raise ValueError('Duplicate canonical entry identity')
                        stats['entries'].add(prior['id'])
                    stats['done_paths'].add(path)
                    continue
                if time.monotonic()>=deadline:
                    raise _SliceExhausted()
                if reader is None:
                    reader=CatFileBatch(repo,env=_git_env())
                try:
                    blob=reader.read(sha,deadline=deadline,max_bytes=MAX_FILE_BYTES)
                except TimeoutError:
                    raise _SliceExhausted() from None
                if blob is None:raise ValueError(f'{path}: cannot read the candidate blob')
                text=blob.decode('utf-8','strict')
                if '\r' in text:raise ValueError(f'{path}: canonical LF bytes required')
                if scan_text(text):raise ValueError(f'{path}: public data fails the privacy scan')
                if path.startswith('feedback/'):
                    validate_feedback(text,path);stats['feedback']+=1
                    verified[path]=True
                else:
                    doc=parse_entry(text)
                    if doc['domain']!=domain:raise ValueError(f'{path}: wrong domain')
                    expected='topics/' if doc['kind']=='knowledge' else 'cases/'
                    if not path.startswith(expected):raise ValueError(f'{path}: kind does not match directory')
                    if doc['entry_id'] in stats['entries']:raise ValueError('Duplicate canonical entry identity')
                    stats['entries'].add(doc['entry_id'])
                    verified[path]={'id':doc['entry_id'],'kind':doc['kind'],'sha':sha}
                stats['done_paths'].add(path)
                stats['count']+=1;stats['bytes']+=int(size)
                pending+=1
                if pending%_CHECKPOINT_EVERY==0:
                    _save_checkpoint(state_path,revision,context,verified)
        finally:
            if reader is not None:
                reader.close()
    finally:
        try:listing_file.unlink()
        except OSError:pass
    _save_checkpoint(state_path,revision,context,verified)
    return stats

def validate(repo,revision,domain,state=None):
    if not re.fullmatch(r'[0-9a-f]{40}',revision):
        raise ValueError('revision must be a full immutable Git commit SHA')
    if _git(repo,['cat-file','-t',revision],4096,time.monotonic()+SLICE_SECONDS).strip()!=b'commit':
        raise ValueError('revision must identify a commit')
    context=_validator_context(domain)
    state_path=state if state is not None else _default_state_path(repo)
    verified=_load_checkpoint(state_path,revision,context)
    stats={'count':0,'bytes':0,'entries':set(),'feedback':0,'done_paths':set()}
    stalled=0
    while True:
        before=len(verified)
        try:
            stats=_validate_slice(repo,revision,domain,state_path,context,verified,stats)
            break
        except _SliceExhausted:
            # Verified progress was persisted; continue with a fresh slice.
            stalled=0 if len(verified)>before else stalled+1
            if stalled>=2:
                raise ValueError('no validation progress across bounded slices') from None
    return {'commit':revision,'entries':len(stats['entries']),
            'feedback_files':stats['feedback'],'bytes':stats['bytes']}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--domain',default='vllm-ascend')
    parser.add_argument('--state',default=None,
                        help='checkpoint file override (default: the candidate repo Git metadata dir)')
    args=parser.parse_args(argv)
    try:result=validate(args.repo,args.revision,args.domain,state=args.state)
    except (ValueError,CommunityError,OSError,UnicodeError) as exc:
        parser.exit(1,f'Invalid publication: {str(exc)[:500]}\n')
    print(json.dumps(result,sort_keys=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
