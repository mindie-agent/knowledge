"""[Role] Validate Git publication data with the installed, trusted runtime.

CI invokes this with ``python -I -m mindie_knowledge.publication_check`` from a
pinned wheel. Candidate bytes are read as Git blobs, never imported or run.
[Invariants] Contract/1 validates complete task packages and feedback;
contract/2 parses every graded unit through GradedUnit, checks exact semantic
paths, stable-ID uniqueness, the complete inheritance graph, persisted revision
and digest, and validates optional derived routing data against the actual
units. Without a contract/2 unit tree an old task corpus is never "empty".
[Timeouts & Locks] One bounded persistent ``git cat-file --batch`` read at a
time; listing output streams to scratch and validation has no elapsed-time
slice. Checkpoints bind verified blobs to the exact commit and context.
[Side-Effect Policy] Read-only candidate inspection plus scratch/checkpoint
files; never execute candidate content.
"""
from __future__ import annotations
import argparse,importlib.metadata,json,os,re,sys,tempfile
from contextlib import closing
from pathlib import Path
from .community.batch import check_path,validate_feedback
from .community.common import SCHEMA_FEEDBACK,CommunityError,git_executable,run_argv,sha256_text
from .gitread import CatFileBatch,iter_file_records,run_stdout_to_file,with_windows_longpaths
from .loop import documents
from .loop.documents import MAX_FILE_BYTES
from .materials.store import BLOCK_SCHEMA, _parse, _parse_manifest, _parse_block, _sha
from .redact import scan_text
from .publication_contract import (
    DATA_FORMAT_V2, GRADED_SCHEMA, SCHEMA, data_format, read_git_contract,
)
from .graded.feed import (
    ROUTING_MANIFEST_PATH, ROUTING_MANIFEST_SCHEMA,
    SHARD_TOPOLOGY_PATH, SHARD_TOPOLOGY_SCHEMA, classify_path,
    is_graded_unit_path, validate_derived,
)
from .graded.index import MAX_UNIT_BYTES, RoutingIndex
from .graded.units import SCHEMA as GRADED_UNIT_SCHEMA
from .graded.units import GradedUnit, relative_path

def _git_env():
    return with_windows_longpaths({'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never','GIT_CONFIG_COUNT':'1',
        'GIT_CONFIG_KEY_0':'core.hooksPath','GIT_CONFIG_VALUE_0':os.devnull})

_CHECKPOINT_EVERY=64
_STATE_NAME='mindie-validator-state.json'
PUBLICATION_GIT_TIMEOUT_SECONDS = 300.0

def _graded_feedback_schema():
    try:
        from .graded.feedback import SCHEMA_FEEDBACK
    except ImportError:
        return 'unavailable'
    return SCHEMA_FEEDBACK


def _validator_context(domain):
    """Fingerprint of the validation context: domain, validator version,
    redaction profile and active rule patterns, and schema versions."""
    import hashlib
    from mindie_knowledge import redact as _redact

    try:
        version = importlib.metadata.version('mindie-knowledge')
    except Exception:
        version = 'unknown'
    rules_digest = hashlib.sha256(
        repr([(r.id, getattr(r.pattern, 'pattern', None)) for r in _redact.RULES]).encode('utf-8')
    ).hexdigest()
    return {
        'domain': domain,
        'validator': version,
        'redaction_profile': _redact.REDACTION_PROFILE,
        'redaction_rules': rules_digest,
        'entry_schema': documents.SCHEMA,
        'package_schema': 'mindie-material-package/1',
        'feedback_schema': SCHEMA_FEEDBACK,
        'graded_unit_schema': GRADED_UNIT_SCHEMA,
        'graded_feedback_schema': _graded_feedback_schema(),
        'graded_routing_schema': ROUTING_MANIFEST_SCHEMA,
        'graded_topology_schema': SHARD_TOPOLOGY_SCHEMA,
    }

def _default_state_path(repo):
    repo=Path(repo)
    gitdir=repo if (repo/'objects').is_dir() else repo/'.git'
    if gitdir.is_dir():
        return gitdir/_STATE_NAME
    return Path(tempfile.gettempdir())/f'mindie-validator-{sha256_text(str(repo))}.json'

def _load_checkpoint(path,revision,context):
    if path is None or not isinstance(context,dict):
        return {}
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
        if (isinstance(value,dict) and value.get('type') in ('task','block','feedback','unit')
              and isinstance(value.get('sha'),str)):
            clean[path]=value
    return clean

def _save_checkpoint(path,revision,context,verified):
    if path is None:return
    target=Path(path);tmp=target.with_name(target.name+'.tmp')
    tmp.write_text(json.dumps({'commit':revision,'context':context,
                               'verified':verified},sort_keys=True),encoding='utf-8')
    os.replace(tmp,target)

def _git(repo,args,maximum):
    result=run_argv([git_executable(),'-C',str(repo),*args],timeout=PUBLICATION_GIT_TIMEOUT_SECONDS,max_output=maximum,input_bytes=b'',
                    env=_git_env())
    if result.code:raise ValueError('Cannot read the candidate Git publication')
    return result.out


def _record_verified(path, value, stats):
    if value['type']=='feedback':
        if not path.startswith('feedback/'):
            raise ValueError(f'{path}: checkpoint type differs from path')
        stats['feedback']+=1
        return
    parts=path.split('/')
    task_id=parts[1]
    if value['type']=='task':
        if len(parts)!=3 or parts[2]!='index.md' or value['header']['task_id']!=task_id:
            raise ValueError(f'{path}: task manifest identity differs from path')
        if task_id in stats['entries']:
            raise ValueError('Duplicate canonical task identity')
        stats['entries'][task_id]=value['header']
    else:
        if len(parts)!=4 or parts[2]!='blocks' or parts[3]!=value['block_id']+'.md':
            raise ValueError(f'{path}: material block identity differs from path')
        stats['blocks'].setdefault(task_id,{})[value['block_id']]=value


def _validate_task_sets(stats):
    """Same-commit topology and hashes, using metadata only after blob checks."""
    if set(stats['blocks'])-set(stats['entries']):
        raise ValueError('Material blocks have no task index in the selected commit')
    for task_id,header in stats['entries'].items():
        actual=stats['blocks'].get(task_id,{})
        expected={block['block_id']:block for block in header['blocks']}
        if set(actual)!=set(expected):
            raise ValueError(f'tasks/{task_id}: files differ from the exact manifest reference set')
        for block_id,descriptor in expected.items():
            blob=actual[block_id]
            if blob['sha256']!=descriptor['sha256'] or blob['source_range']!=descriptor['source_range']:
                raise ValueError(f'tasks/{task_id}/blocks/{block_id}.md: material block hash or source mismatch')
        if not all(block['indexed'] for block in header['blocks']):
            raise ValueError(f'tasks/{task_id}: public material indexing is incomplete')

def _unit_from_checkpoint(value, path):
    if not is_graded_unit_path(path):
        raise ValueError(f'{path}: unsafe or noncanonical graded unit path')
    if value.get('path') != path:
        raise ValueError(f'{path}: checkpoint path differs from the tree path')
    try:
        metadata, body = value['metadata'], value['body']
        unit = GradedUnit.from_parts(metadata, body, path)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'{path}: checkpointed graded unit is invalid: {exc}') from exc
    expected = f"{unit.metadata['domain_path']}/{unit.id}.md"
    if path != expected:
        raise ValueError(f'{path}: unit path must be exactly {expected}')
    return unit


def _unit_from_text(text, path):
    if not is_graded_unit_path(path):
        raise ValueError(f'{path}: unsafe or noncanonical graded unit path')
    unit = GradedUnit.from_text(text, path)
    expected = f"{unit.metadata['domain_path']}/{unit.id}.md"
    if path != expected:
        raise ValueError(f'{path}: unit path must be exactly {expected}')
    return unit


def _json_object(text, path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'{path}: duplicate derived JSON field {key!r}')
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{path}: derived data is not valid JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{path}: derived data must be an object')
    return value


def _scan_graded_unit(unit, path):
    """Scan C5 metadata and body; a recognized Git locator is not SSH text."""
    from .graded.privacy import metadata_findings
    findings = [*metadata_findings(unit.metadata, path), *scan_text(unit.body, path=path)]
    if findings:
        raise ValueError(f'{path}: public data fails the privacy scan')


def _scan_graded_json(value, path):
    """Scan derived JSON while accepting the same C5 Git locators as units."""
    from .graded.privacy import metadata_findings
    if metadata_findings(value, path):
        raise ValueError(f'{path}: public data fails the privacy scan')


def _validate_graded_feedback(text, path):
    # The feedback worker owns this schema and validator; a contract/2 tree
    # with feedback cannot be called valid while that authority is unavailable.
    try:
        from .graded.feedback import validate_feedback as graded_validate_feedback
    except ImportError as exc:
        raise ValueError(
            f'{path}: graded feedback validation is unavailable; '
            'mindie_knowledge.graded.feedback is required'
        ) from exc
    return graded_validate_feedback(text, path)["votes"]


def _record_graded(unit, stats):
    stats['graded_units'].append(unit)


def _validate_graded_tree(repo, revision, domain, state_path, context, verified, stats):
    """Validate a contract/2 snapshot; every unit is parsed and graph-checked.

    The listing streams to a scratch file; verified unit bodies are reused only
    under the exact same commit and validation context, and the whole unit graph
    and derived data are still revalidated on every call.
    """
    fd, listing_name = tempfile.mkstemp(prefix='mindie-ls-tree-')
    os.close(fd)
    listing_file = Path(listing_name)
    derived = {}
    try:
        run_stdout_to_file(
            [git_executable(), '-C', str(repo), 'ls-tree', '-r', '-z', '--long', revision],
            listing_file, timeout=PUBLICATION_GIT_TIMEOUT_SECONDS, env=_git_env(),
        )
        reader = None
        pending = 0
        try:
            with closing(iter_file_records(listing_file, separator=b'\0')) as records:
                for raw in records:
                    if not raw:
                        continue
                    head, path = raw.decode('utf-8', 'strict').split('\t', 1)
                    mode, kind, sha, size = head.split()
                    content_kind = classify_path(path)
                    if content_kind in (None, 'metadata'):
                        continue
                    if content_kind == 'unit' and not is_graded_unit_path(path):
                        raise ValueError(f'{path}: unsafe or noncanonical graded unit path')
                    if path in stats['done_paths']:
                        continue
                    if content_kind == 'feedback':
                        check_path(path)
                    relative_path(path)
                    if mode != '100644' or kind != 'blob' or not size.isdigit():
                        raise ValueError(f'{path}: publication requires a plain data blob')
                    maximum = MAX_UNIT_BYTES if content_kind == 'unit' else MAX_FILE_BYTES
                    if int(size) > maximum:
                        raise ValueError(f'{path}: exceeds the per-file platform envelope')
                    prior = verified.get(path)
                    if content_kind == 'unit' and prior is not None and prior.get('sha') == sha:
                        unit = _unit_from_checkpoint(prior, path)
                        _record_graded(unit, stats)
                        stats['count'] += 1
                        stats['bytes'] += int(size)
                        stats['done_paths'].add(path)
                        continue
                    if reader is None:
                        reader = CatFileBatch(repo, env=_git_env())
                    blob = reader.read(sha, deadline=None, max_bytes=maximum)
                    if blob is None:
                        # A missing blob is not evidence of deletion; no snapshot
                        # switch may happen from an incomplete read.
                        raise OSError(f'{path}: the listed candidate blob is unavailable')
                    text = blob.decode('utf-8', 'strict')
                    if '\r' in text or '\x00' in text:
                        raise ValueError(f'{path}: canonical LF bytes required')
                    if content_kind == 'unit':
                        unit = _unit_from_text(text, path)
                        _scan_graded_unit(unit, path)
                        _record_graded(unit, stats)
                        verified[path] = {
                            'type': 'unit', 'sha': sha, 'path': path,
                            'metadata': unit.metadata, 'body': unit.body,
                        }
                    elif content_kind == 'feedback':
                        if scan_text(text):
                            raise ValueError(f'{path}: public data fails the privacy scan')
                        stats['graded_feedback'].extend(_validate_graded_feedback(text, path))
                        stats['feedback'] += 1
                    else:
                        value = _json_object(text, path)
                        _scan_graded_json(value, path)
                        derived[path] = value
                    stats['count'] += 1
                    stats['bytes'] += int(size)
                    stats['done_paths'].add(path)
                    pending += 1
                    if pending % _CHECKPOINT_EVERY == 0:
                        _save_checkpoint(state_path, revision, context, verified)
        finally:
            if reader is not None:
                reader.close()
    finally:
        pending_error = sys.exc_info()[1]
        try:
            listing_file.unlink()
        except OSError as cleanup_error:
            if pending_error is None:
                raise
            pending_error.add_note('Scratch cleanup also failed: ' + str(cleanup_error))
    _save_checkpoint(state_path, revision, context, verified)
    index = RoutingIndex(stats['graded_units'])
    units = {unit.id: unit for unit in stats['graded_units']}
    if len(units) != len(stats['graded_units']):
        raise ValueError('Duplicate canonical unit identity')
    validate_derived(derived.get(ROUTING_MANIFEST_PATH), derived.get(SHARD_TOPOLOGY_PATH),
                     units, index)
    from .graded.feedback import validate_bindings
    validate_bindings(stats['graded_feedback'], units)
    stats['graded_index'] = index
    return stats


def _validate_tree(repo,revision,domain,state_path,context,verified,stats):
    """Stream the candidate tree; reuse verified per-path checkpoints.

    Cheap per-path checks (layout, path allowlist, mode, size, directory/kind
    consistency, duplicate identity) always run against the current listing,
    even for checkpointed paths; only the expensive body read/parse/scan is
    skipped for a path already verified under the exact same commit and
    validation context."""
    fd,listing_name=tempfile.mkstemp(prefix='mindie-ls-tree-')
    os.close(fd)  # the name is reopened by path; the descriptor never leaks
    listing_file=Path(listing_name)
    try:
        run_stdout_to_file(
            [git_executable(),'-C',str(repo),'ls-tree','-r','-z','--long',revision],
            listing_file,timeout=PUBLICATION_GIT_TIMEOUT_SECONDS,
            env=_git_env())
        reader=None
        pending=0
        try:
            with closing(iter_file_records(listing_file,separator=b'\0')) as records:
                for raw in records:
                    if not raw:continue
                    head,path=raw.decode('utf-8','strict').split('\t',1)
                    mode,kind,sha,size=head.split()
                    if not path.startswith(('tasks/','feedback/')):
                        if path.startswith(('cases/','topics/','corpus/','generations/')):
                            raise ValueError('Retired knowledge layout is not supported')
                        if classify_path(path) == 'unit':
                            raise ValueError(
                                f'{path}: unsupported content layout for publication-contract/1'
                            )
                        continue
                    if path in stats['done_paths']:
                        continue  # fully processed in an earlier slice of this call
                    check_path(path)
                    if mode!='100644' or kind!='blob' or not size.isdigit():
                        raise ValueError(f'{path}: publication requires a plain data blob')
                    if int(size)>MAX_FILE_BYTES:
                        raise ValueError(f'{path}: exceeds the per-file platform envelope')
                    prior=verified.get(path)
                    if prior is not None and prior.get('sha')==sha:
                        stats['count']+=1;stats['bytes']+=int(size)
                        _record_verified(path,prior,stats)
                        stats['done_paths'].add(path)
                        continue
                    if reader is None:
                        reader=CatFileBatch(repo,env=_git_env())
                    blob=reader.read(sha,deadline=None,max_bytes=MAX_FILE_BYTES)
                    if blob is None:raise ValueError(f'{path}: cannot read the candidate blob')
                    text=blob.decode('utf-8','strict')
                    if '\r' in text:raise ValueError(f'{path}: canonical LF bytes required')
                    if scan_text(text):raise ValueError(f'{path}: public data fails the privacy scan')
                    if path.startswith('feedback/'):
                        validate_feedback(text,path)
                        value={'type':'feedback','sha':sha}
                    elif path.endswith('/index.md'):
                        header=_parse_manifest(text,domain)
                        value={'type':'task','sha':sha,'header':header}
                    else:
                        header,_body=_parse(text)
                        if set(header)!={'schema','block_id','source_range'} or header['schema']!=BLOCK_SCHEMA:
                            raise ValueError(f'{path}: invalid material block header')
                        value={'type':'block','sha':sha,'block_id':header['block_id'],
                               'source_range':header['source_range'],'sha256':_sha(text)}
                        _parse_block(text,value)
                    _record_verified(path,value,stats)
                    verified[path]=value
                    stats['done_paths'].add(path)
                    stats['count']+=1;stats['bytes']+=int(size)
                    pending+=1
                    if pending%_CHECKPOINT_EVERY==0:
                        _save_checkpoint(state_path,revision,context,verified)
        finally:
            if reader is not None:
                reader.close()
    finally:
        pending_error=sys.exc_info()[1]
        try:listing_file.unlink()
        except OSError as cleanup_error:
            if pending_error is None:
                raise
            pending_error.add_note('Scratch cleanup also failed: '+str(cleanup_error))
    _save_checkpoint(state_path,revision,context,verified)
    return stats

def _v2_content_path(path):
    if path.startswith("tasks/"):
        return False
    try:
        return classify_path(path) in {"unit", "feedback", "derived"}
    except ValueError:
        return False


def classify_changes(repo, base_revision, revision, *, schema=SCHEMA):
    """Separate ordinary data PRs from development changes at exact commits.

    A valid tree does not authorize changing workflow, contract or policy.
    Return a classification; maintainers can review development PRs normally,
    but the content Bot must not auto-merge them. Contract/2 treats graded
    units, feedback and validated derived routing data as content; the retired
    task layout remains development rather than an implicit content upgrade.
    """
    if schema not in (SCHEMA, GRADED_SCHEMA):
        raise ValueError("unsupported publication contract schema")
    for name, value in (("base_revision", base_revision), ("revision", revision)):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError(f"{name} must be an immutable Git commit")
    with tempfile.TemporaryDirectory(prefix="mindie-publication-diff-") as directory:
        listing = Path(directory) / "paths"
        run_stdout_to_file(
            [git_executable(), "-C", str(repo), "diff", "--no-renames", "--name-status", "-z", base_revision, revision, "--"],
            listing, timeout=PUBLICATION_GIT_TIMEOUT_SECONDS, env=_git_env(),
        )
        count, development = 0, []
        # A rejected transition may leave unread records. Close the generator
        # before deleting its file, including on Windows, preserving the
        # actual validation failure rather than a later cleanup error.
        with closing(iter_file_records(listing, separator=b"\0")) as records:
            for raw in records:
                if not raw:
                    continue
                status = raw.decode("ascii", "strict")
                path = next(records).decode("utf-8", "strict")
                count += 1
                if schema != GRADED_SCHEMA and status == "M" and re.fullmatch(r"tasks/[0-9a-f]{64}/blocks/[0-9a-f]{64}\.md", path):
                    raise ValueError(f"{path}: immutable block changed under the same identity; replace it with a new block")
                if schema == GRADED_SCHEMA:
                    if not _v2_content_path(path):
                        development.append(path)
                    continue
                try:
                    check_path(path)
                except (ValueError, CommunityError):
                    development.append(path)
    return {"base_commit": base_revision, "changed_files": count,
            "content_only": bool(count) and not development,
            "review_mode": "development" if development else "content",
            "development_paths": development}


def validate(repo,revision,domain,state=None,*,base_revision=None):
    if not re.fullmatch(r'[0-9a-f]{40}',revision):
        raise ValueError('revision must be a full immutable Git commit SHA')
    if _git(repo,['cat-file','-t',revision],4096).strip()!=b'commit':
        raise ValueError('revision must identify a commit')
    contract = read_git_contract(repo, revision, domain, env=_git_env())
    selected_format = data_format(contract['contract'])
    context=_validator_context(domain)
    context['publication_contract_sha256'] = contract['sha256']
    context['contract_schema'] = contract['contract']['schema']
    state_path=state if state is not None else _default_state_path(repo)
    verified=_load_checkpoint(state_path,revision,context)
    if selected_format == DATA_FORMAT_V2:
        stats={'count':0,'bytes':0,'feedback':0,'done_paths':set(),'graded_units':[],'graded_feedback':[]}
        stats=_validate_graded_tree(repo,revision,domain,state_path,context,verified,stats)
        entries=len(stats['graded_units'])
    else:
        stats={'count':0,'bytes':0,'entries':{},'blocks':{},'feedback':0,'done_paths':set()}
        stats=_validate_tree(repo,revision,domain,state_path,context,verified,stats)
        _validate_task_sets(stats)
        entries=len(stats['entries'])
    result = {'commit':revision,'entries':entries,
              'feedback_files':stats['feedback'],'bytes':stats['bytes'],
              'contract_sha256': contract['sha256'], 'content_only': None,
              'data_format': selected_format}
    if base_revision is not None:
        result.update(classify_changes(repo, base_revision, revision,
                                       schema=contract['contract']['schema']))
    return result

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--domain',default='vllm-ascend')
    parser.add_argument('--base-revision', help='exact trusted base for content-only PR classification')
    parser.add_argument('--state',default=None,
                        help='checkpoint file override (default: the candidate repo Git metadata dir)')
    args=parser.parse_args(argv)
    try:result=validate(args.repo,args.revision,args.domain,state=args.state,
                        base_revision=args.base_revision)
    except (ValueError,CommunityError,OSError,UnicodeError) as exc:
        parser.exit(1,f'Invalid publication: {str(exc)[:500]}\n')
    print(json.dumps(result,sort_keys=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
