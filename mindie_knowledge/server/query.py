"""Query and explain Markdown knowledge through OpenViking.

Retrieval returns references, never applicability decisions. Recorded conditions
remain visible for the reader to assess. An unavailable index is labelled
degraded and does not block independent work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import time
from typing import Any, Iterator, Sequence

from mindie_knowledge import package_version
from mindie_knowledge.local.backend import Hit, backend_for_config
from mindie_knowledge.local.instance import instance_for_config
from mindie_knowledge.local.shared import current_shared
from mindie_knowledge.markdown import Document, MAX_METADATA_BYTES, MAX_REFERENCE_BYTES, iter_markdown_files, layer_from_uri, load_document, normalized_sha256, parse_markdown, relative_posix, uri_for
from mindie_knowledge.retrieval import fuse, lexical_search, source_excerpt
from mindie_knowledge.server.layers import LAYERS, ServiceConfig, shared_source

NO_RESULT_MEANING = (
    "No document matched. That means UNKNOWN, not supported and not absent-therefore-fine. "
    "Treat a missing fact as unexamined. If the index is degraded, this is not a complete search."
)
REFERENCE_NOTE = (
    "All knowledge is reference, not an axiom. Local experience and public "
    "documents are returned together by relevance. "
    "Public review status is not an admission or ranking filter and does not "
    "prove hardware facts."
)

def load_layer_documents(config: ServiceConfig, layers: Sequence[str], *, errors: list[str] | None = None,
                         max_documents: int | None = None, max_bytes: int | None = None,
                         time_budget_ms: float | None = None) -> list[Document]:
    documents: list[Document] = []
    started, read_bytes = time.perf_counter(), 0
    scan_entries, scan_stopped = 0, False
    # Non-Markdown assets and empty directories must consume the cold-query
    # budget too. Keep this shared across every mount in this one request.
    entry_limit = max(128, max_documents * 8) if max_documents is not None else None

    def scan_exhausted() -> bool:
        nonlocal scan_stopped
        if scan_stopped:
            return True
        reason = ""
        if time_budget_ms is not None and (time.perf_counter() - started) * 1000 >= time_budget_ms:
            reason = "time budget"
        elif max_documents is not None and len(documents) >= max_documents:
            reason = "document budget"
        elif entry_limit is not None and scan_entries >= entry_limit:
            reason = "directory-entry budget"
        if reason:
            scan_stopped = True
            if errors is not None:
                errors.append(f"bounded fallback stopped at its {reason}")
        return scan_stopped

    def fallback_paths(base: Path) -> Iterator[Path]:
        nonlocal scan_entries
        pending = [base]
        while pending:
            if scan_exhausted():
                return
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    while not scan_exhausted():
                        try:
                            entry = next(entries)
                        except StopIteration:
                            break
                        scan_entries += 1
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.name.endswith(".md"):
                            yield Path(entry.path)
            except OSError as exc:
                if errors is not None:
                    errors.append(f"{directory}: {exc}")

    for layer in layers:
        mount = config.mount(layer)
        if not mount.present:
            continue
        for root in mount.roots:
            if max_documents is not None and scan_exhausted():
                return documents
            base = Path(root)
            try:
                if not base.is_dir():
                    if layer == "candidate" and not base.exists():
                        continue
                    raise OSError("mounted source directory is unavailable")
                if max_documents is None:
                    paths = iter_markdown_files(base)
                else:
                    paths = fallback_paths(base)
            except OSError as exc:
                if errors is not None:
                    errors.append(f"{base}: {exc}")
                continue
            for path in paths:
                try:
                    if ((max_documents is not None and len(documents) >= max_documents)
                            or (max_bytes is not None and read_bytes + path.stat().st_size > max_bytes)
                            or (time_budget_ms is not None and (time.perf_counter() - started) * 1000 >= time_budget_ms)):
                        if errors is not None:
                            errors.append("bounded fallback stopped at its document, byte or time budget")
                        return documents
                    path.resolve().relative_to(base.resolve())
                    path.with_suffix(".meta.json").resolve().relative_to(base.resolve())
                    document = load_document(path, layer=layer, root=base, max_bytes=MAX_REFERENCE_BYTES,
                                             max_metadata_bytes=MAX_METADATA_BYTES)
                    if document.uri != uri_for(layer, relative_posix(path, base)):
                        raise ValueError("document identity disagrees with mounted source")
                    documents.append(document)
                    read_bytes += path.stat().st_size
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    if errors is not None:
                        errors.append(f"{path}: {exc}")
                    continue
            if scan_stopped:
                return documents
    return documents


def documents_by_uri(config: ServiceConfig, layers: Sequence[str]) -> dict[str, Document]:
    return {document.uri: document for document in load_layer_documents(config, layers)}


@dataclass
class QueryResponse:
    results: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    unavailable: bool = False
    index_detail: str = ""
    notes: list[str] = field(default_factory=list)
    request: dict[str, Any] = field(default_factory=dict)
    layers_available: list[str] = field(default_factory=list)
    layers_absent: dict[str, str] = field(default_factory=dict)
    inspected: int = 0
    incomplete: bool = False
    catalog_snapshot: str | int | None = None
    source_reads: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "version": package_version(),
            "request": self.request,
            "layers_available": list(self.layers_available),
            "layers_absent": dict(self.layers_absent),
            "degraded": self.degraded or self.unavailable,
            "unavailable": self.unavailable,
            "absent_fact_semantics": "unknown",
            "no_result_meaning": NO_RESULT_MEANING,
            "count": len(self.results),
            "score_kind": "rank_fusion_with_lexical_agreement",
            "inspected": self.inspected,
            "incomplete": self.incomplete,
            "catalog_snapshot": self.catalog_snapshot,
            "source_reads": self.source_reads,
            "notes": list(self.notes),
            "results": list(self.results),
        }
        if self.index_detail:
            payload["index_detail"] = self.index_detail
        payload.update(shared_source())
        return payload


def _hit_payload(hit: Hit, document: Document | None, *, text: str = "", max_chars: int = 600) -> dict[str, Any]:
    title = (document.title if document else None) or hit.title
    excerpt = (document.excerpt() if document else None) or hit.excerpt
    layer = (document.layer if document else None) or hit.layer or layer_from_uri(hit.uri) or ""
    payload: dict[str, Any] = {
        "ref": hit.uri,
        "uri": hit.uri,
        "title": title,
        "excerpt": excerpt,
        "layer": layer,
        "role": "reference",
        "score": round(hit.score, 4),
    }
    if document and document.status:
        payload["status"] = document.status
    if document and document.source:
        payload["source"] = dict(document.source)
    if document and document.conditions:
        payload["conditions"] = dict(document.conditions)
    if document:
        payload["path"] = str(document.path)
        payload["slug"] = document.slug
        if document.captured_at:
            payload["captured_at"] = document.captured_at
        if document.retrieval.get("topics"):
            payload["topics"] = document.retrieval["topics"]
        if document.retrieval.get("ignored"):
            payload["enrichment_ignored"] = document.retrieval["ignored"]
    raw = document.raw_text if document else hit.content
    if raw:
        evidence = source_excerpt(raw, text, max_chars=max_chars)
        evidence.update(source="mounted_markdown" if document else "indexed_shared_snapshot")
        payload["excerpt"] = evidence.pop("text")
        payload["evidence"] = evidence
    return payload


def query(
    config: ServiceConfig,
    *,
    text: str,
    layers: Sequence[str] | None = None,
    limit: int = 8,
    selection: dict[str, Any] | None = None,
) -> QueryResponse:
    """Search all mounted Markdown by relevance, retaining recorded context."""

    if not text.strip():
        raise ValueError("text is required")
    if limit < 1:
        raise ValueError("limit must be positive")
    wanted_layers = [name for name in (layers or LAYERS) if name in LAYERS]
    consulted = config.consulted(wanted_layers)
    request = {
        "text": text or "",
        "layers": wanted_layers,
        "limit": int(limit or 8),
    }
    backend = backend_for_config(config)
    ok, detail = backend.ready()
    notes: list[str] = [REFERENCE_NOTE]
    if not ok:
        notes.append("Vector retrieval is unavailable; mounted Markdown is searched lexically. Shared pack results may be missing.")

    from mindie_knowledge.maintenance import maintenance_status

    maintenance = maintenance_status(config)
    pending = not maintenance.get("ready", False)
    if pending:
        notes.append("Index maintenance is pending; results may be incomplete. The service retries in the background.")

    cap = min(max(int(limit or 8), 1), 20)
    fetch = max(cap * 4, 16)
    searched_layers = consulted["layers_available"]
    active = current_shared(instance_for_config(config).state_root) if "shared" in searched_layers else None
    active_prefix = str(active["root_uri"]).rstrip("/") + "/" if active else ""
    try:
        hits = backend.search(text, layers=searched_layers, limit=fetch) if ok and searched_layers else []
    except Exception as exc:
        hits = []
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
        notes.append("Vector search failed; mounted Markdown is searched lexically. Results may be incomplete.")
    from mindie_knowledge.catalog import get_catalog_documents, search_catalog, topic_selection
    clean_text, selected = topic_selection(config, text, selection)
    requested_topics = selected.get("topics") or []
    requested_topics = [requested_topics] if isinstance(requested_topics, str) else requested_topics
    selected_topics = {topic.casefold() for topic in requested_topics if isinstance(topic, str)} if isinstance(requested_topics, list) else set()
    if selected_topics:
        request["selection"] = {"topics": sorted(selected_topics), "mode": selected.get("mode", "prefer")}
    snapshot = search_catalog(config, text, layers=wanted_layers, limit=fetch, selection=selection,
                              shared_current=active or {})
    notes.extend(snapshot.notes)
    source_errors: list[str] = []
    catalog = dict(snapshot.documents)
    if snapshot.available:
        catalog.update(get_catalog_documents(config, [hit.uri for hit in hits if hit.uri not in catalog], layers=wanted_layers))
        lexical = snapshot.hits
    else:
        options = config.catalog_options
        fallback = load_layer_documents(config, wanted_layers, errors=source_errors,
                                        max_documents=min(int(options.get("fallback_documents", 128)), 512),
                                        max_bytes=min(int(options.get("fallback_bytes", 1048576)), 4194304),
                                        time_budget_ms=min(float(options.get("fallback_ms", 100)), 500))
        catalog = {document.uri: document for document in fallback}
        lexical = lexical_search(clean_text, fallback, limit=fetch)
        if selected_topics:
            preferred = lambda doc: bool(selected_topics.intersection(topic.casefold() for topic in doc.retrieval.get("topics", [])))
            if selected.get("mode") == "only":
                lexical = [hit for hit in lexical if preferred(catalog[hit.uri])]
            else:
                lexical.sort(key=lambda hit: (-hit.score * (1.25 if preferred(catalog[hit.uri]) else 1), hit.uri))
    valid_hits: list[Hit] = []
    for hit in hits:
        document = catalog.get(hit.uri)
        # A lost ledger must not make deleted local files reappear as references.
        # Only the current imported pack has its authoritative source off disk.
        local_ref = any(hit.uri.startswith(uri_for(layer, "marker.md").removesuffix("marker.md"))
                        for layer in searched_layers)
        if document is None and not local_ref and not (active_prefix and hit.uri.startswith(active_prefix)):
            continue
        if hit.layer and hit.layer not in searched_layers:
            continue
        if layer_from_uri(hit.uri) == "shared" and document is not None:
            from mindie_knowledge.markdown import SHARED_BOOTSTRAP_URI
            if not hit.uri.startswith(SHARED_BOOTSTRAP_URI + "/") and not (active_prefix and hit.uri.startswith(active_prefix)):
                continue
        if selected.get("mode") == "only" and selected_topics and document is not None:
            if not selected_topics.intersection(
                    str(topic).casefold() for topic in document.retrieval.get("topics", [])):
                continue
        valid_hits.append(hit)
    kept: list[dict[str, Any]] = []
    source_reads = 0
    text_left = min(max(int(config.catalog_options.get("output_chars", 4800)), 600), 12000)
    for hit, methods in fuse(valid_hits, lexical):
        if len(kept) >= cap or text_left <= 0:
            break
        document = catalog.get(hit.uri)
        imported = bool(active_prefix and hit.uri.startswith(active_prefix))
        if document is None and not imported:
            try:
                # A new capture can reach vectors before the next catalog
                # refresh. Resolve that exact URI, never scan the collection.
                source_reads += 1
                document = _read_mounted_uri(config, hit.uri, searched_layers)
                source_errors.append(f"{hit.uri}: original is newer than the catalog snapshot")
            except (OSError, UnicodeError, ValueError) as exc:
                source_errors.append(f"{hit.uri}: {exc}")
                continue
        elif document is not None and (not imported or active.get("prepared_root")):
            try:
                current = _read_current(config, document, shared_current=active)
                source_reads += 1
                changed = normalized_sha256(current.raw_text) != normalized_sha256(document.raw_text)
                enrichment_changed = current.retrieval != document.retrieval
                if changed or enrichment_changed:
                    source_errors.append(f"{document.path}: source changed since the catalog snapshot")
                    if methods == ["lexical"] and not lexical_search(clean_text, [current], limit=1):
                        continue
                document = current
            except (OSError, UnicodeError, ValueError) as exc:
                source_errors.append(f"{hit.uri}: {exc}")
                continue
        if selected.get("mode") == "only" and selected_topics:
            if document is None or not selected_topics.intersection(topic.casefold() for topic in document.retrieval.get("topics", [])):
                continue
        item = _hit_payload(hit, document, text=clean_text, max_chars=min(600, text_left))
        text_left -= len(item.get("excerpt", ""))
        item["retrieval"] = methods
        if hit.uri.startswith(active_prefix) and active_prefix:
            item["source_git_sha"] = active.get("source_git_sha")
        kept.append(item)
    if source_errors:
        notes.append(f"{len(source_errors)} source(s) could not be read or changed; results may be incomplete: {source_errors[0]}")
    return QueryResponse(
        results=kept[:cap],
        degraded=consulted["degraded"] or pending or not ok or bool(source_errors) or snapshot.incomplete,
        unavailable=not ok,
        index_detail=detail,
        notes=notes,
        request=request,
        layers_available=consulted["layers_available"],
        layers_absent=consulted["layers_absent"],
        inspected=len(hits),
        incomplete=snapshot.incomplete or bool(source_errors),
        catalog_snapshot=snapshot.snapshot,
        source_reads=source_reads,
    )


def _read_mounted_uri(config: ServiceConfig, ref: str, layers: Sequence[str]) -> Document:
    for layer in layers:
        prefix = uri_for(layer, "marker.md").removesuffix("marker.md")
        if not ref.startswith(prefix):
            continue
        relative = ref[len(prefix):]
        if any(part in {"", ".", ".."} for part in relative.replace("\\", "/").split("/")) or ":" in relative:
            raise ValueError("reference is not a mounted relative Markdown path")
        for root in config.mount(layer).roots:
            base = Path(root).resolve()
            proposed = base / relative
            proposed.resolve().relative_to(base)
            proposed.with_suffix(".meta.json").resolve().relative_to(base)
            if proposed.is_file():
                current = load_document(proposed, layer=layer, root=base, max_bytes=MAX_REFERENCE_BYTES,
                                        max_metadata_bytes=MAX_METADATA_BYTES)
                if current.uri != ref:
                    raise ValueError("reference identity differs from its mounted original")
                return current
    raise ValueError("reference has no current mounted original")


def _read_current(config: ServiceConfig, document: Document, *, shared_current: dict[str, Any] | None = None) -> Document:
    if shared_current and shared_current.get("prepared_root"):
        prefix = str(shared_current["root_uri"]).rstrip("/") + "/"
        if document.layer == "shared" and document.uri.startswith(prefix):
            base = Path(shared_current["prepared_root"]).resolve()
            relative = document.path.resolve().relative_to(base).as_posix()
            document.path.with_suffix(".meta.json").resolve().relative_to(base)
            current = load_document(document.path, layer="shared", root=base, max_bytes=MAX_REFERENCE_BYTES,
                                    max_metadata_bytes=MAX_METADATA_BYTES)
            current.uri = prefix + relative
            if current.uri != document.uri:
                raise ValueError("prepared reference identity disagrees with the active shared source")
            return current
    for root in config.mount(document.layer).roots:
        base = Path(root).resolve()
        try:
            document.path.resolve().relative_to(base)
            document.path.with_suffix(".meta.json").resolve().relative_to(base)
        except ValueError:
            continue
        current = load_document(document.path, layer=document.layer, root=base, max_bytes=MAX_REFERENCE_BYTES,
                                max_metadata_bytes=MAX_METADATA_BYTES)
        if current.uri != document.uri:
            raise ValueError("document identity disagrees with mounted source")
        return current
    raise ValueError("document is outside the currently mounted roots")


def explain(
    config: ServiceConfig,
    ref: str,
    *,
    layers: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Read one document by URI, slug, or path, including its recorded context."""

    ident = ref.strip()
    wanted_layers = [name for name in (layers or LAYERS) if name in LAYERS]
    consulted = config.consulted(wanted_layers)
    base: dict[str, Any] = {
        "version": package_version(),
        "ref": ident,
        "layers_consulted": consulted["layers_available"],
        "layers_absent": consulted["layers_absent"],
        "degraded": consulted["degraded"],
        "absent_fact_semantics": "unknown",
    }
    base.update(shared_source())
    if not ident:
        base.update(found=False, meaning="ref is required")
        return base
    from mindie_knowledge.catalog import get_catalog_document
    active = current_shared(instance_for_config(config).state_root) if "shared" in consulted["layers_available"] else None
    candidate = get_catalog_document(config, ident, layers=wanted_layers)
    if candidate is not None:
        try:
            candidate = _read_current(config, candidate, shared_current=active)
        except (OSError, ValueError, UnicodeError):
            candidate = None
    # Direct URI lookup avoids a scan for fresh captures not yet in a snapshot.
    if candidate is None:
        for layer in wanted_layers:
            for root in config.mount(layer).roots:
                prefix = uri_for(layer, "marker.md").removesuffix("marker.md")
                relative = ident[len(prefix):] if ident.startswith(prefix) else ident
                proposed = Path(root) / relative
                try:
                    proposed.resolve().relative_to(Path(root).resolve())
                    proposed.with_suffix(".meta.json").resolve().relative_to(Path(root).resolve())
                    if proposed.is_file():
                        candidate = load_document(proposed, layer=layer, root=Path(root), max_bytes=MAX_REFERENCE_BYTES,
                                                  max_metadata_bytes=MAX_METADATA_BYTES)
                        break
                except (OSError, ValueError, UnicodeError):
                    continue
    documents = [candidate] if candidate else load_layer_documents(config, wanted_layers, max_documents=128, max_bytes=1048576, time_budget_ms=100)
    match: Document | None = None
    for document in documents:
        if ident in {
            document.uri,
            document.slug,
            str(document.path),
            document.path.name,
            document.path.stem,
        }:
            match = document
            break
    if match is None and "shared" in consulted["layers_available"] and layer_from_uri(ident) == "shared":
        prefix = str(active["root_uri"]).rstrip("/") + "/" if active else ""
        if prefix and ident.startswith(prefix) and ".." not in ident.split("/"):
            backend = backend_for_config(config)
            try:
                ok, detail = backend.ready()
                if not ok:
                    return {**base, "found": False, "unavailable": True, "degraded": True, "index_detail": detail}
                raw = backend.read(ident)
            except Exception as exc:
                return {**base, "found": False, "unavailable": True, "degraded": True,
                        "index_detail": f"{type(exc).__name__}: {exc}"}
            if raw:
                title, content = parse_markdown(raw)
                return {**base, "found": True, "title": title, "content": content,
                        "uri": ident, "layer": "shared", "role": "reference",
                        "source_git_sha": active.get("source_git_sha"), "notes": [REFERENCE_NOTE]}
    if match is None:
        base.update(
            found=False,
            meaning=(
                "No document with this ref in the consulted layers. That is 'unknown': "
                "the document may exist in a layer that is not mounted."
            ),
        )
        return base
    payload = match.to_dict()
    payload.update(found=True, role="reference")
    payload.setdefault("notes", []).append(REFERENCE_NOTE)
    payload.update(base)
    if match.layer == "shared" and active:
        prefix = str(active["root_uri"]).rstrip("/") + "/"
        if match.uri.startswith(prefix):
            payload["source_git_sha"] = active.get("source_git_sha")
    payload["found"] = True
    return payload


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Query local Markdown knowledge")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--text", default="")
    selection.add_argument("--ref", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--backend", default="")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    from mindie_knowledge.server.layers import load_config

    mapping = {"backend": args.backend} if args.backend else None
    config = load_config(mapping, path=args.config or None)
    if args.ref:
        payload = explain(config, args.ref)
    else:
        if not str(args.text or "").strip():
            print("error: --text is required unless --ref is set", file=sys.stderr)
            return 2
        payload = query(
            config,
            text=args.text,
            limit=args.limit,
        ).to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
