"""Observe the real serve path without treating an unrelated lock as admission."""

import faulthandler
import json
import os
from pathlib import Path
import sys
import time


def main():
    config_path, consumer_path, evidence_path = map(Path, sys.argv[1:])
    # Running this helper by filename must test its source snapshot even when
    # the interpreter also has an older editable checkout installed.
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    evidence_path.mkdir()
    started = time.monotonic()

    def observed(stage):
        print(json.dumps({"stage": stage, "elapsed": time.monotonic() - started}),
              file=sys.stderr, flush=True)
        (evidence_path / stage).touch()

    observed("imports_started")
    faulthandler.dump_traceback_later(4, repeat=True)
    try:
        from mindie_knowledge.loop import cli, locks

        assert Path(cli.__file__).resolve() == source / "mindie_knowledge/loop/cli.py"
        observed("imports_ready")
        expected = consumer_path.stat()
        real_lock = locks._lock_file_nb
        recorded = set()

        def lock(fd):
            actual = os.fstat(fd)
            consumer = (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
            try:
                result = real_lock(fd)
            except BlockingIOError:
                if consumer and "consumer_busy" not in recorded:
                    recorded.add("consumer_busy")
                    observed("consumer_busy")
                raise
            if consumer and "consumer_acquired" not in recorded:
                recorded.add("consumer_acquired")
                observed("consumer_acquired")
            return result

        locks._lock_file_nb = lock

        def instrument(name):
            original = getattr(cli, name)

            def call(*args, **kwargs):
                observed(name + "_started")
                result = original(*args, **kwargs)
                observed(name + "_ready")
                return result

            setattr(cli, name, call)

        for name in ("Store", "load_transcript_adapter", "Engine", "Service"):
            instrument(name)
        observed("serve_started")
        return cli.main(["serve", "--config", str(config_path)])
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
