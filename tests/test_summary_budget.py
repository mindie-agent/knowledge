"""Summary workers receive time for native cleanup before their owner expires."""
import json
import sys
import time

import pytest

from mindie_knowledge.loop import process, summary_budget


def test_native_budget_shrinks_after_worker_startup_and_reserves_receipt_time(monkeypatch):
    monkeypatch.setenv(summary_budget.DEADLINE_ENV, "130")
    monkeypatch.setattr(summary_budget.time, "monotonic", lambda: 100.0)
    assert summary_budget.native_timeout() == 25
    monkeypatch.setattr(summary_budget.time, "monotonic", lambda: 110.0)
    assert summary_budget.native_timeout() == 15
    monkeypatch.setattr(summary_budget.time, "monotonic", lambda: 126.0)
    with pytest.raises(TimeoutError, match="before native invocation"):
        summary_budget.native_timeout()


def test_worker_process_receives_budget_and_unrelated_children_do_not(monkeypatch):
    monkeypatch.setenv(summary_budget.DEADLINE_ENV, "1")
    child = [
        sys.executable, "-c",
        "import json,os; from mindie_knowledge.loop import summary_budget as b; "
        "print(json.dumps(dict(deadline=os.environ.get(b.DEADLINE_ENV),"
        "native=b.native_timeout())))",
    ]
    deadline = str(time.monotonic() + 15)
    received = json.loads(process.bounded_run(
        child, "", timeout=15, max_output=1024,
        child_env={summary_budget.DEADLINE_ENV: deadline},
    ))
    assert received["deadline"] == deadline
    assert 0 < received["native"] < 10
    independent = json.loads(process.bounded_run(child, "", timeout=15, max_output=1024))
    assert independent["deadline"] is None
    assert independent["native"] == 300

