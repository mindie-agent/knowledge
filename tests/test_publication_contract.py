"""Cross-repository contract boundaries use exact Git data before effects."""
import hashlib
import json
import pytest

from mindie_knowledge.publication_contract import (
    ContractMismatch, make_contract, parse_contract, read_git_contract, render_contract,
)
from mindie_knowledge.publication_check import validate
from test_publication_check import git, publish, task_files


def test_contract_digest_binds_bytes_and_validator_identity(tmp_path):
    raw = render_contract(make_contract('vllm-ascend', 'a' * 40))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    repo, sha = publish(tmp_path, {'publication-contract.json': raw})
    assert read_git_contract(repo, sha, 'vllm-ascend', expected_sha256=digest)['sha256'] == digest
    with pytest.raises(ContractMismatch, match='selected product'):
        read_git_contract(repo, sha, 'vllm-ascend', expected_sha256='b' * 64)
    with pytest.raises(ContractMismatch, match='domain'):
        read_git_contract(repo, sha, 'npu')
    with pytest.raises(ContractMismatch, match='immutable'):
        read_git_contract(repo, 'HEAD', 'vllm-ascend')
    with pytest.raises(ContractMismatch, match='duplicate'):
        parse_contract(raw.replace('"domain":', '"domain": "npu", "domain":'), 'vllm-ascend')
    declaration = json.loads(raw)
    declaration['validator']['revision'] = 'main'
    with pytest.raises(ContractMismatch, match='exact reviewed'):
        parse_contract(json.dumps(declaration), 'vllm-ascend')


@pytest.mark.parametrize('change', ['missing', 'executable', 'symlink', 'schema'])
def test_missing_or_invalid_contract_cannot_be_valid_empty_content(tmp_path, change):
    repo, sha = publish(tmp_path, {})
    path = repo / 'publication-contract.json'
    if change == 'missing':
        path.unlink()
    elif change == 'executable':
        git(repo, 'update-index', '--chmod=+x', path.name)
    elif change == 'symlink':
        path.unlink()
        path.symlink_to('README.md')
    else:
        path.write_text(path.read_text().replace('mindie-entry/3', 'mindie-entry/2'))
    if change != 'executable':
        git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', change)
    with pytest.raises(ContractMismatch):
        validate(repo, git(repo, 'rev-parse', 'HEAD'), 'vllm-ascend')
    # Reading an earlier exact immutable commit remains valid.
    assert validate(repo, sha, 'vllm-ascend')['entries'] == 0


def test_review_path_distinguishes_valid_development_from_content(tmp_path):
    repo, base = publish(tmp_path, {})
    for name, content in task_files().items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'ordinary contribution')
    head = git(repo, 'rev-parse', 'HEAD')
    checked = validate(repo, head, 'vllm-ascend', base_revision=base)
    assert checked['content_only'] is True and checked['review_mode'] == 'content'
    (repo / 'AGENTS.md').write_text('Changed bot policy.\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'policy development')
    checked = validate(repo, git(repo, 'rev-parse', 'HEAD'), 'vllm-ascend', base_revision=base)
    assert checked['entries'] == 1 and checked['content_only'] is False
    assert checked['development_paths'] == ['AGENTS.md']


def test_feed_contract_change_preserves_current_and_new_requirement_recovers(tmp_path):
    from test_mindie_feed import init_repo, commit_docs, entry_doc
    from mindie_knowledge.loop.feed import Feed
    from mindie_knowledge.loop.store import Store
    repo = tmp_path / 'remote'
    run = init_repo(repo)
    store = Store(tmp_path / 'store', 'vllm-ascend')
    try:
        config = dict(repository='org/knowledge', ref='main', domain='vllm-ascend', url=str(repo))
        original = render_contract(make_contract('vllm-ascend', 'a' * 40))
        old_hash = hashlib.sha256(original.encode()).hexdigest()
        first = commit_docs(run, repo, [entry_doc('1' * 64, 'Original evidence')])
        feed = Feed(store, {**config, 'contract_sha256': old_hash})
        assert feed.sync()['status'] == 'synced'
        changed = render_contract(make_contract('vllm-ascend', 'b' * 40))
        (repo / 'publication-contract.json').write_text(changed)
        second = commit_docs(run, repo, [entry_doc('1' * 64, 'Later evidence')])
        for force in (False, True):
            receipt = feed.sync(force=force)
            assert receipt['status'] == 'invalid' and receipt['error_code'] == 'contract_mismatch'
            assert receipt['retained_commit'] == first and receipt['commit'] == second
        assert store.query('Original evidence')['results']
        updated = Feed(store, {**config, 'contract_sha256': hashlib.sha256(changed.encode()).hexdigest()})
        assert updated.sync()['status'] == 'synced'
        assert store.query('Later evidence')['results']
    finally:
        store.close()


def test_self_consistent_block_rewrite_needs_new_identity_across_commits(tmp_path):
    from mindie_knowledge.materials.store import _parse_manifest, _make_manifest, _sha
    repo, base = publish(tmp_path, task_files())
    index = next((repo / 'tasks').glob('*/index.md'))
    header = _parse_manifest(index.read_text(), 'vllm-ascend')
    block = header['blocks'][0]
    body_path = index.parent / 'blocks' / (block['block_id'] + '.md')
    body_path.write_text(body_path.read_text().replace('Complete public evidence.', 'Changed public evidence.'))
    block['sha256'] = _sha(body_path.read_text())
    _, manifest = _make_manifest(header['entry'], header['blocks'], header['navigation'], header['status'])
    index.write_text(manifest)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'rewrote an immutable block')
    head = git(repo, 'rev-parse', 'HEAD')
    # A single snapshot has valid hashes; the transition must still fail.
    assert validate(repo, head, 'vllm-ascend')['entries'] == 1
    with pytest.raises(ValueError, match='immutable block changed'):
        validate(repo, head, 'vllm-ascend', base_revision=base)
