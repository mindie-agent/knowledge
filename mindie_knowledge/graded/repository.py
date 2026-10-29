"""[Role] Install C5 units into the existing domain store and resolve references.

[Invariants] SQLite selects one validated graded corpus. Existing task data is
preserved; a removed unit is unreadable even through an earlier pinned ref.
[Timeouts & Locks] Uses Store's existing WAL connection and write transaction.
[Side-Effect Policy] Local content-addressed files; catalog and derived index
rows are updated together in the existing SQLite write transaction.
No remote publication, model calls, history scan or new service state machine.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from ..markdown import _atomic_write_bytes
from ..materials.references import ReadReferenceError
from .budget import enforce_explain_budget, enforce_query_budget
from .index import RoutingIndex, _ref_parts
from .units import GradedUnit, canonical

CATALOG_KEY = "graded:current"
INDEX_KEY = "graded:index"


class GradedRepository:
    def __init__(self, store):
        # A filesystem root selects the corpus-review surface; a Store keeps
        # the existing installed-catalog behavior. The two authorities are
        # never mixed, so a path cannot masquerade as an installed store.
        if isinstance(store, (str, Path)):
            self.root = Path(store)
            self.store = None
        else:
            self.store = store
            self.root = None
        self._catalog_raw = None
        self._index = None

    def _corpus_root(self):
        if self.root is None:
            raise ValueError("manifest review requires a filesystem corpus root")
        return self.root

    def build_manifest(self, *, index_dir=None):
        """Validate the corpus and atomically rebuild both derived indexes."""
        from .maintenance import build_routing_index
        report = build_routing_index(self._corpus_root(), index_dir=index_dir)
        if not report.get("ok"):
            errors = "; ".join(item.get("message", "") for item in report.get("errors", ()))
            raise ValueError(f"routing manifest rebuild failed: {errors}")
        return json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))

    def validate_corpus(self):
        """Return the C5/C8 validity report without writing any file."""
        from .maintenance import load_corpus
        units, index, errors = load_corpus(self._corpus_root())
        tiers = {tier: sum(1 for unit in units if unit.metadata["tier"] == tier)
                 for tier in ("L1", "L2", "L3", "L4")}
        categories = {category: sum(1 for unit in units if unit.metadata["category"] == category)
                      for category in ("axiom", "measurement", "sop", "workflow", "mutator")}
        return {"valid": not errors and index is not None, "unit_count": len(units),
                "tiers": tiers, "categories": categories, "errors": list(errors),
                "generation": index.generation if index is not None else None}

    def _current(self):
        row = self.store.db.execute("SELECT value FROM state WHERE key=?", (CATALOG_KEY,)).fetchone()
        return row[0] if row else None

    def available(self):
        with self.store.lock:
            return self._current() is not None

    def _catalog(self):
        raw = self._current()
        if raw is None:
            return {"schema": "mindie-graded-catalog/1", "local": [], "feeds": {}, "retired_local": []}
        value = json.loads(raw)
        if value.get("schema") != "mindie-graded-catalog/1":
            raise ValueError("graded catalog schema mismatch")
        # Earlier local C5 installations are local candidates, never implicit
        # consent or evidence that any public feed has switched formats.
        value.setdefault("local", value.get("units", []))
        value.setdefault("feeds", {})
        value.setdefault("retired_local", [])
        return value

    @staticmethod
    def _pending_local(index, local_by_id, ident):
        candidate = local_by_id.get(ident)
        if candidate is not None and candidate["content_sha256"] != index.units[ident].content_sha256:
            return {"has_pending_local_candidate": True,
                    "pending_local_revision": candidate["revision"]}
        return {}

    def _descriptor(self, unit, **metadata):
        raw = unit.render().encode("utf-8")
        blob = hashlib.sha256(raw).hexdigest()
        _atomic_write_bytes(self.store.root / "graded" / "blobs" / f"{blob}.md", raw)
        return {"id": unit.id, "blob": blob, "content_sha256": unit.content_sha256, "revision": unit.revision,
                "path": unit.path or f"{unit.metadata['domain_path']}/{unit.id}.md", **metadata}

    def _published(self, catalog):
        result = {}
        for feed in catalog["feeds"].values():
            for item in feed["units"]:
                prior = result.get(item["id"])
                if prior and (prior["content_sha256"], prior["path"]) != (item["content_sha256"], item["path"]):
                    raise ValueError("graded feeds disagree on one stable unit")
                if prior and self._read(prior).revision != self._read(item).revision:
                    raise ValueError("graded feeds disagree on one stable unit revision")
                result[item["id"]] = item
        return result

    def _save(self, catalog):
        # Public current bytes outrank an unmerged local correction. A newly
        # distilled ID remains readable locally until it appears in the feed.
        visible = {item["id"]: item for item in catalog["local"]}
        visible.update(self._published(catalog))
        index = RoutingIndex(self._read(item) for item in visible.values())
        catalog.update(units=list(visible.values()), generation=index.generation)
        self._persist_index(catalog, index)
        return index

    def _persist_index(self, catalog, index):
        from .precomputed import dump_index
        # Both rows belong to the caller's existing write transaction. The
        # small catalog stays cheap to read on every query and feedback lookup.
        snapshot = dump_index(index)
        self.store.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (INDEX_KEY, snapshot))
        raw = canonical(catalog)
        self.store.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (CATALOG_KEY, raw))
        self._catalog_raw, self._index = raw, index

    def install(self, units, *, source_identity="local-reviewed-units", generation=None, admitted_ids=None):
        """Install a reviewed local snapshot; publication needs its own grant.

        The optional generation is supplied only by the admitted, redacted C6
        path. Merely importing Markdown never authorizes outbound publication.
        """
        incoming = RoutingIndex(units)
        with self.store._write_txn():
            catalog = self._catalog()
            previous = self._load().units if self.available() else {}
            old_local = {item["id"]: item for item in catalog["local"]}
            published = self._published(catalog)
            selected = set(incoming.units) if admitted_ids is None else set(admitted_ids)
            if not selected <= set(incoming.units):
                raise ValueError("graded admission identifies a unit outside the prepared snapshot")
            local = [] if admitted_ids is None else [item for item in catalog["local"] if item["id"] not in selected]
            for unit in incoming.units.values():
                if unit.id not in selected:
                    continue
                old = previous.get(unit.id)
                revision = (old.revision if old.content_sha256 == unit.content_sha256
                            else max(old.revision + 1, unit.revision)) if old else unit.revision
                unit = GradedUnit.from_parts({**unit.metadata, "revision": revision}, unit.body, unit.path)
                public = published.get(unit.id)
                path = unit.path or f"{unit.metadata['domain_path']}/{unit.id}.md"
                if public and public["content_sha256"] == unit.content_sha256 and public["path"] == path:
                    continue
                old_descriptor = old_local.get(unit.id, {})
                same = (old_descriptor.get("content_sha256") == unit.content_sha256
                        and old_descriptor.get("path") == path)
                meta = {key: old_descriptor[key] for key in ("generation", "base", "batched_blob")
                        if same and key in old_descriptor}
                if not same:
                    meta["base"] = public
                if generation is not None:
                    meta["generation"] = generation
                local.append(self._descriptor(unit, **meta))
            catalog.update(local=local, source_identity=source_identity)
            index = self._save(catalog)
        return {"status": "installed", "units": len(index.units), "generation": index.generation,
                "build_elapsed_ms": index.build_elapsed_ms, "legacy_data_preserved": True,
                "publication": "eligible_for_existing_outbox" if generation is not None else "not_requested"}

    def install_feed(self, units, *, feed_ident, source_revision, files=None, feedback=()):
        """Replace one fully validated feed snapshot without inventing revisions.

        Matching local candidates retire after main contains them. Withdrawn
        public IDs cannot reappear through an older local candidate. Their
        content-addressed bytes and prior vote observations remain retained.
        """
        index = RoutingIndex(units)
        from .feedback import validate_bindings, validate_observed_units
        feedback = list(feedback)
        validate_bindings(feedback, index.units)
        with self.store._write_txn():
            validate_observed_units(self.store, index.units.values())
            catalog = self._catalog()
            previous = catalog["feeds"].get(feed_ident, {"units": []})
            descriptors = []
            previous_units = {item["id"]: self._read(item) for item in previous["units"]}
            for unit in index.units.values():
                old = previous_units.get(unit.id)
                if old and ((old.content_sha256 == unit.content_sha256 and old.revision != unit.revision)
                            or (old.content_sha256 != unit.content_sha256 and unit.revision <= old.revision)):
                    raise ValueError("graded feed update does not preserve or advance its semantic revision")
                path = unit.path or f"{unit.metadata['domain_path']}/{unit.id}.md"
                raw_sha = (files or {}).get(path) or hashlib.sha256(unit.render().encode()).hexdigest()
                descriptors.append(self._descriptor(unit, file_sha256=raw_sha,
                                                     source_revision=source_revision, feed_ident=feed_ident))
            catalog["feeds"][feed_ident] = {"units": descriptors, "source_revision": source_revision,
                                            "feedback": list(feedback), "files": dict(files or {})}
            self._reconcile_local(catalog, previous["units"])
            self._save(catalog)
            retired = self.store.retire_material_feed(feed_ident)
            if retired:
                # Only a fully validated replacement of the SAME public source
                # changes old memberships. Retain old task/vote/file bytes.
                key = "graded-format-switch:" + feed_ident
                retained = (self.store.feed_get(key) or {}).get("retained_task_revisions", {})
                for ident, revision in retired.items():
                    retained[ident] = sorted(set(retained.get(ident, ())) | {revision})
                self.store.feed_set(key,
                                    {"from": "material-package", "to": "graded", "commit": source_revision,
                                     "retained_task_bytes": True, "retained_task_revisions": retained})
            self.store.feed_set("published-commit", {"commit": source_revision, "feed": feed_ident,
                                                     "contract_schema": "mindie-publication-contract/2"})
        return {"entries": len(descriptors), "units": len(descriptors), "source_revision": source_revision}

    def _reconcile_local(self, catalog, previous):
        public = self._published(catalog)
        removed = {item["id"] for item in previous} - set(public)
        kept = []
        for item in catalog["local"]:
            target = public.get(item["id"])
            matched = target and (target["content_sha256"], target["path"]) == (item["content_sha256"], item["path"])
            if item["id"] in removed or matched:
                catalog["retired_local"].append(item)
            else:
                kept.append(item)
        # A withdrawn public parent also removes local candidates whose
        # premise closure no longer exists. Retain their bytes for review.
        while True:
            members = set(public) | {item["id"] for item in kept}
            dangling = [item for item in kept
                        if not set(self._read(item).metadata["inherits_from"]) <= members]
            if not dangling:
                break
            catalog["retired_local"].extend(dangling)
            kept = [item for item in kept if item not in dangling]
        catalog["local"] = kept

    def retire_feed(self, feed_ident):
        """Remove a replaced public format; immutable blobs and votes survive."""
        with self.store._write_txn():
            catalog = self._catalog()
            previous = catalog["feeds"].pop(feed_ident, None)
            if previous is None:
                return False
            self._reconcile_local(catalog, previous["units"])
            self._save(catalog)
            return True

    def pending(self, generation):
        with self.store.lock:
            if not self.available():
                return []
            return [item for item in self._catalog()["local"]
                    if generation is not None and item.get("generation") == generation
                    and item.get("batched_blob") != item["blob"]]

    def mark_batched(self, descriptors):
        catalog = self._catalog()
        frozen = {item["id"]: item["blob"] for item in descriptors}
        for item in catalog["local"]:
            if frozen.get(item["id"]) == item["blob"]:
                item["batched_blob"] = item["blob"]
        self._save(catalog)

    def public_ids(self):
        with self.store.lock:
            return set(self._published(self._catalog()))

    def _feedback(self, unit):
        from .feedback import outbound_votes, summarize_votes
        catalog = self._catalog()
        public = [vote for feed in catalog["feeds"].values()
                  if any(item["id"] == unit.id for item in feed["units"])
                  for vote in feed.get("feedback", [])]
        local = [dict(row) for row in self.store.db.execute("SELECT * FROM votes WHERE entry_id=?", (unit.id,))]
        return summarize_votes([*public, *outbound_votes(self.store, local)], unit)

    def install_root(self, root):
        index = RoutingIndex.from_root(root)
        result = self.install(index.units.values(), source_identity=str(Path(root).resolve()))
        result["build_elapsed_ms"] = index.build_elapsed_ms
        return result

    def _read_blob(self, descriptor):
        blob = descriptor["blob"]
        if not isinstance(blob, str) or not re.fullmatch(r"[a-f0-9]{64}", blob):
            raise ValueError("invalid graded blob identity")
        raw = (self.store.root / "graded" / "blobs" / f"{blob}.md").read_bytes()
        if hashlib.sha256(raw).hexdigest() != blob:
            raise ValueError("graded content digest mismatch")
        return raw

    def _read(self, descriptor):
        raw = self._read_blob(descriptor)
        unit = GradedUnit.from_text(raw.decode("utf-8"), descriptor["path"])
        if unit.id != descriptor["id"] or unit.content_sha256 != descriptor["content_sha256"]:
            raise ValueError("graded catalog and content disagree")
        return unit

    def _load(self):
        raw = self._current()
        if raw is None:
            raise ValueError("no graded corpus is installed")
        if raw != self._catalog_raw:
            from .precomputed import load_index
            started = time.perf_counter()
            raw, catalog, snapshot = self._index_rows()
            if snapshot is None:
                return self._rebuild_index(catalog, started)
            try:
                index = load_index(snapshot, catalog)
            except ValueError as exc:
                if str(exc) not in {"unsupported graded index schema",
                                    "graded index generation mismatch"}:
                    raise
                return self._rebuild_index(catalog, started)
            index.build_elapsed_ms = (time.perf_counter() - started) * 1000
            self._catalog_raw, self._index = raw, index
        return self._index

    def _rebuild_index(self, catalog, started):
        # The projection is derived; rebuild it once under the existing write
        # transaction, re-reading catalog/index after the lock so a newer feed
        # publication is never replaced by this reader.
        with self.store._write_txn():
            raw, current, snapshot = self._index_rows()
            index = None
            if snapshot is not None:
                from .precomputed import load_index
                try:
                    index = load_index(snapshot, current)
                except ValueError as exc:
                    if str(exc) not in {"unsupported graded index schema",
                                        "graded index generation mismatch"}:
                        raise
            if index is None:
                index = RoutingIndex(self._read(item) for item in current["units"])
                if index.generation != current["generation"]:
                    raise ValueError("graded catalog generation mismatch")
                self._persist_index(current, index)
                raw = self._catalog_raw
            index.build_elapsed_ms = (time.perf_counter() - started) * 1000
            self._catalog_raw, self._index = raw, index
            return index

    def _index_rows(self):
        # A single statement sees one SQLite snapshot even if another process
        # publishes a generation between the earlier hot-path check and here.
        rows = dict(self.store.db.execute("SELECT key,value FROM state WHERE key IN (?,?)",
                                          (CATALOG_KEY, INDEX_KEY)))
        raw = rows.get(CATALOG_KEY)
        if raw is None:
            raise ValueError("no graded corpus is installed")
        catalog = json.loads(raw)
        if catalog.get("schema") != "mindie-graded-catalog/1":
            raise ValueError("graded catalog schema mismatch")
        return raw, catalog, rows.get(INDEX_KEY)

    def ref(self, unit_id, revision=None):
        return f"mindie://{self.store.domain}/units/{unit_id}" + ("@" + str(revision) if revision is not None else "")

    def _bind_exploration_refs(self, payload, index):
        context = payload.get("exploration_context")
        if context is None:
            return
        from .explore import refresh_exploration_mounts
        for item in context["mounted_units"]:
            item["ref"] = self.ref(item["id"], item["revision"])
        mounted = {item["id"] for item in context["mounted_units"]}
        for item in payload.get("unresolved", ()):
            if item["status"] != "budget_excluded" or item["id"] in mounted:
                continue
            unit = index.units.get(item["id"])
            if unit is not None and unit.metadata["tier"] != "L3":
                context.setdefault("excluded_units", []).append({
                    "id": unit.id, "revision": unit.revision, "tier": unit.metadata["tier"],
                    "ref": self.ref(unit.id, unit.revision), "status": "budget_excluded"})
        refresh_exploration_mounts(context)

    def query(self, query, *, navigation_only=False, run_id=None, mode="reuse",
              ledger_entries=None, bottleneck_summary=None, native_subcontext=False,
              goal="", defer_delivery=False, **kwargs):
        """Assemble a page from one index; deferred composition retains that index.

        Mixed reads get the in-memory (payload, index) pair and own the final
        budget/receipt. The index never enters the serialized response.
        """
        with self.store.lock:
            index = self._load()
            result = index.query(
                query, navigation_only=navigation_only, mode=mode,
                ledger_entries=ledger_entries, bottleneck_summary=bottleneck_summary,
                native_subcontext=native_subcontext, goal=goal, **kwargs)
            local_by_id = {item["id"]: item for item in self._catalog()["local"]}
            for item in result["results"]:
                item["ref"] = self.ref(item["id"], item["revision"])
                item["unit_ref"] = self.ref(item["id"])
                item["feedback_ref"] = item["ref"]
                item["reference_kind"] = "graded_unit"
                item["source"] = "graded"
                item.update(self._pending_local(index, local_by_id, item["id"]))
                unit = index.units[item["id"]]
                item["feedback"] = self._feedback(unit)
                for parent in item["inherited_rules"]:
                    parent["ref"] = self.ref(parent["id"], parent["revision"])
                    parent["feedback_ref"] = parent["ref"]
            for card in result.get("cards", []):
                card["ref"] = self.ref(card["id"], card["revision"])
                card["unit_ref"] = self.ref(card["id"])
                card["feedback_ref"] = card["ref"]
                card.update(self._pending_local(index, local_by_id, card["id"]))
            for card in result.get("foundation_cards", []):
                card["ref"] = self.ref(card["id"], card["revision"])
                card["unit_ref"] = self.ref(card["id"])
                card["feedback_ref"] = card["ref"]
                card.update(self._pending_local(index, local_by_id, card["id"]))
            for card in result.get("unverified_cards", []):
                card["ref"] = self.ref(card["id"], card["revision"])
                card["unit_ref"] = self.ref(card["id"])
                card["feedback_ref"] = card["ref"]
                card.update(self._pending_local(index, local_by_id, card["id"]))
            payload = {"domain": self.store.domain, "retrieval": "graded-bm25", "page": "graded_units", **result}
            self._bind_exploration_refs(payload, index)
            enforce_query_budget(payload, index, navigation_only=navigation_only)
            # A mixed page composes and trims after this call, so it records the
            # delivery itself once its own outward payload is final.
            if not defer_delivery:
                self.observe_delivered(index, payload, delivery="query", run_id=run_id)
        return (payload, index) if defer_delivery else payload

    @staticmethod
    def _delivered_units(payload, delivery):
        """Name every graded unit in one final outward payload, once each.

        A counted delivery is the unit the caller actually received; an
        inherited premise is registered as readable without being counted as
        its own delivery.
        """
        delivered = []
        for item in payload.get("results", ()):
            if item.get("source") not in (None, "graded"):
                continue
            delivered.append((item["id"], delivery))
            delivered.extend((parent["id"], None)
                             for parent in item.get("inherited_rules", ()) or ())
        for key in ("cards", "foundation_cards", "unverified_cards", "units", "requires"):
            delivered.extend((card["id"], delivery) for card in payload.get(key, []))
        # A trimmed result can still be delivered in the compact exploration
        # context. Its `units`/foundation/mutator views alias mounted_units;
        # read that canonical collection once and let revision deduplication
        # combine it with any surviving rows or cards.
        context = payload.get("exploration_context") or {}
        delivered.extend((unit["id"], delivery) for unit in context.get("mounted_units", ()))
        return delivered

    def observe_delivered(self, index, payload, *, delivery="query", run_id=None):
        """Record one receipt per delivered (unit_id, revision) in one transaction.

        The same unit reaches a caller as both a result row and its navigation
        card; that is one delivery, so its counter advances once. `observe`
        reuses this already-open write transaction instead of opening its own.
        """
        from .feedback import observe

        selected = {}
        for ident, kind in self._delivered_units(payload, delivery):
            unit = index.units[ident]
            key = (unit.id, unit.revision)
            # A counted delivery outranks a bare inherited-premise registration.
            if kind is not None or key not in selected:
                selected[key] = (unit, kind)
        if not selected:
            return
        with self.store._write_txn():
            for unit, kind in selected.values():
                observe(self.store, unit, delivery=kind, run_id=run_id)

    def _legacy_view(self, index, ident):
        unit = index.units[ident]
        return {"kind": "graded_unit", "feedback_ref": self.ref(ident, unit.revision),
                "feedback": self._feedback(unit), "domain": self.store.domain,
                "ref": self.ref(ident, unit.revision), "unit_ref": self.ref(ident), "id": ident,
                "revision": unit.revision,
                "frontmatter": unit.metadata, "content": unit.body,
                "inherits_from_closure": index.closure[ident],
                "status": unit.status, "superseded_by": unit.superseded_by,
                "note": "Reading a reference does not establish applicability; compare its conditions with observed task facts."}

    def explain(self, ref=None, *, refs=None, conditions=None, known_refs=None,
                token_budget=4096, run_id=None, mode="reuse", ledger_entries=None,
                bottleneck_summary=None, native_subcontext=False, goal=""):
        if mode not in {"reuse", "diagnose", "explore"}:
            raise ValueError("mode must be one of ['diagnose', 'explore', 'reuse']")
        requested = [] if refs is None else (list(refs) if not isinstance(refs, str) else [refs])
        if ref is not None and ref not in requested:
            requested.insert(0, ref)
        known = [known_refs] if isinstance(known_refs, str) else list(known_refs or ())
        with self.store.lock:
            index = self._load()
            parts = _ref_parts(ref) if isinstance(ref, str) else None
            if isinstance(ref, str) and refs is None:
                if parts is None or parts[0] not in (None, self.store.domain):
                    raise ReadReferenceError("outside_domain", "The graded reference is outside the selected domain.")
                unit = index.units.get(parts[1])
                if unit is None:
                    raise ReadReferenceError("removed_or_superseded", "The graded unit is absent from the current collection.")
                if parts[2] is not None and parts[2] != unit.revision:
                    raise ReadReferenceError("removed_or_superseded", "The observed graded revision was replaced; no other bytes were substituted.")
            result = index.explain_closure(
                requested, conditions=conditions, known_refs=known,
                domain=self.store.domain, token_budget=token_budget, mode=mode,
                ledger_entries=ledger_entries, bottleneck_summary=bottleneck_summary,
                native_subcontext=native_subcontext, goal=goal)
            local_by_id = {item["id"]: item for item in self._catalog()["local"]}
            for bucket in ("requires", "units"):
                for item in result.get(bucket, []):
                    unit = index.units[item["id"]]
                    item["ref"] = self.ref(item["id"], unit.revision)
                    item["unit_ref"] = self.ref(item["id"])
                    item["feedback_ref"] = item["ref"]
                    item.update(self._pending_local(index, local_by_id, item["id"]))
            result["domain"] = self.store.domain
            self._bind_exploration_refs(result, index)
            single_view = {}
            if (parts is not None and parts[0] in (None, self.store.domain)
                    and any(item["id"] == parts[1]
                            for bucket in ("requires", "units") for item in result[bucket])):
                single_view = self._legacy_view(index, parts[1])
                single_view.update(self._pending_local(index, local_by_id, parts[1]))

            def derive_single_view(payload):
                # Existing single-ref consumers read top-level content and
                # frontmatter. Charge that view before trimming, and remove it
                # with its unit so an excluded body cannot escape the budget.
                for key in single_view:
                    if key != "domain":
                        payload.pop(key, None)
                if single_view and any(item["id"] == single_view["id"]
                                       for bucket in ("requires", "units")
                                       for item in payload[bucket]):
                    payload.update(single_view)

            visible = {
                (parsed[1], parsed[2]) for value in known
                if (parsed := _ref_parts(value)) is not None
                and parsed[0] in (None, self.store.domain) and parsed[2] is not None
            }
            visible_requires = set()
            for value in requested:
                parsed = _ref_parts(value)
                if parsed is None or parsed[0] not in (None, self.store.domain):
                    continue
                unit = index.units.get(parsed[1])
                if (unit is not None and parsed[2] in (None, unit.revision)
                        and (unit.id, unit.revision) in visible):
                    # A visible requested body still needs its missing premises,
                    # even after another requested body's closure is removed.
                    visible_requires.update(index.closure[unit.id])
            enforce_explain_budget(result, index, token_budget, derive=derive_single_view,
                                   visible_requires=visible_requires)
            self.observe_delivered(index, result, delivery="explain", run_id=run_id)
            return result


def build_manifest(root, *, index_dir=None):
    """Convenience wrapper for corpus review scripts and release checks."""
    return GradedRepository(root).build_manifest(index_dir=index_dir)


def validate_corpus(root):
    """Convenience wrapper for a non-writing C5/C8 corpus check."""
    return GradedRepository(root).validate_corpus()


def validate_repository(root):
    """Compatibility wrapper for repository-level validation reports."""
    from .maintenance import validate_repository as validate
    return validate(root)


def build_index(root, *, index_dir=None, check=False):
    """Compatibility wrapper for the routing-index rebuild entry point."""
    from .maintenance import build_index as build
    return build(root, index_dir=index_dir, check=check)
