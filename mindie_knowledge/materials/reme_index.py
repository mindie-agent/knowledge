"""[Role] Embedded ReMe derived chunking, file graph, and BM25 lexical index over canonical Markdown materials.

[Invariants]
- Markdown packages (`tasks/<id>/index.md` + `blocks/<hash>.md`) are the sole body
  authority; `.reme-index/` is a disposable derived index bound to `catalog.generation`.
- One unreadable task is omitted from ranking and named as a fault. It does not
  discard other tasks' derived rows or commit an empty generation over them.
- `TechnicalTokenizerV1` preserves domain API/version/CJK token boundaries;
  chunk hashes and graph-to-BM25 document ID sets are verified on checkpoint load.

[Timeouts & Locks]
- Index refresh and checkpoint writes are serialized under `StartLock(.reme-index/.lock, wait=5.0)`.

[Side-Effect Policy]
- In-process `asyncio.Runner` with `thread_pool_max_workers=0`; zero background
  daemons, watchers, network I/O, or LLM calls.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_left
import json
import logging
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from reme.components import ApplicationContext
from reme.components.file_chunker import MarkdownFileChunker
from reme.components.file_graph import LocalFileGraph
from reme.components.file_store import LocalFileStore
from reme.components.keyword_index import BM25Index
from reme.components.tokenizer import RegexTokenizer
from reme.schema import FileNode, FileChunk

from ..loop.locks import StartLock
from ..retrieval import query_tokens, tokens
from .store import _canonical, _digest, _parse_block
from .provenance import POLICY, body_tokens, citations, group_matches, page_groups
from .references import (
    MaterialHits, block_ref, feedback_ref, material_fault, parse_read_ref, task_ref,
)
from .catalog import signature, CurrentVisibility
from .reme_checkpoint import ReMeCheckpoint


class StrictFileGraph(LocalFileGraph):
    def __init__(self, *, records, **kwargs):
        self.records = records
        super().__init__(**kwargs)

    async def load(self):
        for path, value in self.records.items():
            self._nodes[path] = FileNode.model_validate(value["node"])
        self.records = None

    async def dump(self):
        # ReMeIndex commits graph and chunks together, before returning.
        return None


class TechnicalTokenizerV1(RegexTokenizer):
    """Reuse established API/version/CJK token boundaries inside ReMe BM25.

    Bump this class suffix when token semantics change: ReMe includes the
    class name in its persisted tokenizer fingerprint.
    """

    def _tokenize_one(self, text, **_kwargs):
        return tokens(text)


class StrictBM25Index(BM25Index):
    def __init__(self, *, records, **kwargs):
        self.records = records
        super().__init__(**kwargs)

    async def load(self):
        ids, lengths, token_sets, postings = [], [], [], {}
        for value in self.records.values():
            for chunk in value["chunks"]:
                ident = chunk["value"]["id"]
                frequencies = chunk["frequencies"]
                if not isinstance(frequencies, dict) or any(
                    not isinstance(token, str) or type(count) is not int or count < 1
                    for token, count in frequencies.items()
                ):
                    raise ValueError("invalid ReMe term-frequency checkpoint")
                if not frequencies:
                    continue
                if ident in self._doc_id_to_idx:
                    raise ValueError("duplicate ReMe lexical document identity")
                index = len(ids)
                tids = self._tokens_to_ids(list(frequencies))
                ids.append(ident)
                self._doc_id_to_idx[ident] = index
                lengths.append(sum(frequencies.values()))
                token_sets.append(np.asarray(tids, dtype=np.int32))
                for tid, count in zip(tids, frequencies.values()):
                    postings.setdefault(tid, []).append((index, count))
        self._append_doc_arrays(ids, lengths, token_sets)
        self._extend_postings(postings)
        self.records = None

    async def dump(self):
        return None  # The owning block checkpoint contains exact term counts.


class StrictFileStore(LocalFileStore):
    def __init__(self, *, records, **kwargs):
        self.records = records
        super().__init__(**kwargs)

    async def load(self):
        for value in self.records.values():
            for record in value["chunks"]:
                chunk = FileChunk.model_validate(record["value"])
                verified = chunk.model_copy()
                verified.set_hash_id()
                if verified.id != chunk.id:
                    raise ValueError("ReMe derived chunk text differs from its identity")
                if chunk.id in self.file_chunks:
                    raise ValueError("duplicate ReMe chunk identity")
                self.file_chunks[chunk.id] = chunk
        self.records = None
        graph_ids = {ident for node in await self.file_graph.get_nodes() for ident in node.chunk_ids}
        if graph_ids != set(self.file_chunks):
            raise ValueError("ReMe derived graph/chunk checkpoint is inconsistent; rebuild the index")
        live = set(self.keyword_index.document_ids)
        if live - set(self.file_chunks) or any(
            self.keyword_index.is_indexable(self.file_chunks[ident].text)
            for ident in set(self.file_chunks) - live
        ):
            raise ValueError("ReMe graph and lexical checkpoint differ; rebuild the index")

    async def dump(self):
        return None  # Persisted atomically by ReMeIndex, never at reader close.


class VerifiedMarkdownFileChunker(MarkdownFileChunker):
    """Reuse the exact byte-verified source in ReMe's unchanged chunker."""
    verified = None

    async def _read_text_for_indexing(self, path):
        if self.verified is None or self.verified[0] != path:
            raise ValueError("ReMe chunking requires the verified current material bytes")
        return self.verified[1]


def _block_path(task_id, block_id):
    return f"tasks/{task_id}/blocks/{block_id}.md"


def _annotate_material_failure(exc, faults):
    exc.mindie_material_faults = list(faults)
    return exc


def _retarget_unreadable_cites(groups, fault_ids, domain):
    """A present task that could not be read is not an absent citation."""
    def visit(items):
        for item in items:
            for citation in item.get("cites") or ():
                if (citation.get("source_domain") == domain
                        and citation.get("source_task_id") in fault_ids):
                    citation["citation_status"] = "unreadable"
            visit(item.get("related") or ())
    visit(groups)


class ReMeIndex:
    def __init__(self, root, domain):
        self.root = Path(root)
        self.domain = domain
        self.runner = asyncio.Runner()
        self.components = []
        self.store = None
        self.chunker = None
        self.stamp = self.root / ".reme-index" / "snapshot.json"
        self.bindings = {}
        self.file_metadata = {}
        self.task_paths = {}
        self._generation = None
        self.checkpoint = None
        self._eligible_cache = {}
        self._chunk_body_terms = {}
        self._body_term_pool = {}
        self._read_faults = []
        self._read_causes = []
        self._unbound_ids = set()
        self._unbound_scan = False

    def clear_query_terms(self):
        self._chunk_body_terms.clear()
        self._body_term_pool.clear()

    def _compact_term_pool(self):
        self._body_term_pool = {
            term: term for _, terms in self._chunk_body_terms.values() for term in terms
        }

    def _prune_query_terms(self):
        live = self.store.file_chunks
        retained = {
            ident: value for ident, value in self._chunk_body_terms.items()
            if ident in live and value[0] == live[ident].metadata.get("mindie_body_offset")
        }
        if len(retained) != len(self._chunk_body_terms):
            self._chunk_body_terms = retained
            self._compact_term_pool()

    async def _start(self, records):
        # Default ReMe config is deliberately not loaded: no session copies,
        # automatic evolution, watchers, HTTP/MCP service, jobs or model calls.
        context = ApplicationContext(workspace_dir=str(self.root), metadata_dir=".reme-index",
                                     session_dir="", mem_session_dir="", resource_dir="",
                                     daily_dir="", digest_dir="", thread_pool_max_workers=0)
        tokenizer = TechnicalTokenizerV1(name="material", app_context=context, filter_stopwords=False)
        keyword = StrictBM25Index(records=records, name="material", tokenizer="material", app_context=context)
        graph = StrictFileGraph(records=records, name="material", app_context=context)
        self.store = StrictFileStore(records=records, name="material", embedding_store="", keyword_index="material",
                                    file_graph="material", tag_index="", app_context=context)
        self.chunker = VerifiedMarkdownFileChunker(name="material", chunk_byte_size=8192, embed_toc=True,
                                           invalid_encoding_policy="strict", app_context=context)
        context.components = {"tokenizer": {"material": tokenizer}, "keyword_index": {"material": keyword},
                              "file_graph": {"material": graph}, "file_store": {"material": self.store},
                              "file_chunker": {"material": self.chunker}}
        self.components = [tokenizer, keyword, graph, self.store, self.chunker]
        for component in self.components:
            component.logger = logging.getLogger("mindie_knowledge.materials.reme")
        started = []
        try:
            for component in self.components:
                await component.start()
                started.append(component)
        except BaseException as exc:
            for component in reversed(started):
                try:
                    await component.close()
                except BaseException:
                    pass  # preserve the original required-start failure
            self.components = []
            raise

    async def _close(self):
        first_error = None
        for component in reversed(self.components):
            try:
                await component.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self.components = []
        self.store = None
        self.clear_query_terms()
        if first_error is not None:
            raise first_error

    def _load(self):
        if self.checkpoint is None:
            self.checkpoint = ReMeCheckpoint(self.root / ".reme-index")
        generation = self.checkpoint.generation
        if self.store is None or generation != self._generation:
            if self.store is not None:
                self.runner.run(self._close())
            records = dict(self.checkpoint.records())
            self.runner.run(self._start(records))
            self.bindings = {path: value["binding"] for path, value in records.items()}
            self.file_metadata = {path: value["metadata"] for path, value in records.items()}
            self.task_paths = {}
            for path in records:
                self.task_paths.setdefault(path.split("/")[1], set()).add(path)
            self._generation = generation
            self._eligible_cache.clear()
            self._unbound_ids.clear()
            self._unbound_scan = True

    def _refresh(self, tasks, catalog):
        self._load()
        generation = catalog.generation
        self._read_faults = []
        self._read_causes = []
        if self._generation > generation:
            raise ValueError("ReMe checkpoint is newer than the current material catalog")
        if self._generation != generation:
            try:
                self._apply_changes(tasks, catalog, generation)
            except BaseException as exc:
                self._rollback_index(exc)
                raise
        # A warm query must not walk every current task. Scan once after load,
        # then retry only ids already known to be missing from the checkpoint.
        if self._unbound_scan:
            self._collect_unbound(tasks)
            self._unbound_scan = False
        try:
            self._recover_unbound(tasks, getattr(catalog, "_row_faults", {}))
        except BaseException as exc:
            # A commit that did finish stays in the checkpoint; memory reloads it.
            self._rollback_index(exc)
            raise
        self._fail_if_every_task_unreadable(tasks)

    def _rollback_index(self, exc):
        # Chunking, deletion and upsert mutate memory before persistence.
        # Restore the committed checkpoint on the next attempt even when
        # failure happened before the checkpoint transaction began.
        try:
            self.runner.run(self._close())
        except BaseException as cleanup:
            exc.add_note(f"ReMe cleanup also failed: {type(cleanup).__name__}: {cleanup}")
        self._generation = None

    def _fail_if_every_task_unreadable(self, tasks):
        failed = {fault["task_id"] for fault in self._read_faults}
        if tasks and failed >= set(tasks):
            raise _annotate_material_failure(self._read_causes[0], self._read_faults)

    def _stage_block(self, rel, block):
        # Validate authoritative bytes before ReMe's derived text
        # normalization; universal-newline reads would hide corruption.
        text = (self.root / rel).read_bytes().decode("utf-8")
        body = _parse_block(text, block)
        direct_tokens = body_tokens(body)
        cites = citations(body)
        self.chunker.verified = (self.root / rel, text)
        try:
            node, chunks = self.runner.run(self.chunker.chunk(self.root / rel))
        finally:
            self.chunker.verified = None
        # Fallible block headers travel with the package. Consumers reuse
        # them locally and never pay another model to summarize the body.
        for chunk in chunks:
            prefix = block["title"] + "\n" + block["summary"] + "\n"
            chunk.metadata.update(mindie_policy=POLICY, mindie_body_offset=len(prefix))
            chunk.text = prefix + chunk.text
            chunk.set_hash_id()
        node.chunk_ids = [chunk.id for chunk in chunks]
        record = dict(
            binding=dict(descriptor=_digest(block), signature=signature(self.root / rel)),
            metadata=dict(body_tokens=direct_tokens, cites=cites),
            node=node.model_dump(mode="json"),
            chunks=[dict(value=chunk.model_dump(mode="json"),
                         frequencies=dict(Counter(self.store.keyword_index._tokenize(chunk.text))))
                    for chunk in chunks],
        )
        return (node, chunks), record

    def _remember_fault(self, task_id, exc, block_id):
        # Remember the id even when this search already recorded the fault,
        # so the next search can retry a repaired package without a full walk.
        self._unbound_ids.add(task_id)
        if any(fault["task_id"] == task_id for fault in self._read_faults):
            return
        self._read_faults.append(material_fault(self.domain, task_id, exc, block_id=block_id))
        self._read_causes.append(exc)

    def _commit_index(self, changed, deleted, generation):
        if deleted:
            self.runner.run(self.store.delete(list(deleted)))
        upserts = [value["upsert"] for value in changed.values()]
        # ReMe's native batch API extends each term posting once. A call per
        # block repeatedly copies the growing arrays. These are references to
        # the chunks the in-memory store must retain, not a second body corpus.
        if upserts:
            self.runner.run(self.store.upsert(upserts))
        stored = {path: value["record"] for path, value in changed.items()}
        self.checkpoint.commit(stored, deleted, generation)
        for path in deleted:
            self.bindings.pop(path, None)
            self.file_metadata.pop(path, None)
        for path, value in stored.items():
            self.bindings[path] = value["binding"]
            self.file_metadata[path] = value["metadata"]
        keyword = self.store.keyword_index
        if len(keyword._doc_ids) > max(64, 2 * keyword.n_docs):
            self.runner.run(keyword.optimize_index())
        self._generation = generation
        self._eligible_cache.clear()
        self._prune_query_terms()

    def _apply_changes(self, tasks, catalog, generation):
        desired, deleted = {}, set()
        for change in catalog.changes(self._generation):
            task_id = change["task_id"]
            task = tasks.get(task_id)
            paths = set()
            for block in task["blocks"] if task else ():
                rel = _block_path(task_id, block["block_id"])
                paths.add(rel)
                if self.bindings.get(rel, {}).get("descriptor") != _digest(block):
                    desired[rel] = block
            deleted.update(self.task_paths.get(task_id, set()) - paths)
            if paths:
                self.task_paths[task_id] = paths
            else:
                self.task_paths.pop(task_id, None)
        by_task = {}
        for rel, block in desired.items():
            by_task.setdefault(rel.split("/")[1], []).append((rel, block))
        changed, failed = {}, {}
        for task_id, blocks in by_task.items():
            prepared, block_id = [], None
            try:
                for rel, block in blocks:
                    block_id = block["block_id"]
                    upsert, record = self._stage_block(rel, block)
                    prepared.append((rel, upsert, record))
            except TimeoutError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                failed[task_id] = (block_id, exc)
                self._remember_fault(task_id, exc, block_id)
                continue
            self._unbound_ids.discard(task_id)
            for rel, upsert, record in prepared:
                changed[rel] = dict(upsert=upsert, record=record)
        for task_id in failed:
            deleted.update(self.task_paths.get(task_id, ()))
            for block in tasks.get(task_id, {}).get("blocks", ()):
                deleted.add(_block_path(task_id, block["block_id"]))
        if failed and set(tasks) <= set(failed):
            # Every current task failed. Keep the last committed checkpoint.
            raise _annotate_material_failure(self._read_causes[0], self._read_faults)
        self._commit_index(changed, deleted, generation)

    def _collect_unbound(self, tasks):
        for ident, task in tasks.items():
            if any(self.bindings.get(_block_path(ident, block["block_id"]), {}).get("descriptor")
                   != _digest(block) for block in task["blocks"]):
                self._unbound_ids.add(ident)

    def _recover_unbound(self, tasks, row_faults):
        """Retry ids left out of the checkpoint. Do not walk every current task."""
        skip = {fault["task_id"] for fault in self._read_faults}
        changed, deleted = {}, set()
        for ident in list(self._unbound_ids):
            if ident in skip:
                continue
            task = tasks.get(ident)
            if task is None:
                if ident in row_faults:
                    continue
                deleted.update(self.task_paths.get(ident, ()))
                self._unbound_ids.discard(ident)
                continue
            pending = [block for block in task["blocks"]
                       if self.bindings.get(_block_path(ident, block["block_id"]), {}).get("descriptor")
                       != _digest(block)]
            if not pending:
                self._unbound_ids.discard(ident)
                continue
            prepared, block_id = [], None
            try:
                for block in pending:
                    block_id = block["block_id"]
                    rel = _block_path(ident, block["block_id"])
                    upsert, record = self._stage_block(rel, block)
                    prepared.append((rel, upsert, record))
            except TimeoutError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._remember_fault(ident, exc, block_id)
                for block in task["blocks"]:
                    deleted.add(_block_path(ident, block["block_id"]))
                continue
            self._unbound_ids.discard(ident)
            for rel, upsert, record in prepared:
                changed[rel] = dict(upsert=upsert, record=record)
        if changed or deleted:
            self._commit_index(changed, deleted, self._generation)

    def verify_path(self, path, descriptor):
        binding = self.bindings.get(path)
        if binding is None or binding["descriptor"] != _digest(descriptor):
            raise ValueError("current block differs from the ReMe checkpoint")
        current = signature(self.root / path)
        if current is None:
            raise FileNotFoundError("required current material block is missing")
        if current != binding["signature"]:
            _parse_block((self.root / path).read_bytes().decode("utf-8"), descriptor)
            binding["signature"] = current

    @staticmethod
    def _excerpt(body, query_terms):
        # Keep source wording, but choose a window around an actual query term
        # instead of returning the prepended fallible index metadata.
        lower = body.casefold()
        positions = [lower.find(term) for term in query_terms if term in lower]
        start = max(0, min(positions) - 120) if positions else 0
        return ("…" if start else "") + body[start:start + 1200]

    def _exact_paths(self, tasks, eligible, query):
        if re.fullmatch(r"[0-9a-f]{64}", query):
            parsed = dict(kind="task", domain=self.domain, task_id=query)
        else:
            try:
                parsed = parse_read_ref(query)
            except ValueError:
                return None
        if parsed["domain"] != self.domain or parsed["task_id"] not in tasks:
            return []
        task_id = parsed["task_id"]
        paths = []
        for descriptor in tasks[task_id]["blocks"]:
            path = f"tasks/{task_id}/blocks/{descriptor['block_id']}.md"
            if path not in eligible:
                continue
            if parsed["kind"] == "block" and (
                descriptor["block_id"] != parsed["block_id"] or descriptor["sha256"] != parsed["sha256"]
            ):
                continue
            paths.append(path)
        return paths

    def _select_eligible(self, tasks, allowed_ids, conditions):
        eligible = {}
        faulted = {fault["task_id"] for fault in self._read_faults}
        for ident, task in tasks.items():
            if ident in faulted:
                continue
            entry = task["entry"]
            if allowed_ids is not None and ident not in allowed_ids:
                continue
            if isinstance(allowed_ids, Mapping) and allowed_ids[ident] != entry["revision"]:
                self._remember_fault(ident, ValueError("metadata and current material revision differ"), None)
                continue
            if entry["kind"] == "knowledge" and any(
                key in entry["conditions"] and entry["conditions"][key] != str(value)
                for key, value in (conditions or {}).items()
            ):
                continue
            paths = []
            missing = None
            for block in task["blocks"]:
                path = _block_path(ident, block["block_id"])
                if path not in self.store.file_graph._nodes:
                    missing = block
                    break
                paths.append((path, block))
            if missing is not None:
                self._remember_fault(
                    ident, ValueError("current block is not in the derived index"), missing["block_id"])
                continue
            for path, block in paths:
                eligible[path] = (ident, task, block)
        self._fail_if_every_task_unreadable(tasks)
        faulted = {fault["task_id"] for fault in self._read_faults}
        if faulted:
            eligible = {path: item for path, item in eligible.items() if item[0] not in faulted}
        chunk_ids = {cid for path in eligible for cid in self.store.file_graph._nodes[path].chunk_ids
                     if cid in self.store.keyword_index.document_ids}
        return eligible, chunk_ids, _digest(sorted(eligible))

    def search(self, tasks, query, limit, conditions, allowed_ids, continuation=None, *, catalog):
        lock = StartLock(self.root / ".index.lock", wait=5.0)
        lock.acquire(wait=5.0)
        try:
            self._refresh(tasks, catalog)
            # A damaged header is not an unpromoted pointer. Unpromoted metadata
            # still fails the whole query, with no per-task fault attached.
            row_faults = getattr(catalog, "_row_faults", {})
            if allowed_ids is not None and set(allowed_ids) - set(tasks) - set(row_faults):
                raise ValueError("metadata references material pointers not yet promoted")
            for ident, exc in row_faults.items():
                self._remember_fault(ident, exc, None)
            cacheable = not self._read_faults and (
                allowed_ids is None or isinstance(allowed_ids, CurrentVisibility))
            cache_key = (self._generation, _canonical(conditions or {}), id(allowed_ids))
            cached = self._eligible_cache.get(cache_key) if cacheable else None
            if cached is not None and any(
                path not in self.store.file_graph._nodes for path in cached[1]
            ):
                cached = None
            if cached is None:
                eligible, chunk_ids, eligible_fingerprint = self._select_eligible(
                    tasks, allowed_ids, conditions)
                cached = (allowed_ids, eligible, chunk_ids, eligible_fingerprint)
                if cacheable and not self._read_faults:
                    if len(self._eligible_cache) >= 8:
                        self._eligible_cache.pop(next(iter(self._eligible_cache)))
                    self._eligible_cache[cache_key] = cached
            _, eligible, chunk_ids, eligible_fingerprint = cached
            fingerprint = _digest(dict(policy=POLICY, domain=self.domain,
                                       generation=self._generation, eligible=eligible_fingerprint))
            exact_paths = self._exact_paths(tasks, eligible, query)
            # ReMe filters before ranking. Fetch all matching chunks to select
            # source anchors and preserve every related match for pagination.
            query_terms = set(query_tokens(query))
            if exact_paths is not None:
                matches = [self.store.file_chunks[cid] for path in exact_paths
                           for cid in getattr(self.store.file_graph._nodes.get(path), "chunk_ids", ())
                           if cid in self.store.file_chunks]
            elif eligible:
                expanded_query = " ".join(query_tokens(query)) or query
                scores = self.runner.run(self.store.keyword_index.retrieve_filtered(
                    expanded_query, max(1, len(chunk_ids)), chunk_ids))
                matches = [self.store.file_chunks[cid].model_copy(update={"scores": {"keyword": score, "score": score}})
                           for cid, score in scores.items()]
            else:
                matches = []
            output = {}
            for chunk in matches:
                located = eligible.get(chunk.path)
                if located is None:
                    continue
                ident, task, block = located
                source_metadata = self.file_metadata.get(chunk.path)
                if source_metadata is None:
                    self._remember_fault(
                        ident, ValueError("current block is not in the derived index"), block["block_id"])
                    continue
                metadata = chunk.metadata
                if (metadata.get("mindie_policy") != POLICY
                        or type(metadata.get("mindie_body_offset")) is not int
                        or not 0 <= metadata["mindie_body_offset"] <= len(chunk.text)
                        or not isinstance(source_metadata.get("body_tokens"), list)
                        or not isinstance(source_metadata.get("cites"), list)):
                    raise ValueError("ReMe citation metadata is inconsistent; rebuild the index")
                body = chunk.text[metadata["mindie_body_offset"]:]
                matched_terms = set(source_metadata["body_tokens"]) & query_terms
                # Chunk IDs bind immutable text. Keep the body split with the
                # query-independent terms so header words never become evidence.
                cached_terms = self._chunk_body_terms.get(chunk.id)
                offset = metadata["mindie_body_offset"]
                if cached_terms is None or cached_terms[0] != offset:
                    if cached_terms is not None:
                        del self._chunk_body_terms[chunk.id]
                        self._compact_term_pool()
                    cached_terms = (
                        offset, tuple(self._body_term_pool.setdefault(term, term)
                                      for term in body_tokens(body)),
                    )
                    self._chunk_body_terms[chunk.id] = cached_terms
                terms = cached_terms[1]
                chunk_matches = False
                for term in query_terms:
                    position = bisect_left(terms, term)
                    if position < len(terms) and terms[position] == term:
                        chunk_matches = True
                        break
                # If a header-only chunk outscored the body-containing chunk,
                # still show the latter's actual evidence to the reader.
                selection_rank = (chunk_matches, chunk.score)
                if chunk.path in output and output[chunk.path]["_selection_rank"] >= selection_rank:
                    continue
                entry = task["entry"]
                output[chunk.path] = dict(
                    entry_id=ident, revision=entry["revision"], source=task["source"],
                    kind=entry["kind"], title=entry["title"], summary=entry["summary"],
                    conditions=entry["conditions"], score=chunk.score if exact_paths is None else 1.0,
                    excerpt=self._excerpt(body, query_terms), block_id=block["block_id"],
                    block_title=block["title"], block_summary=block["summary"],
                    navigation=task["navigation"], source_range=block["source_range"],
                    ref=block_ref(self.domain, ident, block["block_id"], block["sha256"]),
                    task_ref=task_ref(self.domain, ident),
                    feedback_ref=feedback_ref(self.domain, ident, entry["revision"]),
                    match_basis="identity" if exact_paths is not None else "body" if matched_terms else "index",
                    _body_terms=matched_terms, _cites=source_metadata["cites"],
                    _selection_rank=selection_rank)
            if self._read_faults:
                faulted = {fault["task_id"] for fault in self._read_faults}
                output = {path: item for path, item in output.items() if item["entry_id"] not in faulted}
                self._eligible_cache.pop(cache_key, None)
                self._fail_if_every_task_unreadable(tasks)
            if exact_paths:
                # Exact lookup is navigation of the requested object. It must
                # never be redirected to a cited source by lexical grouping.
                from .provenance import resolve_citations
                exact = [output[path] for path in exact_paths if path in output]
                for item in exact:
                    item["cites"] = resolve_citations(item["_cites"], tasks, self.domain)
                public = [{key: value for key, value in item.items() if not key.startswith("_")} for item in exact]
                groups = ([dict(public[0], related=public[1:], group_score=1.0, group_basis="task")]
                          if public else [])
            else:
                groups = group_matches(list(output.values()), tasks, self.domain)
            if self._read_faults:
                _retarget_unreadable_cites(
                    groups, {fault["task_id"] for fault in self._read_faults}, self.domain)
            page = page_groups(groups, query=query, conditions=conditions, fingerprint=fingerprint,
                               limit=limit, continuation=continuation)
            cause = self._read_causes[0] if self._read_causes else None
            return MaterialHits(page, self._read_faults, cause)
        finally:
            lock.release()

    def refresh(self, tasks, *, catalog):
        lock = StartLock(self.root / ".index.lock", wait=5.0)
        lock.acquire(wait=5.0)
        try:
            self._refresh(tasks, catalog)
        finally:
            lock.release()

    def close(self):
        # All mutations are checkpointed before a query returns. Do not write
        # from an old reader at close after a second process has updated cache.
        try:
            self.runner.run(self._close())
        finally:
            try:
                if self.checkpoint is not None:
                    self.checkpoint.close()
                    self.checkpoint = None
            finally:
                self.runner.close()
