from mindie_knowledge.loop.store import Store, session_key, canonical, digest


def setup_pair(tmp_path):
    a=Store(tmp_path/'a','vllm-ascend');b=Store(tmp_path/'b','vllm-ascend')
    doc=a.add(kind='experience',title='Owned process cleanup',content='Inspect and stop the owned process group.',producers=[session_key('producer')])
    a.publish(doc['id'])
    use=a.use(ref=doc['id'],session_id='consumer',application='Stopped owned group',evidence='No owned children remained')
    a.capture('consumer','turn1','Owned children exited; unrelated process remains.')
    a.judge(use['use_id'],judge_id='judge',verdict='helpful',reason='Observed useful cleanup')
    b.install_snapshot(a.snapshot())
    return a,b,doc,use


def test_withdraw_one_source_retains_other_membership(tmp_path):
    a,b,doc,_=setup_pair(tmp_path)
    with b._write_txn():
        b._install_distributed_votes(a.snapshot()['feedback'],source='feed:one')
        b._install_distributed_votes([],source='feed:one')
    assert b.weight(doc['id'])['helpful']==1
    snap=a.snapshot();snap['feedback']=[];snap['version']=digest({k:v for k,v in snap.items() if k!='version'})
    b.install_snapshot(snap)
    assert b.weight(doc['id'])['helpful']==0
    a.close();b.close()


def test_local_use_does_not_own_upstream_judgement(tmp_path):
    a,b,doc,_=setup_pair(tmp_path)
    b.use(ref=doc['id'],session_id='consumer',application='Stopped owned group',evidence='No owned children remained')
    b.capture('consumer','turn1','Owned children exited; unrelated process remains.')
    with b._write_txn(): b._install_distributed_votes([],source='upstream')
    assert b.weight(doc['id'])['helpful']==0
    a.close();b.close()


def test_local_correction_survives_later_source_withdrawal(tmp_path):
    a,b,doc,use=setup_pair(tmp_path)
    b.use(ref=doc['id'],session_id='consumer',application='Correct prior observation',evidence='Found surviving child')
    b.capture('consumer','turn2','The earlier process check missed a descendant.')
    b.judge(use['use_id'],judge_id='new-judge',verdict='unhelpful',reason='Corrected evidence')
    with b._write_txn():
        b._install_distributed_votes(a.snapshot()['feedback'],source='upstream')
        b._install_distributed_votes([],source='upstream')
    assert b.weight(doc['id'])['unhelpful']==1
    assert b.weight(doc['id'])['helpful']==0
    a.close();b.close()
