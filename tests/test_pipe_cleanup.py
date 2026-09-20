"""Real child-process output floods must leave no blocked pipe readers."""
import sys,threading,time
import pytest
from mindie_knowledge.loop.process import bounded_run

def test_flood_cleanup_releases_reader_threads():
    before={id(t) for t in threading.enumerate()}
    for _ in range(3):
        with pytest.raises(ValueError,match='output exceeds'):
            bounded_run([sys.executable,'-c','import os,time; os.write(1,b"x"*1048576); time.sleep(10)'],
                        '',timeout=3,max_output=8192)
    leaked=[t for t in threading.enumerate() if id(t) not in before and '_reader' in t.name]
    assert leaked==[]
