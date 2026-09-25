from pathlib import Path
import json
import re
import subprocess
import pytest
from mindie_knowledge.publication_check import validate
from mindie_knowledge.loop.documents import make_entry,render_entry

def git(repo,*args,timeout=10):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True,timeout=timeout).strip()

def publish(tmp_path,files,*,add_timeout=10):
    repo=tmp_path/'repo';repo.mkdir()
    git(repo,'init','-q','-b','main')
    git(repo,'config','user.name','check');git(repo,'config','user.email','check@example.invalid')
    for name,text in files.items():
        p=repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8',newline='\n')
    git(repo,'add','.',timeout=add_timeout);git(repo,'commit','-qm','publication')
    return repo,git(repo,'rev-parse','HEAD')

def test_empty_repository_metadata_is_valid(tmp_path):
    repo,sha=publish(tmp_path,{'README.md':'# Empty\n','AGENTS.md':'Publication instructions.\n'})
    assert validate(repo,sha,'vllm-ascend')['entries']==0

def test_plain_canonical_data_only(tmp_path):
    doc=make_entry(entry_id='a'*64,domain='vllm-ascend',kind='experience',title='Observed boundary',summary='A bounded observation.',content='Detailed evidence and its limits.')
    repo,sha=publish(tmp_path,{'cases/observation.md':render_entry(doc)})
    assert validate(repo,sha,'vllm-ascend')['entries']==1
    git(repo,'update-index','--chmod=+x','cases/observation.md');git(repo,'commit','-qm','executable data')
    with pytest.raises(ValueError,match='plain data blob'):
        validate(repo,git(repo,'rev-parse','HEAD'),'vllm-ascend')

def test_public_revision_or_legacy_field_is_rejected(tmp_path):
    doc=make_entry(entry_id='b'*64,domain='vllm-ascend',kind='experience',title='Revision integrity',summary='Digest binds body.',content='Original body.')
    text=render_entry(doc).replace('entry_id:',f'revision: {"0"*64}\nentry_id:',1)
    repo,sha=publish(tmp_path,{'cases/revision.md':text})
    with pytest.raises(ValueError,match='unknown entry fields'):
        validate(repo,sha,'vllm-ascend')

def test_knowledge_with_citations_in_body_and_empty_conditions_is_valid(tmp_path):
    doc=make_entry(entry_id='c'*64,domain='vllm-ascend',kind='knowledge',
                   title='Reference with unspecified version',summary='Source omits a version.',
                   content='See https://example.com/reference for the detailed limits.',
                   conditions={})
    repo,sha=publish(tmp_path,{'topics/reference.md':render_entry(doc)})
    assert validate(repo,sha,'vllm-ascend')['entries']==1


def test_more_than_1024_entries_validate(tmp_path):
    files={}
    for i in range(1027):
        doc=make_entry(entry_id=f'{i:04x}'+'0'*60,domain='vllm-ascend',kind='experience',
                       title=f'Accumulated case {i}',summary=f'Summary {i}.',
                       content=f'Detailed body of case {i}.')
        files[f'cases/{i:04x}.md']=render_entry(doc)
    repo,sha=publish(tmp_path,files,add_timeout=60)
    result=validate(repo,sha,'vllm-ascend')
    assert result['entries']==1027


def test_checkpoint_resume_skips_reverified_blobs(tmp_path, monkeypatch):
    files={}
    for i in range(70):
        doc=make_entry(entry_id=f'{i:04x}'+'0'*60,domain='vllm-ascend',kind='experience',
                       title=f'Checkpoint case {i}',summary=f'Summary {i}.',
                       content=f'Body of checkpoint case {i}.')
        files[f'cases/{i:04x}.md']=render_entry(doc)
    repo,sha=publish(tmp_path,files)
    state=tmp_path/'validator-state.json'

    import mindie_knowledge.publication_check as pc
    from mindie_knowledge import gitread
    monkeypatch.setattr(pc,'_CHECKPOINT_EVERY',16)
    real_read=gitread.CatFileBatch.read
    reads=[]
    fail={'armed':True}
    def counting_read(self,rev,**kwargs):
        if fail['armed'] and len(reads)==40:
            fail['armed']=False
            reads.append(rev)
            raise OSError('transient read failure mid-validation')
        reads.append(rev)
        return real_read(self,rev,**kwargs)
    monkeypatch.setattr(gitread.CatFileBatch,'read',counting_read)
    with pytest.raises(OSError,match='transient read failure'):
        pc.validate(repo,sha,'vllm-ascend',state=state)
    assert len(reads)==41  # 40 verified-or-attempted reads plus the faulting one
    # Checkpoints landed at 16 and 32 verified blobs; the same exact commit
    # resumes after them instead of restarting at item zero.
    result=pc.validate(repo,sha,'vllm-ascend',state=state)
    assert result['entries']==70
    assert len(reads)==41+(70-32)
    # A fully checkpointed commit revalidates with zero blob reads.
    result2=pc.validate(repo,sha,'vllm-ascend',state=state)
    assert result2['entries']==70 and len(reads)==79
    # A different commit never trusts the checkpoint: full revalidation.
    doc=make_entry(entry_id='f'*64,domain='vllm-ascend',kind='experience',
                   title='Late case',summary='s',content='New body.')
    (repo/'cases'/'ffff.md').write_text(render_entry(doc),encoding='utf-8',newline='\n')
    git(repo,'add','.');git(repo,'commit','-qm','more')
    sha2=git(repo,'rev-parse','HEAD')
    result3=pc.validate(repo,sha2,'vllm-ascend',state=state)
    assert result3['entries']==71 and len(reads)==79+71


def test_oversized_blob_rejected_at_the_platform_envelope(tmp_path, monkeypatch):
    doc=make_entry(entry_id='d'*64,domain='vllm-ascend',kind='experience',
                   title='Oversized case',summary='s',content='x'*4096)
    text=render_entry(doc)
    repo,sha=publish(tmp_path,{'cases/oversized.md':text})
    import mindie_knowledge.publication_check as pc
    monkeypatch.setattr(pc,'MAX_FILE_BYTES',2048)
    with pytest.raises(ValueError,match='per-file platform envelope'):
        pc.validate(repo,sha,'vllm-ascend')


def test_same_blob_under_another_path_is_not_cache_skipped(tmp_path):
    """One blob that is legal as feedback must still be fully examined when it
    also appears as an entry path — checkpoints are per-path, never per blob."""
    fb=json.dumps({'schema':'mindie-feedback/1','votes':[]},indent=2,sort_keys=True)+'\n'
    repo,sha=publish(tmp_path,{'feedback/a.json':fb,'topics/b.md':fb})
    for state in (None,tmp_path/'state.json'):
        with pytest.raises(ValueError):
            validate(repo,sha,'vllm-ascend',state=state)


def test_checkpoint_is_bound_to_domain_and_validator_context(tmp_path, monkeypatch):
    doc=make_entry(entry_id='a'*64,domain='npu',kind='experience',
                   title='Domain bound',summary='s',content='Body text.')
    repo,sha=publish(tmp_path,{'cases/a.md':render_entry(doc)})
    state=tmp_path/'state.json'
    assert validate(repo,sha,'npu',state=state)['entries']==1
    # The same commit under another domain never trusts the checkpoint.
    with pytest.raises(ValueError,match='wrong domain'):
        validate(repo,sha,'other',state=state)

    from mindie_knowledge import gitread
    real_read=gitread.CatFileBatch.read
    reads=[]
    def counting(self,rev,**kwargs):
        reads.append(rev);return real_read(self,rev,**kwargs)
    # Same commit + same context: zero blob re-reads.
    monkeypatch.setattr(gitread.CatFileBatch,'read',counting)
    assert validate(repo,sha,'npu',state=state)['entries']==1
    assert reads==[]
    # A state file whose context no longer matches (rules/policy changed)
    # is never trusted: the blob is read again.
    import json as _json
    data=_json.loads(state.read_text())
    data['context']='staged-by-an-older-ruleset'
    state.write_text(_json.dumps(data))
    assert validate(repo,sha,'npu',state=state)['entries']==1
    assert len(reads)==1


def test_checkpoint_binds_the_installed_validator_sources(tmp_path, monkeypatch):
    """A changed trusted implementation body — same rule id, same version,
    same profile, same pattern flags — invalidates prior checkpoints, and the
    fresh run then governs. Sources are read only from the installed trusted
    package, never from the candidate repository."""
    import shutil

    import mindie_knowledge
    import mindie_knowledge.publication_check as pc
    import mindie_knowledge.redact as redact_mod

    # A controlled copy of the trusted sources: the probe shape is an owned
    # copy of the validator package, the original product untouched.
    pkg = Path(mindie_knowledge.__file__).resolve().parent
    copied = tmp_path / 'trusted-copy'
    for rel in pc._TRUSTED_SOURCES:
        target = copied / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pkg / rel, target)

    doc = make_entry(entry_id='a'*64, domain='npu', kind='experience',
                     title='Policy case', summary='s',
                     content='Body mentions policy-canary-1 as plain text.')
    repo, sha = publish(tmp_path, {'cases/a.md': render_entry(doc)})
    state = tmp_path / 'state.json'
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1
    original = pc._validator_context('npu')

    from mindie_knowledge import gitread
    real_read = gitread.CatFileBatch.read
    reads = []
    def counting(self, rev, **kwargs):
        reads.append(rev)
        return real_read(self, rev, **kwargs)
    monkeypatch.setattr(gitread.CatFileBatch, 'read', counting)
    # Same installed implementation: the checkpoint is trusted (no re-read).
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1
    assert reads == []

    # Edit one trusted implementation body in the copy (a finder body, as in
    # the probe): version, profile, ids and patterns all stay the same.
    redact_copy = copied / 'redact.py'
    redact_copy.write_bytes(redact_copy.read_bytes() + b"\n# tightened finder body\n")
    monkeypatch.setattr(pc, '_PACKAGE_ROOT', copied)
    assert pc._validator_context('npu')['sources'] != original['sources']
    # The old checkpoint is not trusted: the blob is read again (fresh run).
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1
    assert len(reads) == 1

    # With the tightened rule actually in force (as a further source edit —
    # in reality the tightened finder lives in the changed bytes), the old
    # checkpoint is again not trusted and the fresh scan rejects.
    redact_copy.write_bytes(redact_copy.read_bytes() + b"# tighten policy-canary rule\n")
    first = redact_mod.RULES[0]
    tightened = type(first)(
        id=first.id, description=first.description, hint=first.hint,
        pattern=re.compile(r'policy-canary-\d+'),
    )
    monkeypatch.setattr(redact_mod, 'RULES', (tightened,) + redact_mod.RULES[1:])
    with pytest.raises(ValueError, match='privacy scan'):
        validate(repo, sha, 'npu', state=state)

    # An unverifiable implementation (unreadable trusted sources) must never
    # claim an old cache is trustworthy: always re-read.
    monkeypatch.setattr(redact_mod, 'RULES', (first,) + redact_mod.RULES[1:])
    monkeypatch.setattr(pc, '_PACKAGE_ROOT', tmp_path / 'missing-package')
    assert pc._trusted_source_fingerprint() is None
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1
    # reads: one re-read per distrusted checkpoint (source change, tightened
    # policy, unverifiable implementation) — none trusted the old cache.
    assert len(reads) == 3


def test_validate_does_not_leak_descriptors(tmp_path):
    """Eight real validate calls must not grow the process descriptor table
    (mkstemp fds are closed, scratch files are unlinked)."""
    import os
    if not os.path.isdir('/dev/fd'):
        pytest.skip('/dev/fd unavailable')
    doc=make_entry(entry_id='a'*64,domain='vllm-ascend',kind='experience',
                   title='fd case',summary='s',content='Body.')
    repo,sha=publish(tmp_path,{'cases/a.md':render_entry(doc)})
    validate(repo,sha,'vllm-ascend')
    before=len(os.listdir('/dev/fd'))
    for _ in range(8):
        validate(repo,sha,'vllm-ascend')
    assert len(os.listdir('/dev/fd'))<=before


def test_validate_auto_advances_bounded_slices_in_one_call(tmp_path, monkeypatch):
    """One validate() call continues after a controlled blob boundary.

    Not a timer and not a model run. After five successful real blob reads
    on one reader, the next read raises TimeoutError before Git is called.
    The product treats that as the end of the slice, opens a new reader, and
    finishes the tree in the same call. The checkpoint file is the one
    validate() writes.
    """
    import mindie_knowledge.publication_check as pc
    files={}
    for i in range(30):
        doc=make_entry(entry_id=f'{i:04x}'+'0'*60,domain='vllm-ascend',kind='experience',
                       title=f'Sliced case {i}',summary=f'Summary {i}.',
                       content=f'Body of sliced case {i}.')
        files[f'cases/{i:04x}.md']=render_entry(doc)
    repo,sha=publish(tmp_path,files)
    state=tmp_path/'validator-state.json'
    boundary=5
    monkeypatch.setattr(pc,'_CHECKPOINT_EVERY',boundary)
    from mindie_knowledge import gitread
    real_read=gitread.CatFileBatch.read
    real_ctor=pc.CatFileBatch
    reads=[]
    slices=[]
    counts={}
    boundary_sizes=[]
    def bounded_read(self,rev,**kwargs):
        done=counts.get(id(self),0)
        if done>=boundary:
            if not boundary_sizes and state.is_file():
                saved=json.loads(state.read_text(encoding='utf-8'))
                verified=saved.get('verified')
                boundary_sizes.append(len(verified) if isinstance(verified,dict) else -1)
            raise TimeoutError('controlled fixture: blob boundary before this read')
        blob=real_read(self,rev,**kwargs)
        counts[id(self)]=done+1
        reads.append(rev)
        return blob
    class CountingCtor:
        def __new__(cls,*args,**kwargs):
            batch=real_ctor(*args,**kwargs)
            slices.append(batch)
            return batch
    monkeypatch.setattr(gitread.CatFileBatch,'read',bounded_read)
    monkeypatch.setattr(pc,'CatFileBatch',CountingCtor)
    result=pc.validate(repo,sha,'vllm-ascend',state=state)
    assert result['entries']==30
    assert len(slices)>1
    assert len(reads)==30 and len(set(reads))==30
    assert boundary_sizes==[boundary]
    saved=json.loads(state.read_text(encoding='utf-8'))
    assert saved['commit']==sha and len(saved['verified'])==30
    monkeypatch.setattr(gitread.CatFileBatch,'read',real_read)
    monkeypatch.setattr(pc,'CatFileBatch',real_ctor)
    monkeypatch.setattr(pc,'_CHECKPOINT_EVERY',64)
    again_reads=[]
    def tracking(self,rev,**kwargs):
        again_reads.append(rev)
        return real_read(self,rev,**kwargs)
    monkeypatch.setattr(gitread.CatFileBatch,'read',tracking)
    again=pc.validate(repo,sha,'vllm-ascend',state=state)
    assert again['entries']==30 and again_reads==[]
