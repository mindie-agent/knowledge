"""Service-owned live operations; durable business stages stay in SQLite.

Each admitted operation owns an irreversible cancellation token. Revocation
targets contribution work by authorization generation; reads and reconciliation
are independent. The same lock admits work and freezes an idle service, so an
updater cannot switch generations while a new operation is being admitted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading


CONTRIBUTION = frozenset({"capture", "index", "package", "publish"})


@dataclass(eq=False)
class Operation:
    kind: str
    identity: str | None
    generation: str | None
    cancel: threading.Event = field(default_factory=threading.Event)


class OperationOwner:
    def __init__(self, stop):
        self.stop = stop
        self._lock = threading.Lock()
        self._active = set()
        self._frozen = False

    def begin(self, kind, identity=None, generation=None):
        with self._lock:
            if self._frozen or self.stop.is_set():
                return None
            operation = Operation(kind, identity, generation)
            self._active.add(operation)
            return operation

    def end(self, operation):
        with self._lock:
            if operation not in self._active:
                raise RuntimeError("operation is not owned by this service")
            self._active.remove(operation)

    @contextmanager
    def scope(self, kind, identity=None, generation=None):
        operation = self.begin(kind, identity, generation)
        try:
            yield operation
        finally:
            if operation is not None:
                self.end(operation)

    def revoke(self, *, keep_generation=None):
        with self._lock:
            for operation in self._active:
                if (operation.kind in CONTRIBUTION and
                        (keep_generation is None or operation.generation != keep_generation)):
                    operation.cancel.set()

    def has_foreign_contribution(self, generation):
        with self._lock:
            return any(operation.kind in CONTRIBUTION and not operation.cancel.is_set()
                       and (generation is None or operation.generation != generation)
                       for operation in self._active)

    def cancel(self, kind=None):
        """Owner cancellation, including connection loss and service shutdown."""
        with self._lock:
            for operation in self._active:
                if kind is None or operation.kind == kind:
                    operation.cancel.set()

    def shutdown(self):
        with self._lock:
            self.stop.set()
            self._frozen = True
            for operation in self._active:
                operation.cancel.set()

    def freeze(self):
        with self._lock:
            self._frozen = True
            return len(self._active)

    def resume(self):
        with self._lock:
            if not self.stop.is_set():
                self._frozen = False

    def snapshot(self):
        with self._lock:
            return dict(activity=len(self._active), frozen=self._frozen,
                        stopping=self.stop.is_set())

    def frozen(self):
        with self._lock:
            return self._frozen or self.stop.is_set()
