from pathlib import Path
import subprocess
import pytest
from mindie_knowledge.publication_check import validate
from mindie_knowledge.loop.documents import make_entry,render_entry

def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True,timeout=10).strip()

def publish(tmp_path,files):
    repo=tmp_path/'repo';repo.mkdir()
    git(repo,'init','-q','-b','main')
    git(repo,'config','user.name','check');git(repo,'config','user.email','check@example.invalid')
    for name,text in files.items():
        p=repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8',newline='\n')
    git(repo,'add','.');git(repo,'commit','-qm','publication')
    return repo,git(repo,'rev-parse','HEAD')

def test_empty_repository_metadata_is_valid(tmp_path):
    repo,sha=publish(tmp_path,{'README.md':'# Empty\n','AGENTS.md':'Publication instructions.\n'})
    assert validate(repo,sha,'vllm-ascend')['entries']==0

def test_plain_canonical_data_only(tmp_path):
    doc=make_entry(entry_id='a'*64,domain='vllm-ascend',kind='experience',title='Observed boundary',summary='A bounded observation.',content='Detailed evidence and its limits.',producers=[])
    repo,sha=publish(tmp_path,{'cases/observation.md':render_entry(doc)})
    assert validate(repo,sha,'vllm-ascend')['entries']==1
    git(repo,'update-index','--chmod=+x','cases/observation.md');git(repo,'commit','-qm','executable data')
    with pytest.raises(ValueError,match='plain data blob'):
        validate(repo,git(repo,'rev-parse','HEAD'),'vllm-ascend')

def test_changed_body_without_new_revision_is_rejected(tmp_path):
    doc=make_entry(entry_id='b'*64,domain='vllm-ascend',kind='experience',title='Revision integrity',summary='Digest binds body.',content='Original body.',producers=[])
    repo,sha=publish(tmp_path,{'cases/revision.md':render_entry(doc).replace('Original body.','Changed body.')})
    with pytest.raises(ValueError,match='revision'):
        validate(repo,sha,'vllm-ascend')
