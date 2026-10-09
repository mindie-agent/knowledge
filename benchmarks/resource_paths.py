"""Synthetic, model-free audit; product source/configuration remain untouched.

Run each mode in a fresh process. These measurements cover local material,
query and database mechanisms, not scanner/model/network or Windows latency.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import gc
import hashlib
import json
from pathlib import Path
import platform
import tempfile
import threading
import time

try:
    import psutil
except ImportError:
    raise SystemExit("The developer resource benchmark requires psutil; install it in the benchmark environment.") from None

try:
    import resource
except ImportError:  # Windows has process peak_wset instead of getrusage.
    resource = None

from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials.store import MaterialStore
from mindie_knowledge.materials.summarizer import SummaryLedger
from mindie_knowledge.materials.reme_checkpoint import ReMeCheckpoint


PROCESS = psutil.Process()


def identity(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def resources():
    memory = PROCESS.memory_info()
    peak = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource is not None
            else getattr(memory, 'peak_wset', None))
    if resource is not None and platform.system() != 'Darwin':
        peak *= 1024
    return dict(rss=memory.rss, peak_rss=peak,
                threads=PROCESS.num_threads(), python_threads=len(threading.enumerate()),
                fds=PROCESS.num_fds() if hasattr(PROCESS, 'num_fds') else None,
                handles=PROCESS.num_handles() if hasattr(PROCESS, 'num_handles') else None,
                children=len(PROCESS.children(recursive=True)))


def measure(fn, *args, **kwargs):
    wall, cpu = time.perf_counter(), time.process_time()
    result = fn(*args, **kwargs)
    return result, dict(elapsed=time.perf_counter() - wall, cpu=time.process_time() - cpu)


def disk(root):
    sizes = Counter()
    counts = Counter()
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        key = ('blocks' if path.parent.name == 'blocks' else 'manifests' if path.parent.name == 'manifests'
               else 'reme' if '.reme-index' in path.parts else 'sqlite_wal' if path.name.endswith('.sqlite3-wal')
               else 'sqlite_shm' if path.name.endswith('.sqlite3-shm') else 'sqlite_db' if path.name.endswith('.sqlite3')
               else 'pointers' if path.name == 'current.json' else 'other')
        sizes[key] += path.stat().st_size
        counts[key] += 1
    return dict(bytes=dict(sizes), files=dict(counts), total=sum(sizes.values()))


@contextmanager
def observe(root):
    counts = Counter()
    read_bytes, read_text, stat = Path.read_bytes, Path.read_text, Path.stat
    atomic = MaterialStore._atomic
    checkpoint = ReMeCheckpoint.commit
    def key(path):
        return ('blocks' if path.parent.name == 'blocks' else 'manifests' if path.parent.name == 'manifests'
                else 'pointers' if path.name == 'current.json' else 'other')
    def wanted(path):
        return path.is_relative_to(root)
    def rb(path, *args, **kwargs):
        raw = read_bytes(path, *args, **kwargs)
        if wanted(path):
            counts['read_calls_' + key(path)] += 1
            counts['read_bytes_' + key(path)] += len(raw)
        return raw
    def rt(path, *args, **kwargs):
        raw = read_text(path, *args, **kwargs)
        if wanted(path):
            counts['read_calls_' + key(path)] += 1
            counts['read_bytes_' + key(path)] += len(raw.encode('utf-8'))
        return raw
    def st(path, *args, **kwargs):
        if wanted(path):
            counts['stat_' + key(path)] += 1
        return stat(path, *args, **kwargs)
    def aw(store, path, text):
        if wanted(path):
            counts['write_calls_' + key(path)] += 1
            counts['write_bytes_' + key(path)] += len(text.encode('utf-8'))
        return atomic(store, path, text)
    def cp(index, changed, deleted, generation):
        counts['checkpoint_changed_blocks'] += len(changed)
        counts['checkpoint_deleted_blocks'] += len(deleted)
        return checkpoint(index, changed, deleted, generation)
    Path.read_bytes, Path.read_text, Path.stat, MaterialStore._atomic = rb, rt, st, aw
    ReMeCheckpoint.commit = cp
    try:
        yield counts
    finally:
        Path.read_bytes, Path.read_text, Path.stat, MaterialStore._atomic = read_bytes, read_text, stat, atomic
        ReMeCheckpoint.commit = checkpoint


def query_case(root, count):
    store = Store(root, 'audit')
    baseline = resources()
    def seed():
        for i in range(count):
            text = ('capybara ' if i == 0 else '') + ('Public calibration observation with uncertainty.\n' * 90)
            store.create_draft(entry_id=identity(i), kind='experience', title=f'Public task {i}',
                               summary='Calibration evidence', content=text)
    _, seeded = measure(seed)
    result, cold = measure(store.query, 'capybara', limit=5)
    ref = result['results'][0]['ref']
    warm = []
    for query in ('capybara', 'calibration', 'capybara'):
        with observe(root) as counts:
            result, timing = measure(store.query, query, limit=5)
        warm.append(dict(query=query, result_count=len(result['results']), work=dict(counts), **timing))
    with observe(root) as counts:
        _, block_read = measure(store.explain, ref)
    block_read['work'] = dict(counts)
    row = store.materials.read_task(identity(0))
    with store._write_txn():
        updated = store.materials.append_batch(identity(0), [dict(block_id=identity('new-block'),
            text='Independent capybara correction with uncertainty.\n', title='Correction',
            summary='New evidence', source_range={'part': 1})], row['navigation'],
            revision=row['entry']['revision'], promote=False)
        store._bind_material_draft(updated['entry'], generation='audit', owner=identity('owner'))
    with observe(root) as counts:
        _, one_new_block = measure(store.query, 'capybara', limit=5)
    one_new_block['work'] = dict(counts)
    measured = resources()
    storage = disk(root)
    store.close()
    gc.collect()
    closed = resources()
    closed_storage = disk(root)
    reopened, restart = measure(Store, root, 'audit')
    _, first_reopened_query = measure(reopened.query, 'capybara')
    reopened.close()
    return dict(count=count, body_bytes=count*len(('Public calibration observation with uncertainty.\n'*90).encode())+9,
                baseline=baseline, seeded=seeded, cold=cold, warm=warm, block_read=block_read,
                measured=measured, closed=closed, storage=storage, restart=restart,
                first_reopened_query=first_reopened_query, one_new_block=one_new_block,
                closed_storage=closed_storage)


def append_case(root, count):
    store = Store(root, 'audit')
    baseline = resources()
    checkpoints = []
    task, stream, total = identity('task'), identity('stream'), 0
    aggregate = Counter()
    started, cpu = time.perf_counter(), time.process_time()
    for i in range(count):
        text = ('Public observation remains uncertain.\n' * 40) + str(i) + '\n'
        end = total + len(text.encode())
        prepared = dict(batch_id=identity(('batch', i)), redaction_state={},
                        blocks=[dict(block_id=identity(('block', i)), text=text,
                                     source_range=dict(start=total, end=end, part=0), title='', summary='')])
        with observe(root) as work:
            doc, timing = measure(store.commit_material_increment, stream_key=stream, entry_id=task,
                                  prepared=prepared, start=total, end=end, source_identity='synthetic',
                                  authorization=dict(generation='audit', id=identity(i)), owner=identity('owner'),
                                  observed_stream=store.material_stream(stream))
        aggregate.update(work)
        total = end
        if i+1 in {1, 16, 64, count}:
            checkpoints.append(dict(blocks=i+1, body_bytes=total, last_commit=timing, last_work=dict(work),
                                    cumulative_wall=time.perf_counter()-started,
                                    cumulative_cpu=time.process_time()-cpu,
                                    cumulative_work=dict(aggregate), resources=resources(), storage=disk(root),
                                    rows={table:store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                                          for table in ('entries','revisions','known_revisions','material_batches','material_streams')}))
    store.close()
    gc.collect()
    closed = resources()
    closed_storage = disk(root)
    _, restart = measure(lambda: Store(root, 'audit').close())
    return dict(count=count, baseline=baseline, checkpoints=checkpoints, closed=closed,
                closed_storage=closed_storage, restart=restart)


def query_steps(db, sql, args=()):
    callbacks = []
    db.set_progress_handler(lambda: callbacks.append(1) or 0, 1)
    try:
        rows, timing = measure(lambda: db.execute(sql, args).fetchall())
    finally:
        db.set_progress_handler(None, 0)
    return dict(rows=len(rows), vm_steps=len(callbacks), **timing)


def history_case(root, count):
    store = Store(root, 'audit')
    SummaryLedger(store.db)
    with store.db:
        store.db.executemany('INSERT INTO material_batches VALUES(?,?,?,?,?,?,?,?)',
                             ((identity(i), 's', identity(i), '["b"]', 'complete', '', '{}', i) for i in range(count)))
        store.db.executemany('INSERT INTO transcript_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                             ((identity(i), identity(i), 'c', 'd', 'complete', '', i, 0, '{}') for i in range(count)))
        store.db.executemany('INSERT INTO material_summary_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             ((identity(i), identity(i), identity(i), 'body', 'digest', '{}', '["b"]',
                               'complete', None, None, 0, 0) for i in range(count)))
    sql = ("SELECT b.* FROM material_batches b JOIN transcript_tasks t ON t.entry_id=b.entry_id "
           "WHERE b.status IN ('pending','retry-requested') AND t.summary_due<=? ORDER BY b.created,b.batch_id LIMIT 1")
    summary_poll = query_steps(store.db, sql, (time.time(),))
    prior_test_poll = query_steps(store.db, "SELECT * FROM transcript_tasks WHERE summary_status='pending' "
                                           "AND summary_due<=? ORDER BY updated LIMIT 1", (time.time(),))
    admission = query_steps(store.db, "SELECT created,response FROM material_summary_attempts "
                                     "WHERE status!='prepared' AND created>?", (time.time()-3600,))
    result = dict(count=count, summary_poll=summary_poll, existing_test_poll=prior_test_poll,
                  summary_admission=admission, storage=disk(root), resources=resources(),
                  query_plan=[tuple(row) for row in store.db.execute('EXPLAIN QUERY PLAN '+sql, (time.time(),))])
    store.close()
    return result


if __name__ == '__main__':
    args = argparse.ArgumentParser()
    args.add_argument('mode', choices=('query', 'append', 'history'))
    args.add_argument('count', type=int)
    options = args.parse_args()
    with tempfile.TemporaryDirectory(prefix='mindie-resource-audit-') as directory:
        output = globals()[options.mode + '_case'](Path(directory).resolve(), options.count)
        output.update(mode=options.mode, python=platform.python_version(), platform=platform.platform())
        print(json.dumps(output, ensure_ascii=False))
