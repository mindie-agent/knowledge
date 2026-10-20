"""[Role] JSON projection of validated C5 units and their BM25 token counts.

[Invariants] The catalog binds the projection by its generation. Loading trusts
the installed projection/body and does not re-read source blobs on query.
[Timeouts & Locks] No locks or subprocesses; the repository owns SQLite atomicity.
[Side-Effect Policy] Reads immutable blobs through the repository; never writes.
"""
from __future__ import annotations

import json

from .index import RoutingIndex
from .units import GradedUnit, canonical

# Change this schema when serialized fields or token expansion semantics change.
SCHEMA = "mindie-graded-index/1"


def dump_index(index):
    return canonical({"schema": SCHEMA, "generation": index.generation, "units": [
        {"metadata": unit.metadata, "body": unit.body, "path": unit.path,
         "counts": dict(index.counts[ident])}
        for ident, unit in index.units.items()
    ]})


def load_index(raw, catalog):
    if raw is None:
        raise ValueError("declared graded index is missing")
    data = json.loads(raw)
    if data["schema"] != SCHEMA:
        raise ValueError("unsupported graded index schema")
    if data["generation"] != catalog["generation"]:
        raise ValueError("graded index generation mismatch")
    descriptors = {item["id"]: item for item in catalog["units"]}
    if len(descriptors) != len(catalog["units"]):
        raise ValueError("duplicate graded catalog unit")
    units, counts = [], {}
    for row in data["units"]:
        metadata = row["metadata"]
        unit = GradedUnit.from_parts(metadata, row["body"], row["path"])
        item = descriptors.get(unit.id)
        if item is None or unit.id in counts:
            raise ValueError("graded index unit set differs from catalog")
        if unit.path != item["path"] or metadata.get("revision") != item["revision"]:
            raise ValueError("graded index and catalog content disagree")
        frequencies = row["counts"]
        if not isinstance(frequencies, dict) or any(
                not isinstance(term, str) or type(value) is not int or value < 1
                for term, value in frequencies.items()):
            raise ValueError("graded index token frequencies are invalid")
        units.append(unit)
        counts[unit.id] = frequencies
    if set(counts) != set(descriptors):
        raise ValueError("graded index unit set differs from catalog")
    index = RoutingIndex(units, counts=counts)
    if index.generation != catalog["generation"]:
        raise ValueError("graded catalog generation mismatch")
    return index
