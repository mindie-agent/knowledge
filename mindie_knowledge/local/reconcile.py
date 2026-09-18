"""Maintain indexes for mounted Markdown without modifying source files.

Bootstrap shared notes and imported releases occupy separate namespaces.
Only local Markdown records belong to this reconciler; release maintenance
owns imported vectors. Explicit verification checks content and native
vector records, not just the local fingerprint ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from mindie_knowledge.local.backend import backend_for_config
from mindie_knowledge.markdown import (Document, URI_ROOT, MAX_REFERENCE_BYTES, MAX_METADATA_BYTES,
                                     iter_markdown_files, load_document, read_bounded, relative_posix, uri_for)
from mindie_knowledge.local.instance import InstanceLock

INDEX_LAYERS = ("shared", "project", "candidate")
STATE_NAME = "markdown-index.json"


@dataclass
class ReconcileReport:
    ok: bool = True
    upserted: int = 0
    deleted: int = 0
    unchanged: int = 0
    checked: int = 0
    repaired: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return not self.ok or bool(self.errors)

    @property
    def indexed(self) -> int:
        return self.upserted

    @property
    def skipped(self) -> int:
        return self.unchanged


def _state_path(config: Any) -> Path | None:
    root = getattr(config, "state_root", None)
    if root is None:
        return None
    return Path(root) / STATE_NAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_state(config: Any) -> dict[str, Any]:
    path = _state_path(config)
    if path is None or not path.is_file():
        return {"documents": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"documents": {}}
    if not isinstance(loaded, dict):
        return {"documents": {}}
    documents = loaded.get("documents")
    if not isinstance(documents, dict):
        loaded["documents"] = {}
    return loaded


def _save_state(config: Any, state: dict[str, Any]) -> bool:
    path = _state_path(config)
    if path is None:
        return True
    try:
        _atomic_write_json(path, state)
    except OSError:
        return False
    return True


def _state_lock(config: Any):
    path = _state_path(config)
    return InstanceLock(path.with_suffix(".lock")) if path is not None else nullcontext()


def file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under_roots(path: Path, roots: Sequence[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _mount_roots(config: Any, layers: Sequence[str]) -> list[Path]:
    roots: list[Path] = []
    for layer in layers:
        mount = config.mount(layer)
        if not mount.present:
            continue
        roots.extend(Path(item) for item in mount.roots)
    return roots


def _scan_documents(
    config: Any, layers: Sequence[str], report: ReconcileReport,
) -> tuple[dict[str, Document], list[Path]]:
    found: dict[str, Document] = {}
    readable_roots: list[Path] = []
    for layer in layers:
        if layer not in INDEX_LAYERS:
            continue
        mount = config.mount(layer)
        if not mount.present:
            continue
        for root in mount.roots:
            base = Path(root)
            try:
                # A disappeared/unreadable mount is not proof that its notes
                # were deliberately deleted. Keep its previous index records.
                if not base.is_dir():
                    if layer == "candidate" and not base.exists():
                        continue  # a never-created candidate directory is empty
                    raise OSError("mounted source directory is unavailable")
                paths = iter_markdown_files(base)
            except OSError as exc:
                report.ok = False
                report.errors.append(f"{base}: {exc}")
                continue
            readable_roots.append(base)
            for path in paths:
                try:
                    if not _under_roots(path, (base,)):
                        raise ValueError("document resolves outside its mounted source directory")
                    document = load_document(path, layer=layer, root=base, max_bytes=MAX_REFERENCE_BYTES,
                                             max_metadata_bytes=MAX_METADATA_BYTES)
                    canonical = uri_for(layer, relative_posix(path, base))
                    if document.uri != canonical:
                        raise ValueError("document URI is outside its mounted source identity")
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    report.ok = False
                    report.errors.append(f"{path}: {exc}")
                    continue
                found[document.uri] = document
    return found, readable_roots


def remember_document(config: Any, document: Document, *, indexed_bytes: bytes) -> None:
    """Record the indexed snapshot so reconciliation can detect later edits."""

    fingerprint = hashlib.sha256(indexed_bytes).hexdigest()
    with _state_lock(config):
        state = _load_state(config)
        documents = state.setdefault("documents", {})
        documents[document.uri] = {
            "path": str(document.path.resolve()),
            "layer": document.layer,
            "sha256": fingerprint,
            "index_fingerprint": dict(backend_for_config(config).index_fingerprint()),
        }
        _save_state(config, state)


def _owned_record_uri(uri: str, layer: str, path: Path, roots: Sequence[Path]) -> bool:
    """Require the recorded URI to be derivable from a current source root."""

    for root in roots:
        if not _under_roots(path, (root,)):
            continue
        relative = relative_posix(path, root)
        canonical = uri_for(layer, relative)
        if uri == canonical:
            return True
        # Migrate the old local shared namespace only with its recorded local
        # source path. Never sweep the shared parent or a release subtree.
        if layer == "shared" and uri == f"{URI_ROOT}/shared/{relative}":
            return True
    return False


def reconcile_markdown(
    config: Any, layers: Sequence[str] | None = None, *, verify: bool = False,
    changed_uris: Sequence[str] | None = None, deleted_uris: Sequence[str] = (),
) -> ReconcileReport:
    """Apply local changes; optionally verify and repair every stored document.

    The ledger is an optimization, not evidence that vectors still exist.
    Verification repairs missing content/records even when the ledger is
    intact. A changed engine/model fingerprint rebuilds affected documents.
    """

    wanted = [name for name in (layers or INDEX_LAYERS) if name in INDEX_LAYERS]
    report = ReconcileReport()
    if not wanted:
        return report
    backend = backend_for_config(config)
    ok, detail = backend.available()
    if not ok:
        report.ok = False
        report.errors.append(detail)
        return report

    with _state_lock(config):
        return _reconcile(config, wanted, backend, report, verify=verify,
                          changed_uris=None if verify else changed_uris, deleted_uris=deleted_uris)


def _reconcile(
    config: Any, wanted: Sequence[str], backend: Any, report: ReconcileReport, *, verify: bool,
    changed_uris: Sequence[str] | None = None, deleted_uris: Sequence[str] = (),
) -> ReconcileReport:
    if changed_uris is None:
        current, readable_roots = _scan_documents(config, wanted, report)
    else:
        # The maintenance owner supplies a continuous catalog delta only after
        # matching its last successful vector snapshot and ledger identity.
        from mindie_knowledge.catalog import get_catalog_documents

        current = get_catalog_documents(config, changed_uris, layers=wanted)
        missing = set(changed_uris) - current.keys()
        if missing:
            report.ok = False
            report.errors.append(f"catalog delta has {len(missing)} unavailable documents; full reconciliation required")
        readable_roots = [root for root in _mount_roots(config, wanted) if root.is_dir()]
        for uri, document in list(current.items()):
            roots = config.mount(document.layer).roots
            if not _owned_record_uri(uri, document.layer, document.path, roots):
                report.ok = False
                report.errors.append(f"{uri}: catalog path is outside its mounted source identity")
                current.pop(uri)
    state = _load_state(config)
    recorded: dict[str, Any] = state.setdefault("documents", {})
    fingerprint_contract = dict(backend.index_fingerprint())
    completed: set[str] = set()
    current_by_path = {str(document.path.resolve()): document for document in current.values()}

    for uri, document in current.items():
        try:
            raw = read_bounded(document.path, MAX_REFERENCE_BYTES)
            fingerprint = hashlib.sha256(raw).hexdigest()
            content = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            report.ok = False
            report.errors.append(f"{document.path}: {exc}")
            continue
        previous = recorded.get(uri) if isinstance(recorded.get(uri), dict) else None
        resolved = str(document.path.resolve())
        same_file = bool(
            previous
            and previous.get("sha256") == fingerprint
            and previous.get("path") == resolved
        )
        same_index = bool(previous and previous.get("index_fingerprint") == fingerprint_contract)
        try:
            if same_file and same_index:
                if verify:
                    report.checked += 1
                    intact = backend.check_document(uri, content)
                else:
                    intact = True
                if intact:
                    report.unchanged += 1
                    completed.add(uri)
                    continue
            backend.upsert(uri, content, layer=document.layer)
            if verify:
                report.checked += 1
                if not backend.check_document(uri, content):
                    raise RuntimeError("content/vector verification failed after indexing")
        except Exception as exc:  # noqa: BLE001 - Markdown stays; search is incomplete
            report.ok = False
            report.errors.append(f"{uri}: {type(exc).__name__}: {exc}")
            continue
        recorded[uri] = {
            "path": resolved,
            "layer": document.layer,
            "sha256": fingerprint,
            "index_fingerprint": fingerprint_contract,
        }
        report.upserted += 1
        if same_file:
            report.repaired += 1
        completed.add(uri)

    for uri in list(recorded) if changed_uris is None else list(deleted_uris):
        record = recorded.get(uri)
        if not isinstance(record, dict):
            recorded.pop(uri, None)
            continue
        layer = str(record.get("layer") or "")
        if layer not in wanted:
            continue
        if uri in current:
            continue
        path = Path(str(record.get("path") or ""))
        layer_roots = [root for root in readable_roots if root in config.mount(layer).roots]
        if not path.is_absolute() or not _owned_record_uri(uri, layer, path, layer_roots):
            continue
        try:
            if path.exists():
                replacement = current_by_path.get(str(path.resolve()))
                if not (layer == "shared" and replacement and replacement.uri in completed):
                    continue
        except OSError:
            continue
        try:
            backend.delete(uri)
        except Exception as exc:  # noqa: BLE001
            report.ok = False
            report.errors.append(f"delete {uri}: {type(exc).__name__}: {exc}")
            continue
        recorded.pop(uri, None)
        report.deleted += 1

    state["schema"] = 2
    if not _save_state(config, state):
        report.ok = False
        report.errors.append("could not save the local Markdown index ledger; a later cycle will retry")
    return report
