"""Filesystem planning only, not model-quality acceptance."""
import json
from mindie_knowledge.loop.history import plan


def test_history_resume_is_explicit_bounded_and_no_publication(tmp_path):
    source=tmp_path/'native.jsonl'
    records=[{'type':'session_meta','payload':{'id':'task'}}]
    for i in range(8):
        records.append({'timestamp':'2026-09-20T01:00:00Z','type':'response_item','payload':{
            'type':'message','role':'user','content':[{'type':'input_text','text':f'case-{i} '+ 'x'*6000}]}})
    source.write_text(''.join(json.dumps(r)+'\n' for r in records))
    output=tmp_path/'plan'
    a=plan(source,'task',output,max_bytes=16000,max_seconds=10)
    assert 0<a['cursor']<source.stat().st_size
    b=plan(source,'task',output,max_seconds=10)
    assert b['status']=='complete' and b['cursor']==source.stat().st_size
    assert not (output/'drafts').exists() and not (output/'outbox').exists()
