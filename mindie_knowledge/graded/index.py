"""[Role] C8 inheritance index and condition-aware local graded retrieval.

[Invariants] Resolve all stable references at build time; never truncate semantic
inheritance by depth. Each returned parent independently matches task conditions.
[Timeouts & Locks] No model/network work; build time is separate from query time.
[Side-Effect Policy] Read explicit corpus roots only; never write or import tasks.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import math
import os
from pathlib import Path
import re
import time

from ..retrieval import token_counts, tokens
from .conditions import match_conditions
from .units import GradedUnit, canonical

SKIP_DIRECTORIES = {".git", "tasks", "feedback", "index", "docs", "private"}
MAX_UNIT_BYTES = 262_144


def token_estimate(text: str) -> int:
    """Conservative UTF-8/4 estimate, explicitly not a model tokenizer count."""
    return math.ceil(len(text.encode("utf-8")) / 4)


def _excerpt(text: str, budget: int) -> tuple[str, int, bool]:
    raw = text.encode("utf-8")
    chunk = raw[:budget * 4].decode("utf-8", errors="ignore")
    return chunk, token_estimate(chunk), len(chunk) != len(text)


def read_units(root: Path) -> list[GradedUnit]:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("corpus root must be a directory")
    units = []
    for directory, children, files in os.walk(root):
        children[:] = sorted(name for name in children if (Path(directory) != root or name not in SKIP_DIRECTORIES) and not name.startswith(".")
                             and not name.lower().startswith(("private-history", "private_history", "history-private", "history_private")))
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            path = Path(directory) / name
            path.resolve(strict=True).relative_to(root)
            with path.open("rb") as stream:
                prefix = stream.read(8192)
                # A corpus may contain ordinary documentation during migration.
                # Only explicit C5 units enter this index; task packages are never read.
                if not prefix.startswith(b"---\n") or not re.search(rb"(?m)^schema:\s*['\"]?mindie-graded-unit/1['\"]?\s*$", prefix):
                    continue
                raw = prefix + stream.read(MAX_UNIT_BYTES + 1 - len(prefix))
            if len(raw) > MAX_UNIT_BYTES:
                raise ValueError(f"graded unit exceeds {MAX_UNIT_BYTES} bytes: {path.relative_to(root)}")
            units.append(GradedUnit.from_text(raw.decode("utf-8"), path.relative_to(root).as_posix()))
    return units


class RoutingIndex:
    def __init__(self, units):
        started = time.perf_counter()
        self.units = {}
        paths = set()
        for unit in units:
            if unit.id in self.units:
                raise ValueError(f"duplicate stable unit ID: {unit.id}")
            path = unit.path or f"{unit.metadata['domain_path']}/{unit.id}.md"
            if path in paths:
                raise ValueError(f"two units occupy the same path: {path}")
            paths.add(path)
            self.units[unit.id] = unit
        self.closure = {}
        visiting = []

        def visit(ident):
            if ident in self.closure:
                return self.closure[ident]
            if ident in visiting:
                raise ValueError("inheritance cycle: " + " -> ".join([*visiting, ident]))
            if ident not in self.units:
                raise ValueError(f"missing inherited unit: {ident}")
            visiting.append(ident)
            ancestors = []
            for parent in self.units[ident].metadata["inherits_from"]:
                for member in [*visit(parent), parent]:
                    if member not in ancestors:
                        ancestors.append(member)
            visiting.pop()
            self.closure[ident] = ancestors
            return ancestors

        for ident in self.units:
            visit(ident)
        self.closure_digests = {ident: "sha256:" + hashlib.sha256(canonical([
            (member, self.units[member].revision, self.units[member].content_sha256)
            for member in [*ancestors, ident]
        ]).encode()).hexdigest() for ident, ancestors in self.closure.items()}
        self.counts = {}
        self.frequencies = Counter()
        self.condition_keys = {}
        self.conditions = {}
        for ident, unit in self.units.items():
            key = canonical(unit.metadata["conditions"])
            self.condition_keys[ident] = key
            self.conditions[key] = unit.metadata["conditions"]
            triggers = unit.metadata["triggers"]
            count = token_counts("\n".join([unit.metadata["title"], *triggers["error_signatures"], *triggers["keywords"], unit.body]))
            self.counts[ident] = count
            self.frequencies.update(count.keys())
        self.lengths = {ident: sum(count.values()) for ident, count in self.counts.items()}
        self.average = sum(self.lengths.values()) / max(1, len(self.units)) or 1
        self.generation = hashlib.sha256(canonical([
            (ident, unit.revision, unit.content_sha256, unit.path, unit.metadata["domain_path"])
            for ident, unit in sorted(self.units.items())
        ]).encode()).hexdigest()
        self.build_elapsed_ms = (time.perf_counter() - started) * 1000

    @classmethod
    def from_root(cls, root):
        started = time.perf_counter()
        index = cls(read_units(Path(root)))
        index.build_elapsed_ms = (time.perf_counter() - started) * 1000
        return index

    def manifest(self):
        rows = []
        for ident, unit in sorted(self.units.items()):
            metadata = unit.metadata
            rows.append({
                **{key: metadata[key] for key in ("id", "tier", "evidence_strength", "category", "domain_path", "title", "triggers", "conditions", "inherits_from")},
                "inherits_from_closure": self.closure[ident],
                "path": unit.path or f"{metadata['domain_path']}/{ident}.md",
                "content_sha256": unit.content_sha256,
                "revision": unit.revision, "content_digest": unit.metadata["content_digest"],
                "closure_digest": self.closure_digests[ident],
                "token_estimate": token_estimate(unit.body),
                "provenance": metadata["provenance"],
            })
        return {"schema_version": "mindie-routing-manifest/1", "generation": self.generation,
                "units": rows, "unit_count": len(rows), "build_elapsed_ms": self.build_elapsed_ms,
                "token_estimator": "utf8_bytes_div_4_ceiling", "query_budget_excludes_build": True}

    def query(self, query, *, conditions=None, domain_path=None, token_budget=800, include_unknown=False, limit=8):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be nonempty text")
        if conditions is not None and not isinstance(conditions, dict):
            raise ValueError("conditions must be an object of observed facts")
        if type(token_budget) is not int or not 1 <= token_budget <= 16000:
            raise ValueError("token_budget must be between 1 and 16000")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        started = time.perf_counter()
        terms = set(tokens(query))
        # Shared premises are evaluated once, while each child and inherited
        # rule still selects its own condition group independently.
        grouped = {key: match_conditions(required, conditions) for key, required in self.conditions.items()}
        applicability = {ident: grouped[key] for ident, key in self.condition_keys.items()}
        ranked = []
        rejected = Counter()
        for ident, unit in self.units.items():
            status = applicability[ident].status
            if status == "mismatch" or (status == "unknown" and not include_unknown):
                rejected[status] += 1
                continue
            score = 0.0
            count = self.counts[ident]
            for term in terms:
                frequency = count[term]
                if frequency:
                    inverse = math.log(1 + (len(self.units) - self.frequencies[term] + .5) / (self.frequencies[term] + .5))
                    score += inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * self.lengths[ident] / self.average))
            if score:
                preferred = bool(domain_path and (unit.metadata["domain_path"] == domain_path or unit.metadata["domain_path"].startswith(domain_path + "/")))
                ranked.append((score * (1.1 if preferred else 1), ident, preferred))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected = ranked[:limit]
        left = token_budget
        results = []
        for position, (score, ident, preferred) in enumerate(selected):
            if left < 1:
                break
            unit = self.units[ident]
            share = max(1, left // (len(selected) - position))
            content, used, truncated = _excerpt(unit.body, share)
            left -= used
            inherited, omitted = [], []
            for parent_id in self.closure[ident]:
                parent = self.units[parent_id]
                status = applicability[parent_id]
                if status.status != "matched":
                    omitted.append({"id": parent_id, **status.to_dict()})
                    continue
                # An inherited rule is injected only as a complete condition-bound
                # rule. A budget miss is visible, never silent truncation of a premise.
                cost = token_estimate(parent.body)
                if cost > left:
                    omitted.append({"id": parent_id, "status": "budget_excluded"})
                    continue
                left -= cost
                inherited.append({"id": parent_id, "content_sha256": parent.content_sha256,
                                  "revision": parent.revision, "content_digest": parent.metadata["content_digest"],
                                  "closure_digest": self.closure_digests[parent_id],
                                  "content": parent.body, "conditions": parent.metadata["conditions"],
                                  "tier": parent.metadata["tier"], "evidence_strength": parent.metadata["evidence_strength"],
                                  "provenance": parent.metadata["provenance"], "applicability": status.to_dict()})
            results.append({"id": ident, "content_sha256": unit.content_sha256, "title": unit.metadata["title"],
                            "revision": unit.revision, "content_digest": unit.metadata["content_digest"],
                            "closure_digest": self.closure_digests[ident],
                            "match_status": {"matched": "matched", "mismatch": "mismatched", "unknown": "condition_unknown"}[applicability[ident].status],
                            "missing_conditions": [reason.split(":", 1)[0] for reason in applicability[ident].reasons
                                                   if reason.endswith(("not supplied", "version not supplied"))],
                            "tier": unit.metadata["tier"], "category": unit.metadata["category"],
                            "evidence_strength": unit.metadata["evidence_strength"],
                            "domain_path": unit.metadata["domain_path"], "conditions": unit.metadata["conditions"],
                            "provenance": unit.metadata["provenance"], "applicability": applicability[ident].to_dict(),
                            "excerpt": content, "excerpt_truncated": truncated,
                            "score": score, "route": "preferred_domain" if preferred else "cross_directory",
                            "inherited_rules": inherited, "inheritance_excluded": omitted})
        return {"results": results, "generation": self.generation, "unit_count": len(self.units),
                "body_token_estimate": token_budget - left, "token_budget": token_budget,
                "budget_scope": "body excerpts and inherited rules; metadata excluded",
                "excluded_conditions": dict(rejected), "query_elapsed_ms": (time.perf_counter() - started) * 1000,
                "note": "Conditions must be established. Unknown conditions are not evidence of applicability; retrieval scores are not confidence."}
