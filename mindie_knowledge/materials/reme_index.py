"""Pinned ReMe components embedded without its service, jobs, agents or LLMs.

Component persistence uses one atomic row per current block instead of writing
the entire graph/chunk corpus after each addition. Chunking, lexical scoring
and graph maintenance remain the pinned ReMe implementations.
"""

from __future__ import annotations

import asyncio
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
from ..retrieval import tokens
from .store import _canonical, _digest, _parse_block
from .provenance import POLICY, body_tokens, citations, group_matches, page_groups
from .references import block_ref, feedback_ref, parse_read_ref, task_ref
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

    def _refresh(self, tasks, catalog):
        self._load()
        generation = catalog.generation
        if self._generation > generation:
            raise ValueError("ReMe checkpoint is newer than the current material catalog")
        if self._generation == generation:
            return
        try:
            self._apply_changes(tasks, catalog, generation)
        except BaseException as exc:
            # Chunking, deletion and upsert mutate memory before persistence.
            # Restore the committed checkpoint on the next attempt even when
            # failure happened before the checkpoint transaction began.
            try:
                self.runner.run(self._close())
            except BaseException as cleanup:
                exc.add_note(f"ReMe cleanup also failed: {type(cleanup).__name__}: {cleanup}")
            self._generation = None
            raise

    def _apply_changes(self, tasks, catalog, generation):
        desired, deleted = {}, set()
        for change in catalog.changes(self._generation):
            task_id = change["task_id"]
            task = tasks.get(task_id)
            paths = set()
            for block in task["blocks"] if task else ():
                rel = f"tasks/{task_id}/blocks/{block['block_id']}.md"
                paths.add(rel)
                if self.bindings.get(rel, {}).get("descriptor") != _digest(block):
                    desired[rel] = block
            deleted.update(self.task_paths.get(task_id, set()) - paths)
            if paths:
                self.task_paths[task_id] = paths
            else:
                self.task_paths.pop(task_id, None)
        if deleted:
            self.runner.run(self.store.delete(list(deleted)))
        changed = {}
        for rel, block in desired.items():
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
            self.runner.run(self.store.upsert([(node, chunks)]))
            binding = dict(descriptor=_digest(block), signature=signature(self.root / rel))
            metadata = dict(body_tokens=direct_tokens, cites=cites)
            changed[rel] = dict(binding=binding, metadata=metadata, node=node.model_dump(mode="json"),
                chunks=[dict(value=chunk.model_dump(mode="json"),
                             frequencies=dict(Counter(self.store.keyword_index._tokenize(chunk.text)))) for chunk in chunks])
        self.checkpoint.commit(changed, deleted, generation)
        for path in deleted:
            self.bindings.pop(path, None)
            self.file_metadata.pop(path, None)
        for path, value in changed.items():
            self.bindings[path] = value["binding"]
            self.file_metadata[path] = value["metadata"]
        keyword = self.store.keyword_index
        if len(keyword._doc_ids) > max(64, 2 * keyword.n_docs):
            self.runner.run(keyword.optimize_index())
        self._generation = generation
        self._eligible_cache.clear()

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

    def search(self, tasks, query, limit, conditions, allowed_ids, continuation=None, *, catalog):
        lock = StartLock(self.root / ".index.lock")
        lock.acquire(wait=None)
        try:
            self._refresh(tasks, catalog)
            cacheable = allowed_ids is None or isinstance(allowed_ids, CurrentVisibility)
            cache_key = (self._generation, _canonical(conditions or {}), id(allowed_ids))
            cached = self._eligible_cache.get(cache_key) if cacheable else None
            if cached is None:
                eligible = {}
                if allowed_ids is not None and set(allowed_ids) - set(tasks):
                    raise ValueError("metadata references material pointers not yet promoted")
                for ident, task in tasks.items():
                    entry = task["entry"]
                    if allowed_ids is not None and ident not in allowed_ids:
                        continue
                    if isinstance(allowed_ids, Mapping) and allowed_ids[ident] != entry["revision"]:
                        raise ValueError("metadata and current material revision differ")
                    if entry["kind"] == "knowledge" and any(
                        key in entry["conditions"] and entry["conditions"][key] != str(value)
                        for key, value in (conditions or {}).items()
                    ):
                        continue
                    for block in task["blocks"]:
                        eligible[f"tasks/{ident}/blocks/{block['block_id']}.md"] = (ident, task, block)
                chunk_ids = {cid for path in eligible for cid in self.store.file_graph._nodes[path].chunk_ids
                             if cid in self.store.keyword_index.document_ids}
                cached = (allowed_ids, eligible, chunk_ids, _digest(sorted(eligible)))
                if cacheable:
                    if len(self._eligible_cache) >= 8:
                        self._eligible_cache.pop(next(iter(self._eligible_cache)))
                    self._eligible_cache[cache_key] = cached
            _, eligible, chunk_ids, eligible_fingerprint = cached
            fingerprint = _digest(dict(policy=POLICY, domain=self.domain,
                                       generation=self._generation, eligible=eligible_fingerprint))
            exact_paths = self._exact_paths(tasks, eligible, query)
            # ReMe filters before ranking. Fetch all matching chunks to select
            # source anchors and preserve every related match for pagination.
            if exact_paths is not None:
                matches = [self.store.file_chunks[cid] for path in exact_paths
                           for cid in self.store.file_graph._nodes[path].chunk_ids]
            elif eligible:
                scores = self.runner.run(self.store.keyword_index.retrieve_filtered(
                    query, max(1, len(chunk_ids)), chunk_ids))
                matches = [self.store.file_chunks[cid].model_copy(update={"scores": {"keyword": score, "score": score}})
                           for cid, score in scores.items()]
            else:
                matches = []
            output = {}
            query_terms = set(tokens(query))
            for chunk in matches:
                ident, task, block = eligible[chunk.path]
                metadata = chunk.metadata
                source_metadata = self.file_metadata[chunk.path]
                if (metadata.get("mindie_policy") != POLICY
                        or type(metadata.get("mindie_body_offset")) is not int
                        or not 0 <= metadata["mindie_body_offset"] <= len(chunk.text)
                        or not isinstance(source_metadata.get("body_tokens"), list)
                        or not isinstance(source_metadata.get("cites"), list)):
                    raise ValueError("ReMe citation metadata is inconsistent; rebuild the index")
                body = chunk.text[metadata["mindie_body_offset"]:]
                matched_terms = set(source_metadata["body_tokens"]) & query_terms
                chunk_terms = set(body_tokens(body)) & query_terms
                # If a header-only chunk outscored the body-containing chunk,
                # still show the latter's actual evidence to the reader.
                selection_rank = (bool(chunk_terms), chunk.score)
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
            return page_groups(groups, query=query, conditions=conditions, fingerprint=fingerprint,
                               limit=limit, continuation=continuation)
        finally:
            lock.release()

    def refresh(self, tasks, *, catalog):
        lock = StartLock(self.root / ".index.lock")
        lock.acquire(wait=None)
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
