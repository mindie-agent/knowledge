# Reference retrieval measurements

Status: dated implementation evidence (2026-09-13); not a hardware guarantee.

These runners exercise ordinary files and the public query implementation.
They do not claim that repeated source text is a relevance benchmark or that a
memory vector fixture establishes native OpenViking performance. Native MCP,
OpenViking and FastEmbed acceptance is recorded separately by the integration
owner.

## Reproduce

From the package checkout:

```powershell
uv run --no-project python tests/performance/benchmark_legacy_lexical.py --output .mindie-local/performance/legacy.json
uv run --no-project python tests/performance/evaluate_reference_fixture.py --output .mindie-local/performance/retrieval.json
uv run --no-project python tests/performance/benchmark_reference_catalog.py --sizes 10000 100000 --queries 40 --output .mindie-local/performance/catalog.json
uv run --no-project python tests/performance/benchmark_capture_growth.py --output .mindie-local/performance/capture-growth.json
```

The capacity runner creates real temporary Markdown files from all 65 checked-in
VA corpus entries, adds unique markers, builds the actual catalog, changes one
file, verifies incremental reuse, and calls actual `query` and `maintain`.
It reports the initial incomplete query, build, scan, update, search, query,
output size, source reads, SQLite size and Python peak working set separately.
Its maintenance phase uses `MemoryBackend`; native embedding cost is excluded.
Temporary files are removed on completion. Large first builds take minutes.

## Observed CPU capacity

Windows 11 build 26200, Python 3.13.12, 32 logical CPUs; development host has
approximately 64 GiB RAM. The target remains 16 GiB without a discrete GPU.
These are measured process-memory requirements on the development host, not a
claim of testing a physical 16 GiB machine. Other implementation work was active
during measurements; these are not isolated laboratory timings.

| Measured phase | 10,000 notes, earlier final repeat | 100,000 notes, final maintenance path |
|---|---:|---:|
| Authored UTF-8 text | 39.38 MiB | 393.84 MiB |
| First incomplete public query | 137.61 ms | 132.41 ms |
| First offline build | 89.15 s | 856.47 s |
| Unchanged refresh | 0.946 s | 9.879 s |
| One-file changed refresh | 1.144 s | 8.135 s |
| Warm catalog p50 / p95 | 35.09 / 51.05 ms | 190.98 / 234.65 ms |
| Public query p50 / p95, vectors disabled | 56.52 / 73.59 ms | 201.53 / 269.32 ms |
| Maximum public query | 88.76 ms | 281.02 ms |
| Maximum successful original reads per query | 8 | 8 |
| Maximum response bytes | 13,120 | 13,120 |
| SQLite on disk | 181.97 MiB | 1.77 GiB |
| Peak Python working set before vector fixture | 78.62 MiB | 271.98 MiB |
| First `maintain(verify=True, force=True)`, memory vector fixture | 40.52 s | 543.71 s |
| Unchanged `maintain(force=True)`, memory vector fixture | 1.459 s | 9.805 s |
| Both maintenance calls returned ready | yes | yes |
| Peak working set including memory vector fixture | 195.59 MiB | 1.58 GiB |

The 10k warm series has 40 samples; the final 100k series has 20, over the same
identifier and NPU-domain query cycle. The
100k data contains repeated vocabulary, which makes broad queries match many
documents. Both unchanged refreshes read zero body bytes and parse zero notes;
the changed refreshes parse exactly one note. Windows newline conversion means
on-disk source bytes differ slightly from authored UTF-8 text.

The 10k p95 target of 100 ms passes for the catalog and the complete public
lexical route. The final 100k run is measured, not hidden behind a smaller
corpus: public lexical p95 is about 269 ms, and metadata scanning takes about
9.9 seconds. First builds and integrity audits belong to maintenance. The
catalog alone should be budgeted at approximately 0.5 GiB resident memory and
2 GiB disk for this 100k workload; repair temporarily retains an additional
complete catalog generation. Native vector storage and processes need their
own measured budget.

The final 100k run executed the implementation at
`95185f3cf71984a39c22f18d1caf14d56f1edc18`, including the force-wakeup/health
separation and vector delta fixes. It completed with exit 0. The seven recorded
core/runner file hashes and all 65 corpus hashes matched before and after the
run; later Git integration history did not alter these measured inputs.
Actual unchanged `maintain(force=True)` took **9.805 s** and returned ready.
The initial `verify=True, force=True` call took **543.71 s**, also ready. This
explicit initial integrity/reconciliation work remains expensive and runs
outside ordinary queries.

The earlier 100k run used the old forced health-audit behavior: initial
maintenance 239.31 s, unchanged forced maintenance 113.60 s, first catalog
build 776.92 s. Its artifact is retained; those are not final-path timings.
The new first build (856.47 s) and initial maintenance (543.71 s) were slower,
while unchanged maintenance was substantially shorter. These are separate
runs under concurrent development activity, not a matched repeated A/B
experiment or a general speedup claim. No native 100k embedding throughput or
physical 16 GiB host acceptance is implied.

Actual final command and retained local evidence:

```powershell
python tests/performance/benchmark_reference_catalog.py --sizes 100000 --queries 20 --output .mindie-local/performance/catalog-final-100k.json
```

The same directory holds `catalog-final-100k.log` and
`catalog-final-100k-provenance.json` with the source revision, runtime, input
hashes and exit status. The result SHA256 is
`c717ec3d637984c209dc0c3f7226d21ea9208d82f279eee41dac471c0dc73428`.
Earlier `catalog-scale-optimized.json` and `catalog-scale-final-10k.json`
remain historical source measurements.

## Scoring baseline and quality regression

The retained whole-corpus lexical algorithm, with enrichment cleared, took
median 12.30 ms for 65 notes, 191.61 ms for 1,000 and 2,020.84 ms for 10,000
over three samples. This excludes filesystem scanning and vectors. An earlier
design probe observed 15.29 / 232.60 / 2,953.74 ms; the runnable baseline makes
the workload and current timings reproducible instead of treating one probe as
an SLA.

The checked-in fixture contains ten authored VA domain notes and thirteen
queries: ACLGraph, HCCL, NIXL PD, Triton UB, Kimi K3/KDA, ACLNN dtype, HBM
attribution, NPU theoretical-versus-measured basis, version conditions and one
explicit no-evidence case. These are regression scenarios, not verified device
claims or independent held-out annotations. No VAWS startup/coordinator note
is used to establish business relevance.

| Same-fixture ablation | Without generated metadata | Source-bound aliases/topics |
|---|---:|---:|
| Scored questions | 12 | 12 |
| Recall@8 / MRR / nDCG | 0.75 / 0.75 / 0.75 | 1.00 / 1.00 / 1.00 |
| Checked source spans and hashes | 14/14 | 18/18 |
| Explicit no-evidence abstention | 1/1 | 1/1 |
| Query p95, vectors disabled | 11.14 ms | 11.72 ms |

This is an unenriched-catalog ablation, not a comparison against an old package
version. Query reports retain degraded/incomplete diagnostics because vectors
are deliberately unavailable. Missing labels stay unknown; repeated hits
cannot inflate nDCG. Tests separately cover stale aliases, malformed scopes,
table headers, code clipping, distant conditions and exact multi-span positions.

## Maintenance API and failure boundaries

- `refresh_catalog(config, force=False, extra_documents=None)` is the sole normal
  writer. `force` hashes existing sources, while unchanged fingerprints still
  avoid extraction. The report contains complete `changed_uris`, `deleted_uris`,
  `previous_snapshot`, `snapshot`, body reads, parses, counts and errors.
- A snapshot is an opaque `epoch:revision` string. A new database gets a new
  epoch; comparing just revision numbers is invalid. Vector receipt consumers
  must check both their indexed snapshot and the report's previous snapshot
  before consuming a delta. Imported shared vectors remain distribution-owned.
- `extra_documents`, when supplied, is a complete iterator of the active
  prepared shared collection. Replacement and retirement of old imported rows
  share one SQL transaction; any iterator failure rolls it back. `None` does
  not re-read shared material, and an empty iterator removes only imported
  derived rows. The release owner supplies source-validated, public-prepared
  documents and reuses an unchanged release pointer.
- `search_catalog` reads a snapshot; `get_catalog_document(s)` fetches selected
  references. `shared_current` lets search use the caller's single observed
  release pointer. Query validates returned originals. Topic preference does
  not hide cross-topic evidence; explicit maintenance `selection.mode=only`
  is a content filter, not authorization. `all-topics` and `topic:NAME` are
  optional text conveniences, not new ordinary tool parameters.
- `repair_catalog(config, extra_documents=None, limits=None)` explicitly builds
  a new bounded database and atomically replaces `reference-catalog.json`.
  Defaults are 100,000 notes, 1 GiB source bytes and 1,800 seconds. Existing
  databases/WAL files and active reader transactions are preserved. Failure or
  pointer contention leaves the old pointer active; the report says whether a
  switch occurred. Repair is never invoked by query or provider initialization.
  Old generations remain available as backups; storage is not silently deleted.
- `export_catalog` streams portable source-bound metadata and exact source
  context. It is **not** a public redaction boundary. Public callers build from
  the package-prepared copy and validate every field.
- `evaluate(config, cases, limit=8)` uses public query by default and supports
  exact-reference grades 0–4, unknown labels, no-evidence cases, evidence checks,
  categories, latency and output size. It does not certify source claims.

The ordinary MCP surface remains `knowledge_query`, `knowledge_explain` and
`knowledge_capture`. Normal query has a 20-result ceiling and defaults to 4,800
excerpt characters. Without a catalog, fallback has a 128-note / 1 MiB / 100 ms
scan budget and reports incomplete; it never builds an index. It also caps
directory entries at 1,024 across all mounts, including non-Markdown assets
and empty directories. The 1 MiB limit accounts for stat-observed successful
Markdown bodies; metadata has its separate per-file bound. Deadlines are
cooperative between filesystem operations. A read of one
reference is bounded at 4 MiB and its metadata at 256 KiB, including files that
grow after a stat call. Oversized/unavailable sources remain unknown and intact.

## Capture growth acceptance

`capture-growth-final.json` exercised 64 and 1,024 real candidate files with
MemoryBackend, Python 3.13.12 on Windows and instrumented file opens. Six
implementation/runner hashes matched before and after. Cold summary capture
read 8/18 old bodies and took 45.968/35.637 ms including saving. Cold title
lookup stops at 32 notes, 256 directory entries, 512 KiB or a cooperative
25 ms lookup deadline. That deadline is not a hard whole-capture limit.
Ten warm captures at each size read zero unrelated Markdown bodies, with
p50 17.946/20.582 ms and maxima 24.761/23.989 ms. Ten one-result queries at
each size read ten original bodies total, with p50 6.383/8.003 ms.

The existing catalog supplies an exact-title index created by maintenance.
Capture never scans the full corpus or builds that index; a missing/old catalog
uses only the bounded compatibility lookup. Unobserved manual renames may leave
title matching incomplete and are reported without overwriting arbitrary old
notes. These smaller growth runs complement, rather than replace, the separately
pinned 100k and native embedding measurements.
