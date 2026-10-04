# Local resource validation, 2026-10-05

These are synthetic implementation measurements on macOS 27 ARM64, Python
3.13.12. They do not establish scanner, model, network, Windows, hardware or
end-to-end consumer acceptance. Workload sizes and elapsed measurement windows
are developer probes, never product deadlines or body limits.

The comparison starts at `3e6046d` and measures the prerelease working tree based
on `f5d9b43`. The 128-task query and 128-block append cases were repeated after
the final material-lock change. Timings are individual observations, not a
statistical performance guarantee; deterministic work counts are release gates.

## Observed work and time

| Synthetic operation | Before | After |
| --- | --- | --- |
| Idle due-summary SQL, 20,000 completed rows | At least 80,000 SQLite VM steps | 18 VM steps |
| Summary admission SQL, same completed history | At least 120,000 VM steps | 15 VM steps |
| Same two SQL paths at 1,000 / 100,000 completed rows | Not both measured | 18 / 15 steps at each size |
| Warm rare query among 128 tasks | 128 manifests read, 128 blocks statted; 118–130 ms | No manifest/body reads, one selected manifest/block statted; 0.79–0.81 ms |
| Warm query matching all 128 tasks, five returned groups | 139 ms | 25.0 ms; five returned manifest/block pairs statted |
| One new 190-byte block, next query among 128 tasks | 138 ms; all manifests reread | 2.08 ms; exactly one block read and one checkpoint row changed |
| 128th append to one task | Five manifest reads totaling 219,690 bytes; 236 ms | One 43,734-byte manifest read; 56.3 ms |
| All 128 appends, 194,962 source bytes | 15.77 s elapsed / 15.60 s CPU | 3.82 s elapsed / 3.79 s CPU |

Both append implementations read no old block body. The improvement removes
repeated manifest parsing and whole-directory pointer rewrites; it does not
claim that the full current task manifest became constant-size. The final
append writes one 44,074-byte manifest, one 1,694-byte block file and a 56-byte
generation marker. At 512 blocks the prerelease probe still reads and writes one
manifest, approximately 174 KiB, and takes 230 ms for the final append. Metadata
cost remains proportional to the current task's block descriptors.

An earlier scale probe in this same implementation work measured rare warm
queries across 1 / 128 / 1,000 tasks at approximately 0.6 / 0.6 / 0.8 ms before
the final shared-lock serialization. It observed no body or manifest reads and
one selected block stat at every size. A query matching every task still has
real matching/grouping work: the 1,000-task case took 183 ms. A single new block
read and checkpoint row remained one at all three sizes. The deterministic
tests, rather than these timing samples, check that warm lookup does not scan
unrelated current entries.

## Storage and process interpretation

The current directory and ReMe checkpoint each add one SQLite database. Small
corpora therefore have a fixed storage cost. ReMe checkpoints compress each
current block's graph/chunks/term counts; one changed block updates one row
without rewriting the complete graph and lexical corpus. Deleted current
blocks are removed in the same checkpoint generation.

The 1 / 128 / 1,000-task scale cases used approximately 0.26 / 1.59 / 10.49 MB
after closing, including original current files, runtime metadata and the
derived index. Current Markdown bodies and required identity receipts remain;
these numbers are not a promise of constant total storage. The 100,000-history
case deliberately inserted its synthetic history in one transaction, so its
large WAL is not evidence of ordinary sustained WAL growth. WAL files disappear
on clean final close in these probes; allocated database pages are not live
historical bodies.

After closing, each query process returned to one Python thread, three open
descriptors and no children. RSS remained approximately 108–141 MB because the
same process still held imported libraries and allocator memory. A single
before/after sample cannot establish a memory leak or long-lived service idle
cost. Service shutdown, update-generation retention and full idle lifecycle
have separate acceptance paths.

## Failure and concurrency gates

`tests/test_incremental_resources.py` exercises the real summary polling and
admission functions with 1,000 and 20,000 completed history rows; together they
execute fewer than 200 VM steps. It also checks one-manifest append, no global
pointer enumeration, no unrelated warm-query directory walk, one-block
checkpoint updates, and restart without retokenizing or rechunking old bodies.

A second local writer updates an existing reader's current generation. An
explicit interleaving pauses a writer after the catalog transaction but before
the marker replacement; a reader waits for the same material owner lock instead
of repairing or serving that in-progress generation. A persisted exact pending
marker can be repaired after failure; an unrelated missing marker is an error.

Checkpoint-write failures and failures before checkpointing discard uncommitted
ReMe memory. The latter test deletes an old derived block, encounters a corrupt
replacement body, repairs that body and verifies that the next successful
checkpoint contains only the current replacement. Errors remain visible; stale
cache data is not labeled a successful refresh.

## Reproduction

From a checkout with its supported dependencies installed, plus developer-only
`psutil`:

```sh
PYTHONPATH=. python benchmarks/resource_paths.py history 20000
PYTHONPATH=. python benchmarks/resource_paths.py query 128
PYTHONPATH=. python benchmarks/resource_paths.py append 128
PYTHONPATH=. python -m pytest -q tests/test_incremental_resources.py
```

Each benchmark command creates and removes an isolated synthetic temporary
store. It makes no model call, reads no user transcript and changes no user
configuration. JSON output includes CPU, elapsed time, RSS, threads,
descriptors, child processes, authoritative file reads/writes, changed
checkpoint rows, storage and restart observations. Full-suite results and
cross-platform CI must be bound to the final committed revision separately.
