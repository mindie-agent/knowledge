"""Background organization and independent judging with a configured agent runner."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid

from .store import Store, canonical, session_key


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
        self.evaluate_uses = True
        self.queue = queue.Queue(maxsize=32)
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

    def agent(self, role, payload):
        completed = subprocess.run(
            self.agent_command,
            input=canonical(dict(role=role, **payload)),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"{role} agent exited {completed.returncode}")
        if len(completed.stdout) > 131072:
            raise ValueError("agent output exceeds limit")
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("agent must return one JSON object")
        return result

    def capture(self, session_id, turn_id, summary):
        if not self.store.attached(session_id):
            return dict(status="skipped", reason="session has not selected this domain")
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
            doc = self.store.get(usage["entry_id"])
            if not self.evaluate_uses and self.store.upstream_entry(doc["id"]):
                continue
            if usage["session"] in doc["producers"]:
                continue
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
                )
                if set(result) != {"verdict", "reason"}:
                    raise ValueError("judge must return verdict and reason")
                self.store.judge(
                    usage["id"],
                    judge_id="judge-" + uuid.uuid4().hex,
                    verdict=result["verdict"],
                    reason=result["reason"],
                )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"[:1000]
                self.store.failed_judge(usage["id"], detail)
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

    def wake(self):
        try:
            self.queue.put_nowait((None, None, None))
        except queue.Full:
            pass

    def status(self):
        return dict(
            **self.store.status(),
            maintenance_pending=self.queue.unfinished_tasks,
            errors=self.errors,
        )
