"""Background organization and independent judging with a configured agent runner."""

from __future__ import annotations

import json
import queue
import threading
import uuid

from .store import Store, canonical, session_key, text
from .budget import MaintenanceBudget


class Engine:
    def __init__(self, store: Store, *, agent_command, auto_publish=False):
        if (
            not isinstance(agent_command, list)
            or not agent_command
            or not all(isinstance(x, str) for x in agent_command)
        ):
            raise ValueError("agent_command must be a nonempty argv list")
        self.store, self.agent_command = store, agent_command
        self.auto_publish = auto_publish
        self.budget = MaintenanceBudget(store)
        self.evaluate_uses = True
        self.session_allowed = lambda session: True
        self.queue = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="mindie-maintenance", daemon=True
        )
        self.errors = []

    def start(self):
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "UPDATE captures SET status='discarded',detail='service restarted before processing completed' WHERE status='queued'"
            )
        self.thread.start()

    def agent(self, role, payload, *, attempt_id, session):
        if not self.session_allowed(session):
            raise ValueError("session is no longer active; maintenance not started")
        raw = canonical(dict(role=role, **payload))
        if len(raw.encode()) > 65536:
            raise ValueError("maintenance input exceeds limit")
        self.budget.reserve(attempt_id, session, role)
        succeeded = False
        try:
            from .process import bounded_run

            output = bounded_run(
                self.agent_command,
                raw,
                timeout=65,
                max_output=131072,
                cancel=self.stop,
            )
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError("agent must return one JSON object")
            if role == "organize":
                if (
                    set(result) != {"entries"}
                    or not isinstance(result["entries"], list)
                    or len(result["entries"]) > 3
                ):
                    raise ValueError("invalid organizer result")
                for entry in result["entries"]:
                    if not isinstance(entry, dict) or set(entry) != {
                        "title",
                        "content",
                    }:
                        raise ValueError("invalid organized experience")
                    text(entry["title"], "title", 240)
                    text(entry["content"], "content", 8192)
            elif (
                set(result) != {"verdict", "reason"}
                or result["verdict"] not in {"helpful", "unhelpful", "unknown"}
                or not isinstance(result["reason"], str)
                or not 0 < len(result["reason"].strip()) <= 2000
            ):
                raise ValueError("invalid judge result")
            succeeded = True
            return result
        finally:
            self.budget.finish(attempt_id, succeeded)

    def capture(self, session_id, turn_id, summary):
        if not self.session_allowed(session_key(session_id)):
            return dict(status="skipped", reason="session is not manually active")
        if not self.store.attached(session_id):
            return dict(status="skipped", reason="session has not selected this domain")
        if self.budget.status()["paused"]:
            return dict(status="discarded", reason="maintenance circuit paused")
        if self.queue.full():
            return dict(status="discarded", reason="maintenance queue full")
        captured = self.store.capture(session_id, turn_id, summary)
        if captured["duplicate"]:
            return captured
        try:
            self.queue.put_nowait((captured["id"], session_id, summary))
        except queue.Full:
            self.store.mark_capture(
                captured["id"], "discarded", "maintenance queue full"
            )
            return dict(status="discarded", reason="maintenance queue full")
        return captured

    def review(self, ident, session, summary):
        candidates = self.store.query(summary[:2000], limit=5)["results"]
        result = self.agent(
            "organize",
            dict(domain=self.store.domain, session_summary=summary, related=candidates),
            attempt_id="organize:" + ident,
            session=session_key(session),
        )
        if (
            set(result) != {"entries"}
            or not isinstance(result["entries"], list)
            or len(result["entries"]) > 3
        ):
            raise ValueError("organizer must return at most three entries")
        refs = []
        for entry in result["entries"]:
            if not isinstance(entry, dict) or set(entry) != {"title", "content"}:
                raise ValueError("invalid organized experience")
            doc = self.store.add(
                kind="experience",
                title=entry["title"],
                content=entry["content"],
                producers=[session_key(session)],
            )
            refs.append(self.store.ref(doc["id"]))
            if self.auto_publish:
                self.store.publish(doc["id"])
        self.store.mark_capture(ident, "organized", canonical(refs))

    def evaluate(self):
        for usage in self.store.pending_uses():
            if not self.session_allowed(usage["session"]):
                continue
            doc = self.store.get(usage["entry_id"])
            if not self.evaluate_uses and self.store.upstream_entry(doc["id"]):
                continue
            if usage["session"] in doc["producers"]:
                continue
            observation = usage["observation"]
            try:
                result = self.agent(
                    "judge",
                    dict(
                        domain=self.store.domain,
                        experience=doc,
                        application=usage["application"],
                        evidence=usage["evidence"],
                        outcome=usage["outcome"],
                    ),
                    # The attempt identity binds the exact observation: a
                    # corrected use is new work, never a retry of a failure.
                    attempt_id=f"judge:{usage['id']}:{observation[:16]}",
                    session=usage["session"],
                )
                if set(result) != {"verdict", "reason"}:
                    raise ValueError("judge must return verdict and reason")
                self.store.judge(
                    usage["id"],
                    judge_id="judge-" + uuid.uuid4().hex,
                    verdict=result["verdict"],
                    reason=result["reason"],
                    observation=observation,
                )
            except ValueError as exc:
                detail = f"{type(exc).__name__}: {exc}"[:1000]
                if "stale judgement" in str(exc):
                    # The evidence changed mid-flight; discard, do not fail-mark.
                    self.errors = (self.errors + [detail])[-20:]
                    continue
                self.store.failed_judge(usage["id"], observation, detail)
                self.errors = (self.errors + [detail])[-20:]
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"[:1000]
                self.store.failed_judge(usage["id"], observation, detail)
                self.errors = (self.errors + [detail])[-20:]

    def run(self):
        while not self.stop.is_set():
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            ident, session, summary = item
            try:
                if ident:
                    self.review(ident, session, summary)
            except Exception as exc:
                # A failure is visible, bounded, and does not block the user's task.
                detail = f"{type(exc).__name__}: {exc}"[:1000]
                self.errors = (self.errors + [detail])[-20:]
                if ident:
                    self.store.mark_capture(ident, "failed", detail)
            finally:
                self.evaluate()
                self.queue.task_done()
        # Shutdown: discard remaining queued work explicitly; nothing replays.
        while True:
            try:
                ident, _, _ = self.queue.get_nowait()
            except queue.Empty:
                break
            if ident:
                self.store.mark_capture(
                    ident, "discarded", "service stopping before processing"
                )
            self.queue.task_done()

    def wake(self):
        try:
            self.queue.put_nowait((None, None, None))
        except queue.Full:
            pass

    def status(self):
        return dict(
            **self.store.status(),
            maintenance_pending=self.queue.unfinished_tasks,
            maintenance_budget=self.budget.status(),
            errors=self.errors,
        )
