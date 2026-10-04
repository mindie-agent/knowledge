"""Exact-commit package validation from orthogonal long-task publication cases."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import re
import subprocess
import pytest

from mindie_knowledge.publication_check import validate
from mindie_knowledge.loop.documents import make_entry
from mindie_knowledge.materials import MaterialStore


def git(repo, *args, timeout=10):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, timeout=timeout).strip()


def publish(tmp_path, files, *, write_timeout=10):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'check')
    git(repo, 'config', 'user.email', 'check@example.invalid')
    git(repo, 'config', 'core.autocrlf', 'false')
    git(repo, 'config', 'core.eol', 'lf')
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
    git(repo, 'add', '.', timeout=write_timeout)
    git(repo, 'commit', '-qm', 'publication', timeout=write_timeout)
    return repo, git(repo, 'rev-parse', 'HEAD')


def task_files(number=1, *, domain='vllm-ascend', body='Complete public evidence.', kind='experience'):
    entry_id = f'{number:064x}'
    doc = make_entry(entry_id=entry_id, domain=domain, kind=kind,
                     title=f'Inference task {number}', summary=f'Case {number}; evidence remains reference material.',
                     content=body)
    with TemporaryDirectory() as root:
        store = MaterialStore(root, domain)
        saved = store.put_document(doc)
        store.retain_current(entry_id, {'draft': saved['revision']})
        package = store.export_task(entry_id)
    return {f'tasks/{entry_id}/{path}': text for path, text in package['files'].items()}


def test_empty_repository_metadata_is_valid(tmp_path):
    repo, sha = publish(tmp_path, {'README.md': '# Empty\n', 'AGENTS.md': 'Publication instructions.\n'})
    assert validate(repo, sha, 'vllm-ascend')['entries'] == 0


def test_plain_canonical_task_data_only(tmp_path):
    files = task_files()
    repo, sha = publish(tmp_path, files)
    assert validate(repo, sha, 'vllm-ascend')['entries'] == 1
    path = next(path for path in files if '/blocks/' in path)
    git(repo, 'update-index', '--chmod=+x', path)
    git(repo, 'commit', '-qm', 'executable data')
    with pytest.raises(ValueError, match='plain data blob'):
        validate(repo, git(repo, 'rev-parse', 'HEAD'), 'vllm-ascend')


@pytest.mark.parametrize('layout', ['cases', 'topics', 'corpus', 'generations'])
def test_legacy_layout_is_rejected_without_migration(tmp_path, layout):
    repo, sha = publish(tmp_path, {f'{layout}/retired.md': 'Old material is not imported.'})
    with pytest.raises(ValueError, match='Retired knowledge layout'):
        validate(repo, sha, 'vllm-ascend')


def test_unknown_manifest_field_is_rejected(tmp_path):
    files = task_files()
    index = next(path for path in files if path.endswith('/index.md'))
    files[index] = files[index].replace('---\n', '---\nlegacy_body: forbidden\n', 1)
    repo, sha = publish(tmp_path, files)
    with pytest.raises(ValueError, match='canonical|task fields'):
        validate(repo, sha, 'vllm-ascend')


def test_knowledge_body_citation_is_valid(tmp_path):
    files = task_files(kind='knowledge', body='See https://example.com/reference for the applicability limits.')
    repo, sha = publish(tmp_path, files)
    assert validate(repo, sha, 'vllm-ascend')['entries'] == 1


@pytest.mark.parametrize('damage', ['missing', 'extra', 'changed', 'wrong-task'])
def test_whole_same_commit_package_is_required(tmp_path, damage):
    files = task_files()
    block_path = next(path for path in files if '/blocks/' in path)
    if damage == 'missing':
        files.pop(block_path)
    elif damage == 'extra':
        files[block_path.replace(block_path.rsplit('/', 1)[1], 'e' * 64 + '.md')] = files[block_path]
    elif damage == 'changed':
        files[block_path] += 'A correction absent from the manifest.\n'
    else:
        files = {path.replace(f'{1:064x}', f'{2:064x}', 1): text for path, text in files.items()}
    repo, sha = publish(tmp_path, files)
    with pytest.raises(ValueError, match='reference set|identity|hash'):
        validate(repo, sha, 'vllm-ascend')


def test_pending_block_index_cannot_be_published(tmp_path):
    with TemporaryDirectory() as root:
        store = MaterialStore(root, 'vllm-ascend')
        task = store.append_batch('a' * 64, [dict(block_id='b' * 64, text='Unindexed observation.',
                                                 source_range={}, title='', summary='')],
                                  'Pending indexing.', title='Unfinished task')
        package = store.export_task('a' * 64, revision=task['entry']['revision'])
    repo, sha = publish(tmp_path, {f"tasks/{'a' * 64}/{p}": t for p, t in package['files'].items()})
    with pytest.raises(ValueError, match='indexing is incomplete'):
        validate(repo, sha, 'vllm-ascend')


def test_more_than_1024_tasks_validate(tmp_path):
    files = {}
    for i in range(1027):
        files.update(task_files(i, body=f'Full evidence in task {i}.'))
    repo, sha = publish(tmp_path, files, write_timeout=60)
    assert validate(repo, sha, 'vllm-ascend')['entries'] == 1027


def test_checkpoint_resume_skips_reverified_blobs(tmp_path, monkeypatch):
    import mindie_knowledge.publication_check as pc
    from mindie_knowledge import gitread
    files = {}
    for i in range(70):
        files.update(task_files(i, body=f'Evidence {i}.'))
    repo, sha = publish(tmp_path, files)
    state = tmp_path / 'validator-state.json'
    monkeypatch.setattr(pc, '_CHECKPOINT_EVERY', 16)
    real_read, reads, armed = gitread.CatFileBatch.read, [], [True]

    def counting_read(self, rev, **kwargs):
        if armed[0] and len(reads) == 40:
            armed[0] = False
            reads.append(rev)
            raise OSError('transient read failure mid-validation')
        reads.append(rev)
        return real_read(self, rev, **kwargs)

    monkeypatch.setattr(gitread.CatFileBatch, 'read', counting_read)
    with pytest.raises(OSError, match='transient read failure'):
        pc.validate(repo, sha, 'vllm-ascend', state=state)
    assert len(reads) == 41
    assert pc.validate(repo, sha, 'vllm-ascend', state=state)['entries'] == 70
    assert len(reads) == 41 + (140 - 32)
    assert pc.validate(repo, sha, 'vllm-ascend', state=state)['entries'] == 70
    assert len(reads) == 149
    for path, text in task_files(71).items():
        file = repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(text.encode('utf-8'))
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'more')
    assert pc.validate(repo, git(repo, 'rev-parse', 'HEAD'), 'vllm-ascend', state=state)['entries'] == 71
    assert len(reads) == 149 + 142


def test_oversized_blob_rejected_at_platform_envelope(tmp_path, monkeypatch):
    import mindie_knowledge.publication_check as pc
    repo, sha = publish(tmp_path, task_files(body='x' * 4096))
    monkeypatch.setattr(pc, 'MAX_FILE_BYTES', 2048)
    with pytest.raises(ValueError, match='per-file platform envelope'):
        validate(repo, sha, 'vllm-ascend')


def test_same_blob_under_another_path_is_not_cache_skipped(tmp_path):
    feedback = json.dumps({'schema': 'mindie-feedback/1', 'votes': []}, sort_keys=True) + '\n'
    repo, sha = publish(tmp_path, {'feedback/a.json': feedback, f"tasks/{'a' * 64}/index.md": feedback})
    with pytest.raises(ValueError):
        validate(repo, sha, 'vllm-ascend')


def test_checkpoint_bound_to_domain_and_validator_context(tmp_path, monkeypatch):
    from mindie_knowledge import gitread
    repo, sha = publish(tmp_path, task_files(domain='npu'))
    state = tmp_path / 'state.json'
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1
    with pytest.raises(ValueError, match='domain'):
        validate(repo, sha, 'other', state=state)
    real_read, reads = gitread.CatFileBatch.read, []

    def counting(self, rev, **kwargs):
        reads.append(rev)
        return real_read(self, rev, **kwargs)

    monkeypatch.setattr(gitread.CatFileBatch, 'read', counting)
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1
    assert reads == []
    data = json.loads(state.read_text())
    data['context'] = 'older-ruleset'
    state.write_text(json.dumps(data))
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1
    assert len(reads) == 2


def test_checkpoint_binds_installed_sources_and_privacy_scan(tmp_path, monkeypatch):
    import shutil
    import mindie_knowledge
    import mindie_knowledge.publication_check as pc
    import mindie_knowledge.redact as redact_mod
    package_root = Path(mindie_knowledge.__file__).resolve().parent
    copied = tmp_path / 'trusted-copy'
    for relative in pc._TRUSTED_SOURCES:
        target = copied / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(package_root / relative, target)
    repo, sha = publish(tmp_path, task_files(domain='npu', body='Body contains policy-canary-1.'))
    state = tmp_path / 'state.json'
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1
    original = pc._validator_context('npu')
    source = copied / 'redact.py'
    source.write_bytes(source.read_bytes() + b'\n# tighten policy canary rule\n')
    monkeypatch.setattr(pc, '_PACKAGE_ROOT', copied)
    assert pc._validator_context('npu')['sources'] != original['sources']
    first = redact_mod.RULES[0]
    tightened = type(first)(id=first.id, description=first.description, hint=first.hint,
                           pattern=re.compile(r'policy-canary-\d+'))
    monkeypatch.setattr(redact_mod, 'RULES', (tightened,) + redact_mod.RULES[1:])
    with pytest.raises(ValueError, match='privacy scan'):
        validate(repo, sha, 'npu', state=state)
    monkeypatch.setattr(redact_mod, 'RULES', (first,) + redact_mod.RULES[1:])
    monkeypatch.setattr(pc, '_PACKAGE_ROOT', tmp_path / 'missing-package')
    assert pc._trusted_source_fingerprint() is None
    assert validate(repo, sha, 'npu', state=state)['entries'] == 1


def test_validate_does_not_leak_descriptors(tmp_path):
    import os
    if not os.path.isdir('/dev/fd'):
        pytest.skip('/dev/fd unavailable')
    repo, sha = publish(tmp_path, task_files())
    validate(repo, sha, 'vllm-ascend')
    before = len(os.listdir('/dev/fd'))
    for _ in range(8):
        validate(repo, sha, 'vllm-ascend')
    assert len(os.listdir('/dev/fd')) <= before


def test_validate_advances_bounded_slices_in_one_call(tmp_path, monkeypatch):
    import mindie_knowledge.publication_check as pc
    from mindie_knowledge import gitread
    files = {}
    for i in range(30):
        files.update(task_files(i, body=f'Complete task {i}.'))
    repo, sha = publish(tmp_path, files)
    state = tmp_path / 'validator-state.json'
    boundary = 5
    monkeypatch.setattr(pc, '_CHECKPOINT_EVERY', boundary)
    real_read, real_ctor = gitread.CatFileBatch.read, pc.CatFileBatch
    reads, slices, counts = [], [], {}

    def bounded_read(self, rev, **kwargs):
        done = counts.get(id(self), 0)
        if done >= boundary:
            raise TimeoutError('controlled boundary before this read')
        blob = real_read(self, rev, **kwargs)
        counts[id(self)] = done + 1
        reads.append(rev)
        return blob

    class CountingCtor:
        def __new__(cls, *args, **kwargs):
            batch = real_ctor(*args, **kwargs)
            slices.append(batch)
            return batch

    monkeypatch.setattr(gitread.CatFileBatch, 'read', bounded_read)
    monkeypatch.setattr(pc, 'CatFileBatch', CountingCtor)
    assert pc.validate(repo, sha, 'vllm-ascend', state=state)['entries'] == 30
    assert len(slices) > 1
    assert len(reads) == 60 and len(set(reads)) == 60
    saved = json.loads(state.read_text())
    assert saved['commit'] == sha and len(saved['verified']) == 60
    monkeypatch.setattr(gitread.CatFileBatch, 'read', real_read)
    monkeypatch.setattr(pc, 'CatFileBatch', real_ctor)
    assert pc.validate(repo, sha, 'vllm-ascend', state=state)['entries'] == 30
