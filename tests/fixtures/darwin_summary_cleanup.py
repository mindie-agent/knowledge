"""Real sandbox probe: the terminal result and its owned scratch close together."""
import json
from pathlib import Path
import sys

if sys.argv[-1] == "--inspect-self":
    import os
    from mindie_knowledge.loop.darwin_process import group_has_writers

    assert group_has_writers(os.getpgrp(), timeout=1)
elif sys.argv[-1] == "--worker":
    from mindie_knowledge.loop.summary_scratch import create_profile

    profile, managed = create_profile(prefix="sandbox-summary-")
    (Path(profile) / "written").write_text("owned result")
    print(json.dumps(dict(status="returned", profile=profile, managed=managed)), flush=True)
else:
    from mindie_knowledge.loop import process

    cleanup, saved = {}, []
    raw = process.bounded_run(
        [sys.executable, "-I", __file__, "--worker"], "{}", timeout=5,
        max_output=8192, cleanup_receipt=cleanup,
        on_result=lambda value: saved.append(json.loads(value)),
    )
    result = json.loads(raw)
    assert saved == [result] and result["managed"] is True
    assert not cleanup, cleanup
    assert not Path(result["profile"]).exists()
    print(json.dumps(dict(status="returned", owned_scratch_removed=True, cleanup=cleanup)))
