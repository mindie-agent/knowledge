"""Pinned ReMe components embedded without its service, jobs, agents or LLMs.

Three narrow persistence overrides propagate upstream's swallowed load/write
errors. They do not change chunking, lexical scoring, or graph maintenance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from reme.components import ApplicationContext
from reme.components.file_chunker import MarkdownFileChunker
from reme.components.file_graph import LocalFileGraph
from reme.components.file_store import LocalFileStore
from reme.components.keyword_index import BM25Index
from reme.components.tokenizer import RegexTokenizer
from reme.schema import FileNode
from reme.utils.jsonl_zst import read_jsonl_zst, write_jsonl_zst

from ..loop.locks import StartLock
from ..retrieval import tokens
from .store import _canonical, _digest, _parse_block


class StrictFileGraph(LocalFileGraph):
    async def load(self):
        if self._graph_file.exists():
            for line in read_jsonl_zst(self._graph_file):
                if line.strip():
                    node = FileNode.model_validate_json(line)
                    self._nodes[node.path] = node

    async def dump(self):
        write_jsonl_zst(self._graph_file, (node.model_dump_json() for node in self._nodes.values()))


class TechnicalTokenizerV1(RegexTokenizer):
    """Reuse established API/version/CJK token boundaries inside ReMe BM25.

    Bump this class suffix when token semantics change: ReMe includes the
    class name in its persisted tokenizer fingerprint.
    """

    def _tokenize_one(self, text, **_kwargs):
        return tokens(text)


class StrictBM25Index(BM25Index):
    async def load(self):
        if self.index_file.exists():
            self._restore(self._load_sync())


class StrictFileStore(LocalFileStore):
    async def load(self):
        if self.chunks_path.exists():
            for line in read_jsonl_zst(self.chunks_path, self.encoding):
                if line.strip():
                    chunk = self._deserialize_chunk(line)
                    self.file_chunks[chunk.id] = chunk
        graph_ids = {ident for node in await self.file_graph.get_nodes() for ident in node.chunk_ids}
        if graph_ids != set(self.file_chunks):
            raise ValueError("ReMe derived graph/chunk checkpoint is inconsistent; rebuild the index")
        await self._sync_keyword_index_from_chunks()


class ReMeIndex:
    def __init__(self, root):
        self.root = Path(root)
        self.runner = asyncio.Runner()
        self.components = []
        self.store = None
        self.chunker = None
        self.stamp = self.root / ".reme-index" / "snapshot.json"
        self.bindings = {}
        self._stamp_revision = None

    async def _start(self):
        # Default ReMe config is deliberately not loaded: no session copies,
        # automatic evolution, watchers, HTTP/MCP service, jobs or model calls.
        context = ApplicationContext(workspace_dir=str(self.root), metadata_dir=".reme-index",
                                     session_dir="", mem_session_dir="", resource_dir="",
                                     daily_dir="", digest_dir="", thread_pool_max_workers=0)
        tokenizer = TechnicalTokenizerV1(name="material", app_context=context, filter_stopwords=False)
        keyword = StrictBM25Index(name="material", tokenizer="material", app_context=context)
        graph = StrictFileGraph(name="material", app_context=context)
        self.store = StrictFileStore(name="material", embedding_store="", keyword_index="material",
                                    file_graph="material", tag_index="", app_context=context)
        self.chunker = MarkdownFileChunker(name="material", chunk_byte_size=8192, embed_toc=True,
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
        except BaseException:
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
        raw = self.stamp.read_text(encoding="utf-8") if self.stamp.exists() else None
        fingerprint = _digest(raw)
        if self.store is None or fingerprint != self._stamp_revision:
            # Another local process may have advanced the derived snapshot.
            if self.store is not None:
                # Its old in-memory snapshot must not overwrite the newer one.
                self.components = []
                self.store = None
            self.runner.run(self._start())
            self.bindings = json.loads(raw) if raw is not None else {}
            if not isinstance(self.bindings, dict):
                raise ValueError("invalid ReMe file snapshot")
            self._stamp_revision = fingerprint

    def _refresh(self, tasks):
        self._load()
        desired = {}
        for task_id, task in tasks.items():
            for block in task["blocks"]:
                rel = f"tasks/{task_id}/blocks/{block['block_id']}.md"
                stat = (self.root / rel).stat()
                desired[rel] = dict(descriptor=_digest(block), mtime_ns=stat.st_mtime_ns, size=stat.st_size)
        deleted = set(self.bindings) - set(desired)
        changed = [path for path in desired if self.bindings.get(path) != desired[path]]
        if not changed and not deleted:
            return
        if deleted:
            self.runner.run(self.store.delete(list(deleted)))
        for rel in changed:
            task_id, block_id = rel.split("/")[1], Path(rel).stem
            block = next(b for b in tasks[task_id]["blocks"] if b["block_id"] == block_id)
            # Validate authoritative bytes before ReMe's derived text
            # normalization; universal-newline reads would hide corruption.
            text = (self.root / rel).read_bytes().decode("utf-8")
            _parse_block(text, block)
            node, chunks = self.runner.run(self.chunker.chunk(self.root / rel))
            # Fallible block headers travel with the package. Consumers reuse
            # them locally and never pay another model to summarize the body.
            for chunk in chunks:
                chunk.text = block["title"] + "\n" + block["summary"] + "\n" + chunk.text
                chunk.set_hash_id()
            node.chunk_ids = [chunk.id for chunk in chunks]
            self.runner.run(self.store.upsert([(node, chunks)]))
        self.runner.run(self.store.dump())
        self.stamp.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.stamp.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as stream:
                raw = _canonical(desired)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.stamp)
        except BaseException as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as cleanup:
                exc.add_note(f"ReMe checkpoint temporary cleanup also failed: {type(cleanup).__name__}")
            raise
        self.bindings = desired
        self._stamp_revision = _digest(raw)

    def search(self, tasks, query, limit, conditions, allowed_ids):
        lock = StartLock(self.root / ".index.lock")
        lock.acquire(wait=3)
        try:
            self._refresh(tasks)
            eligible = {}
            for ident, task in tasks.items():
                entry = task["entry"]
                if allowed_ids is not None and ident not in allowed_ids:
                    continue
                if isinstance(allowed_ids, dict) and allowed_ids[ident] != entry["revision"]:
                    raise ValueError("metadata and current material revision differ")
                if entry["kind"] == "knowledge" and any(
                    key in entry["conditions"] and entry["conditions"][key] != str(value)
                    for key, value in (conditions or {}).items()
                ):
                    continue
                for block in task["blocks"]:
                    eligible[f"tasks/{ident}/blocks/{block['block_id']}.md"] = (ident, task, block)
            if not eligible:
                return []
            # ReMe filters before ranking. Fetch all matching chunks to select
            # the best one per task without allowing a long task to hide others.
            matches = self.runner.run(self.store.keyword_search(
                query, limit=max(1, len(self.store.file_chunks)), search_filter={"paths": list(eligible)}))
            output = {}
            for chunk in matches:
                ident, task, block = eligible[chunk.path]
                if ident in output:
                    continue
                entry = task["entry"]
                output[ident] = dict(entry_id=ident, revision=entry["revision"], source=task["source"],
                                     kind=entry["kind"], title=entry["title"], summary=entry["summary"],
                                     conditions=entry["conditions"], score=chunk.score,
                                     excerpt=chunk.text[:1200], block_id=block["block_id"],
                                     block_title=block["title"], block_summary=block["summary"],
                                     navigation=task["navigation"], source_range=block["source_range"])
                if len(output) >= limit:
                    break
            return list(output.values())
        finally:
            lock.release()

    def refresh(self, tasks):
        lock = StartLock(self.root / ".index.lock")
        lock.acquire(wait=3)
        try:
            self._refresh(tasks)
        finally:
            lock.release()

    def close(self):
        # All mutations are checkpointed before a query returns. Do not write
        # from an old reader at close after a second process has updated cache.
        self.components = []
        self.store = None
        self.runner.close()
