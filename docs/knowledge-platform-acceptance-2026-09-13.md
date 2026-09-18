# Knowledge platform acceptance — 2026-09-13

Status: dated implementation and installed retrieval acceptance through package 778a7b6b, 2026-09-13; later parser compatibility and final consumer selection tracked separately

This records actual execution for the [capability plan](knowledge-platform-plan-2026-09-13.md).
The library serves vLLM, vllm-ascend, NPU, AI and infrastructure development.
VAWS engineering validation is archived separately in the consumer repository.
Ordinary tasks still have only `knowledge_query`, `knowledge_explain` and
`knowledge_capture`; none of the maintenance commands below is a task gate.

Early runs used the implementation worktree while changes were being integrated.
The previous package baseline is `e16d87287f7db51ca96efc945a9514e4146131d6`;
it is not the source revision of every measured run. Final implementation
commits and installed package pins are recorded separately from these measurements.
A response's source metadata or an older CI artifact cannot supply a missing
execution identity.

The retrieval/distribution change merged as [PR 30](https://github.com/mindie-agent/knowledge/pull/30),
commit `4bdc37571e0ddb9c7312aabf64eaa22aabea2b9d`; curation, source maps and
version 0.7.0 merged as [PR 31](https://github.com/mindie-agent/knowledge/pull/31),
commit `24b652de235bae9ecfd08bbc29048cf7b59b4e90`. Local committed snapshots
`7105a2c` and `95185f3` passed 439 and 501 tests respectively, each with four
skips and twelve passed subtests. Final PR-head CI passed Linux, Windows and
macOS. Those CI runs checked GitHub's merge-test refs, distinct from the final
squash commits; the consumer validation archive preserves both identities.

Final hardening and fusion merged in
[PR 33](https://github.com/mindie-agent/knowledge/pull/33),
commit `778a7b6b6a6f1b8c541dbdc6a646bc033f5e2c0f`. Its final PR head was
`3a2e8707eed610f681f7941c0845353f8f46df02`. All three jobs checked GitHub's
merge-test ref `cea65a73ea1462a3b0c64cb07d195b61ad70694d`; Git API verification
confirmed the tested and consumed commits have the same parents and tree
`0e855733d5b269e43df6b92258d4ea01bfbacf73`.

| Final package check | Passed | Skipped | Passed subtests | Pytest elapsed |
|---|---:|---:|---:|---:|
| [Ubuntu / Python 3.11](https://github.com/mindie-agent/knowledge/actions/runs/34747646848) | 531 | 4 | 12 | 20.50 s |
| [Windows / Python 3.13](https://github.com/mindie-agent/knowledge/actions/runs/34747646837/job/103698412064) | 533 | 2 | 12 | 220.61 s |
| [macOS / Python 3.13](https://github.com/mindie-agent/knowledge/actions/runs/34747646837/job/103698412104) | 533 | 2 | 12 | 165.68 s |

Both native JUnit hashes were reread and matched; each contains 535 testcase
elements including two skips, while the suite reports 547 tests including
twelve subtests. Both have zero failures/errors. Ubuntu retained its actual
pytest log and did not emit JUnit. Native model-cache-dependent distribution
coverage was skipped where its required cache was absent; the independently
executed native chain below remains its own evidence. Private final CI records
are under `.vaws-local/hardening-ci/`, including `acceptance.json`, logs,
JUnit and `tested-and-merged-trees.json`.

## Executed source and media intake: K01, K02, K08

The independently installable [intake tool](../tools/knowledge-intake/README.md)
has no `vaws_knowledge` import or dependency. Base Markdown/text/HTML, URL,
Git and community-PR use needs no format-parser dependency. PDF/Office/image
parsers are optional extras owned by this tool. Outputs are ordinary Markdown,
retained original assets and optional source/evidence sidecars consumed by a
normal knowledge mount.

On Windows, Python 3.12.14 with installed PDFium 5.13.0, python-docx 1.2.0,
python-pptx 1.0.2, openpyxl 3.1.5 and Pillow 12.3.0 executed eight synthetic
fixtures: Markdown, HTML, DOCX, PPTX, XLSX, text PDF, PNG chart and scanned PDF.
ReportLab 4.4.9 created the test PDF. The conversion processed 144,746 source
bytes in 3.687 s; repeat took 0.047 s with zero conversions and eight unchanged
items. The local suite discovered 20 tests: 19 passed, one optional Tesseract
capability was skipped because it was absent, in 9.917 s.

Actual Windows OCR recognized `HCCL graph replay 910B` from both an image and a
rendered scan page. A separate native Codex Agent actually viewed the chart,
then returned a description bound to the request and original image hashes.
The tool accepted it in 0.844 s and repeated with zero conversions. The chart
has synthetic 910B/910C bars at 120/180 tokens/s and unspecified batch/software
conditions. These are labelled fixture values, not a hardware comparison. Its
request/result identity is
`caa0d6dc5c00b613b1bd8d32269063d1fbf0f336ff04b90e00dc977e870c243b`.
The original image and page remain linked from the searchable description.

The final intake mount contains ten notes: eight format outputs, the accepted
native-caption output and a real PR snapshot. Before the PR was added, actual
catalog refresh of the nine-note mount took 26.909 ms; public query/explain
returned the caption with `native_caption` evidence and original-image hash.
That integration deliberately disabled vectors and retained `degraded=true`.

The tool actually fetched [vllm-ascend PR 16157](https://github.com/vllm-project/vllm-ascend/pull/16157)
at base `fb2820b9b6598f888d7dc56260c03d3352cc08e9`, head
`debfa9bd48fefcc90added07b28540daee8b5435`, observed updated timestamp
`2026-09-09T09:24:15Z`. The snapshot recorded five changed files, API patch
excerpts and discussions. Initial/repeat times were 4.579/4.343 s with zero
repeat conversion. A README GitHub snapshot at the same base used 50 bounded
tree entries and 60,564 bytes in 3.110 s; a direct raw URL imported 10,056 bytes
in 0.828 s. The PR was open/WIP at observation; fetching it does not validate
its behavior. The shipped PR adapter accepts only the two vllm-project repos.

Tests execute real local HTTP/Git, slow streaming HTTP deadline termination,
interrupted body/sidecar recovery, unchanged reuse, edited-output preservation,
symlink boundaries, page/archive/byte/output limits and native result hash/ref
rejection. Successful cursors advance only after complete work. Embedded
Office images and multi-frame images can produce explicit partial results;
arbitrary production documents and all platforms are not certified by these
fixtures. No OCR/model installation is performed by capability discovery.

Source and wheel builds passed. An isolated fresh environment installed the
wheel with no dependencies and performed real Markdown conversion followed by
zero-conversion repeat; imports confirmed no VAWS or document parser package.
Independent [CI 34746454502](https://github.com/mindie-agent/knowledge/actions/runs/34746454502)
passed Ubuntu Python 3.11/3.13 and Windows/macOS Python 3.13. Its actual checkout
was `2a135380f4cb77aba83316cff5df85454acf8c07`, GitHub's merge-test ref for
PR head `b3eb681ef3ca5d20fb66ed56113b6968d3965cdc` and base `24b652de`.
The 45-test matrix passed 41/41/43/42 tests with 4/4/2/3 capability skips
respectively. Native Windows OCR and macOS RSS-limit tests executed on their
own hosts; absent Tesseract and foreign-platform checks stayed skipped.
Each job built and installed the independent wheel, checked both CLI entries,
and proved that VAWS was absent from the isolated environment. macOS RSS is
sampled every 10 ms, so a short overshoot is possible; Windows/Linux retain
their operating-system process limits. The independent tool merged in
[PR 32](https://github.com/mindie-agent/knowledge/pull/32) at
`52754b5e5a714b9c60b4fa840daba5ecde97cb37` after all seven package/native/intake
checks passed.

Private evidence: `.vaws-local/knowledge-intake-acceptance/` contains
`formats-result.json`, `public-result.json`, `native-result.json`,
`retrieval-result.json`, `native-requests/` and the final `final/output/` mount.
The tool's [detailed acceptance](../tools/knowledge-intake/ACCEPTANCE-2026-09-13.md)
records fixture and worker limitations.

## Actual Grok Bot research and adoption: K02, K03, K06, K10, K11

This was the user's Grok Bot with a cloud computer, distinct from Grok Build
CLI. The returned run manifest records actual GitHub CLI/API fetches of PR
16157 and its pinned head sources, plus six selected existing Ascend peak and
matmul notes at the earlier corpus revision. The agent produced a PR topic,
a reusable case, a corpus navigation page and a dated maintenance digest.
Each has five proposed retrieval questions; application retained twenty
source-bound aliases with the body hash and five VA-domain topic labels.

The downloaded archive SHA256 is
`bdf8f22fca3421a67b8d88239344c9eef4269ac7515068eae27287b0ffb58c67`.
The package applied the four selected Markdown outputs under an independent
curation job and retained its input manifest and application history. Catalog
refresh then parsed fourteen imported/generated notes in 51.919 ms. Actual
queries found the K3/DSpark topic, the Ascend peak-versus-measurement navigation
page and the maintenance material. The initial adoption queries used lexical
retrieval and correctly reported unavailable vectors; the later native MCP
run below exercised both lexical and vector retrieval.

The revised PR analysis distinguishes dedicated K3 branch gates from the
broader graph switch: the claim that every non-K3 model is necessarily eager
is not established for DeepSeek V4 by these inspected assignments. The
generated material retains this uncertainty and open PR status. No unit test,
NPU, full inference or production-correctness result was manufactured.

The six existing corpus notes did not establish a useful direct link to this
PR. Grok recorded that gap instead of inventing an association. The digest
covers the actual bounded research run; this is one executed interval, not
evidence of multiple completed daily or weekly cycles.

After the user completed GitHub login on the cloud computer, Grok installed
the fixed public exporter with only PyYAML and `--no-deps`, exported and
verified the four notes, and published its personal reference branch. The
observed feed commit is `4ca634c8d0e54f07d831feffaaec633adb5eca9b`, generation
`a79b2a01d444478c9a1dcb573a6da883`, manifest SHA256
`5b66c3ce0032d43a3ff53cdb0110d69dd836469ccf4d5166e485a704d5a82ce7`.
The Git tree contains exactly ten ordinary files: current pointer, prepared
manifest, four Markdown notes and four permitted metadata sidecars. No raw
snapshots, original private sidecars or run archives were published. Repeating
the verified export returned unchanged and created no second commit.

Both saved Grok routines were read in the actual UI: daily PR maintenance at
09:00 Asia/Shanghai (20-minute maximum), and weekly topic/digest maintenance
on Saturday at 10:00 (30-minute maximum). They retain public-source bounds,
source revisions and uncertainty, verified personal-feed publication, and
silence on unchanged runs. The routines were active with no scheduled runs
yet at observation; the manually dispatched research/export runs above did
execute. Future recurring execution is not claimed as completed evidence.

An independently installed reader imported the live GitHub feed in 14.266 s;
unchanged replay took 1.140 s with zero downloaded source blobs and no changed
file hashes or timestamps. A separate 80-note export/Git/reader test completed
in 12.467 s; changing one note took 2.211 s and read only the pointer, manifest
and two changed content files, reusing the other 158. The exporter tested
1,024 notes and retained the old generation on overflow. The 32 MiB / 1,024
note capacity is a bound, not proof that a remote cold import of the maximum
corpus can finish within its default 120-second transport budget.

The fixed cloud exporter and both saved routines were subsequently upgraded
to `cc1a1d76fdee2c90b129f6ba56c171d500c158af`. Actual export/verify remained
unchanged at the same manifest and feed commit. The local independent reader
was installed in its persistent environment with source `b3eb681`, wheel SHA256
`f346bcbdb1d7973fca19b550dbedfed9791282ba6af628e9d5f8330794512e5e`.
A real per-user Windows task has hourly and logon triggers, uses windowless
Python with isolated imports and least privilege, and requires the user to be
logged on. Installation repeated as unchanged. Its first actual trigger found
no terminal-provided GitHub authentication, retained the old notes and failed;
the shipped public-API fallback fixed this without copying credentials.
A manual task trigger then succeeded in 1.304 s. The natural 16:00 hourly
trigger also succeeded in 1.357 s, both with zero source downloads. Thus local
scheduled execution, as well as manual cloud export and live Git transport,
has executed; a future scheduled Grok run has not yet elapsed.

The generated Markdown directory is a second project mount in the real
knowledge configuration. Both primary and prepared-workspace refreshes retained
that custom mount and the existing roots. The transport itself does not start
knowledge or model services.

Private evidence: `.vaws-local/grok-maintenance-20260913/adoption.json`,
`downloaded/meta/sources-2026-09-13.json`,
`downloaded/meta/run-summary-2026-09-13.md`, the four `selected/` Markdown
outputs, their adopted `candidate/grok-va/` metadata, and the private curation
job history. These artifacts contain local details and are not copied into
public contributions.

## Source maps and associations: K04, K05

A pinned real-source run inspected `csrc/torch_binding.cpp`,
`csrc/torch_binding_meta.cpp` and `vllm_ascend/ops/layernorm.py` at
`b36dc06d8e1b914e7a1318ee32310ef1d502007a`. Three files / 83,590 bytes parsed
in 391 ms. Repeat reused all three with zero source bytes read and zero parses
in 235 ms. Navigation connects the Python `torch.ops` reference, Torch schema
and registrations with line/hash/revision evidence. Dynamic names and
unresolved calls remain explicitly static or unknown.

A separate full-tree working-copy run discovered 1,371 files and read
12,267,673 bytes in 11.438 s. Repeat reused all 1,371 parse results in 5.938 s;
it still read working-copy bytes to determine their current hashes. This run
observed the same HEAD but uses actual working-copy hashes, not a claim of an
immutable Git snapshot. Its result is partial: unexpanded macros, parse errors
and static-analysis scope remain visible. No compiler/preprocessor evaluation,
runtime dispatch, NPU compatibility or program correctness is asserted.

Real maintenance CLI integration edited a Python function, wrote before/after
maps and a change artifact, then identified the Markdown document that linked
to that file. The test uses a real file URI, including the Windows drive
conversion. Separate checks preserve remote-host URI semantics without network
probing. Source associations are a worklist for judgment and never silently
invalidate or rewrite the linked claim.

Private evidence: `.vaws-local/code-acceptance/live-gemma-acceptance.json` and
`live-full-result.json`. Regressions reside in `tests/test_code_map.py`,
`tests/test_relations.py` and `tests/test_reference_cli.py`.

## Retrieval, cost and shared releases: K07, K09, K12

The [reproducible measurement report](../tests/performance/README.md) separates
authored quality fixtures, repeated-text capacity, native processes and
maintenance. The checked-in VA fixture has ten notes and thirteen queries;
twelve carry relevance grades and one explicitly expects no evidence.
Removing source-bound enrichment produced recall/MRR/nDCG of 0.75; retaining
aliases/topics produced 1.00 on the same authored fixture. Exact source/hash
checks were 14/14 and 18/18; the no-evidence query returned no hit in both.
This is a metadata ablation, not an old-version comparison or held-out study.
Vectors were disabled and degraded diagnostics remain in the outputs.

| Actual Windows CPU phase | 10,000-note earlier final repeat | 100,000-note final maintenance path |
|---|---:|---:|
| First offline catalog build | 89.15 s | 856.47 s |
| Unchanged refresh | 0.946 s | 9.879 s |
| One changed note refresh | 1.144 s | 8.135 s |
| Public lexical query p50 / p95 | 56.52 / 73.59 ms | 201.53 / 269.32 ms |
| Maximum query original reads | 8 | 8 |
| Maximum response | 13,120 bytes | 13,120 bytes |
| Catalog disk | 181.97 MiB | 1.77 GiB |
| Python peak working set before vector fixture | 78.62 MiB | 271.98 MiB |
| Initial verified maintenance / unchanged forced maintenance | 40.52 / 1.459 s | 543.71 / 9.805 s |
| Both maintenance calls returned ready | yes | yes |
| Peak working set including memory vector fixture | 195.59 MiB | 1.58 GiB |

The 10k warm series contains forty queries and the final 100k series twenty.
Unchanged refresh read zero body
bytes and reparsed zero notes; the changed refresh parsed one. The 10k p95
target of 100 ms passed. The larger corpus repeats real vocabulary, tests
capacity rather than relevance, and used a development host with about 64 GiB
RAM. The intended 16 GiB Windows/no-GPU host is a resource target, not a
physically tested machine configuration. Initial build and integrity work stay
outside query. The final 100k path executed at implementation
`95185f3cf71984a39c22f18d1caf14d56f1edc18`; seven core/runner file hashes and
65 corpus hashes were unchanged when the process exited successfully. Its
unchanged forced maintenance took **9.805 s**, with ready true. Initial
verified maintenance took **543.71 s**, also ready, and remains substantial
offline work. The earlier 100k run measured 113.60 s unchanged maintenance
with the old forced health audit, 239.31 s initial maintenance and 776.92 s
first build. The final initial maintenance and first build were slower. These
separate runs under concurrent activity are not a controlled A/B or universal
speedup claim. The maintenance phase uses a memory vector fixture; native
100k embedding throughput and a physical 16 GiB host remain unmeasured.

Actual NDJSON MCP stdio initialization exposed exactly three tools in
739.52 ms without creating service state. The active service initialized in
703.23 ms; first query took 1,485.07 ms. Fourteen warm queries measured
p50 42.79 ms / p95 45.70 ms / max 61.62 ms, with both lexical and vector hits.
Explain took 8.41 ms, capture 15.21 ms and maximum output was 10,438 bytes.
The capture body is explicitly a local connectivity fixture. These figures
cover fourteen source/generated notes and CPU FastEmbed/OpenViking, not 100k
native vectors or a guarantee for every workstation.

The subsequent actual process-tree measurement counted OpenViking RSS about
331.02 MiB and embedding RSS about 634.39 MiB; private committed sums were
about 289.98 MiB and 603.68 MiB. RSS sums may double-count shared pages. This
includes real Python children and consoles, not only a small launcher process.
The earlier MCP report could not import `psutil`; the separate process-tree
artifact supplies these observations. No local multimodal model was loaded.

The native small-sample distribution chain built two Git-versioned releases,
imported existing vectors without text embedding calls, retained aliases and
topics, switched the source pointer, rejected old-version alias results before
catalog refresh, and survived target restart while preserving candidate notes.
The integration owner recorded one passing test in 27.85 s; retained source,
release, catalog and native log artifacts support that execution scope. A
separate actual 10k shared-reference stream prepared in 77.431 s, streamed in
26.963 s with 64.01 MiB Python allocation peak, and built its catalog in
23.865 s. Query median/p95 was 6.242/8.960 ms. Its vectors are synthetic fixtures;
these stream/cost numbers must not be described as native embedding performance.

Private evidence: `.vaws-local/performance/catalog-scale-final-10k.json`,
`catalog-final-100k.json`, `catalog-final-100k-provenance.json`,
`catalog-final-100k.log`, historical `catalog-scale-optimized.json`,
`domain-retrieval-evaluation.json`,
`.vaws-local/grok-maintenance-20260913/native-mcp-acceptance.json`,
`native-process-memory.json`,
`.vaws-local/distribution-reference-native/test_native_build_release_sync0/`
and `.vaws-local/shared-reference-scale-10k/result.json`.

## Maintainer commands and engineering archive: K09, K13

The first installed real-project probe at `0.7.0 / 52754b5` returned the
designated page for 17 of twenty source-bound questions within eight results.
All four imported pages were retrievable, all four explanations matched their
originals, and seventeen checked citation spans matched. The three misses
were not lost metadata: their designated pages ranked 1, 1 and 2 in the
existing lexical catalog. Flat reciprocal rank fusion let many weak shared
matches from both routes displace stronger lexical-only evidence. The failure
is retained in `provisional-52754-native-mcp.json` and the read-only catalog
analysis; this run is not labelled a passing final acceptance.

The revised fusion retains each route's strongest rank vote and weights its
additional agreement vote by relative positive lexical strength within that
query. It never compares raw vector and lexical score units, adds no I/O or
model call, and preserves single-route order. Four focused old-algorithm
failures now pass, including public query with and without a catalog and
continued access to a vector-only reference. The associated group passed
64 tests and three subtests. Thirteen authored question cases retain the
0.75/1.00 metadata ablation; populated MemoryBackend old/new fusion both
score 1.00 on that fixture. These fixture runs report incomplete maintenance
state and establish ranking/evidence behavior, not native readiness or
held-out quality. The final installed-project run below used the original
twenty questions and retained this failed provisional report unchanged.

A final growth audit found two unbounded compatibility paths: cold query
could traverse non-Markdown files/empty directories without consuming its
budget, and each Stop summary could reread every candidate twice while
resolving its title. Cold fallback now counts directory entries across all
mounts. Capture first uses its deterministic path and the existing maintained
catalog's title index, then a compatibility scan capped at 32 notes, 256
entries, 512 KiB and a 25 ms cooperative lookup budget. Capture does not build
or upgrade that index. Original documents validate observed title candidates;
an unobserved manual rename can leave lookup incomplete, which is reported
while existing files are preserved. Native summaries reuse the single lookup.

The retained `capture-growth-final.json` records 64 and 1,024 real old files,
with matching before/after hashes of six implementation/runner inputs.
Cold summary capture read 8/18 old Markdown files and took 45.968/35.637 ms
including saving; the lookup deadline is checked between filesystem calls,
not a hard whole-capture deadline. Ten warm captures per corpus read zero
unrelated Markdown bodies, with p50 17.946/20.582 ms and maxima
24.761/23.989 ms. Ten queries per corpus read ten selected bodies total,
with p50 6.383/8.003 ms. This uses an instrumented MemoryBackend on Windows
Python 3.13.12; it does not replace the separately pinned 100k or native-model
measurements. Existing private summaries remain usable without new domain
filtering, per-task reporting or a generative classifier.

Another regression exercised an old shared pack whose Git revision stays
unchanged while verification first prepares its Markdown reference source.
The same maintenance call now aligns that new pointer with the catalog and
local snapshot: 92 local/bootstrap documents become 94 with the two shared
notes, without reimporting shared vectors. A second unchanged call performs
one catalog refresh with zero body reads and reuses local vectors. The
maintenance/distribution regression group passed 70 tests and two subtests.

Existing target and orphan metadata reads also obey the per-file bounds
before writing. Oversized or differently titled secondary collision targets
are preserved. Three regressions first reproduced the old failures, then the
Markdown/capture/summary group passed thirty tests and four subtests. Saving
returns the already rendered body and metadata without rereading its files.

Fifteen actual subprocess regressions passed in 7.79 s for the reference CLI.
An explicit missing or malformed configuration fails before work. Evaluation
accepts 1–500 question objects, at most 4,000 characters per query and 8 MiB
input. Default total time is 120 s, checked before each real query; an active
synchronous query may finish. A stopped run retains completed results and
never scores unexecuted questions as negatives. Full reports are written under
private state and stdout is bounded to 16 KiB. Maps, change evidence and
relation reports also retain full private artifacts. Busy/partial catalog
results return nonzero with `ready=false`.

These are explicit maintainer operations, available through the installed
package's normal help; they add no new ordinary Agent reporting:

```sh
python -m vaws_knowledge catalog --config CONFIG
python -m vaws_knowledge evaluate --config CONFIG --cases CASES
python -m vaws_knowledge code-map --root SOURCE --state STATE
python -m vaws_knowledge relations --config CONFIG --ref REFERENCE --code-map MAP
```

The consumer's separate `docs/validation/` archive maps 26 families: four
runtime owners, four consumer/support families and eighteen current business
skills. Its checked index contains thirteen versioned evidence records and five
explicit applicability gaps. The bounded read-only checker verifies coverage, source/test
references, artifact containment, hashes and XML counts. Its negative tests
actually reject tampered JUnit and escaped artifact paths.

An integrity audit reused ninety existing local suites: 2,187 JUnit entries
including subtests, six skips, zero failures/errors; all recorded JUnit/log
hashes were reread and checked. The original summary lacks exact source and
loaded runtime revisions, so this is historical evidence, not a fresh final-pin
run. Existing final coordinator/consumer behavior, older measured timings,
source-workspace preparation and business device cases retain their actual
separate revisions in that archive. No unrelated NPU run was repeated merely
to fill the archive. None of these VAWS engineering records is imported into
the default VA-domain knowledge library.

## Installed consumer acceptance at 778a7b6b

The selected immutable environment actually installed package `0.7.0` at
`778a7b6b6a6f1b8c541dbdc6a646bc033f5e2c0f`. An isolated Python probe verified
distribution metadata, its VCS revision and loaded package hashes; it did not
import the component worktree. The real project configuration retained its
ordinary notes and the additional Grok feed mount. Completed package-owner
preparation returned ready in 27.720 s with 94 catalog documents, aligned
catalog/local snapshots and shared prepared-source identity. The consumer's
separate upgrade record preserves an initial busy result and the bounded
retirement of obsolete lock-owning MCP processes; those observations are not
misreported as successful preparation or a new ordinary-task requirement.

The single final installed native MCP run passed the unchanged strict test:
twenty of twenty source-bound questions returned their designated originals
within eight results; all four pages were found and all four explanations
matched original bodies/hashes. Twenty selected citation spans passed exact
line, column, text and hash verification. Both lexical and vector retrieval
participated, with no degraded or incomplete responses and exactly three tools.
The original three misses now ranked fourth, third and second respectively.
Exact-source recall was 1.00; other returned references were retained but not
independently labelled or graded for relevance.

Initialize took 796.642 ms and first query 1,583.029 ms. Nineteen warm queries
measured p50 69.020 ms, p95 85.843 ms and maximum 87.699 ms. Maximum query
structured content was 15,292 bytes; the full MCP result also carries a text
representation and reached 35,362 bytes. These are serialization sizes, not
token counts. This real 94-document run is separate from the older fourteen-
note native experiment and the separately pinned 100k capacity fixture.

The test called only query and explain: no capture, explicit maintenance,
source/configuration edit or backend stop. Eight feed files, configuration,
installed package inputs and existing backend PIDs remained unchanged. Its
own MCP process exited normally. Both before and after, the catalog held 94
documents at the same snapshot and shared Git revision
`f21e048537a9f6de52760fad97fccb5c4da32be2`, with the same verified prepared
manifest. The complete report is
`.vaws-local/grok-maintenance-20260913/final-installed-native-mcp.json`, SHA256
`84f23b330b0b3bdbacfb50581a87a8674ab3bec88206ff9e854ff5203d42376e`.

Consumer functional head `52b3bec5d17371447c6902ef1c0c24a011e17836` passed
[CI 34748066882](https://github.com/mindie-agent/mindie-agent/actions/runs/34748066882)
on Ubuntu/Python 3.11, Windows/Python 3.13 and macOS/Python 3.13. Job wall
times were 82, 613 and 200 seconds respectively. Final consumer deployment,
affected suites and upgrade provenance are carried by the rolling consumer
`docs/knowledge-platform-validation-2026-09-13.md` record in
[PR 169](https://github.com/mindie-agent/mindie-agent/pull/169).
The [published 92d61a36 record](https://github.com/mindie-agent/mindie-agent/blob/92d61a365d5a4dd6884322114520e77a2eaa772e/docs/knowledge-platform-validation-2026-09-13.md)
preserves this earlier installation stage.
[PR 169](https://github.com/mindie-agent/mindie-agent/pull/169)
was open at this cutoff. Its subsequent documentation head
`92d61a365d5a4dd6884322114520e77a2eaa772e` had
[CI 34748635798](https://github.com/mindie-agent/mindie-agent/actions/runs/34748635798)
still running; the PR records that head's checks and merge state. Neither this
package record nor the earlier functional CI asserts it has passed or merged.

A subsequent installed C++ CLI probe exposed a separate native parser failure
(process exit `3221225477`) in a base environment containing Tree-sitter 0.26.0
with the C++ grammar 0.23.4. This change checks distribution metadata for the
supported Tree-sitter 0.25.2 / C++ grammar 0.23.4 pair before importing either
native extension. Unknown, missing or malformed version metadata returns a
`parser_unavailable` gap; the map is partial and the last complete graph is
retained. This boundary is necessary because a native process fault cannot be
handled by a Python exception around parser construction.

The installed 778a7b6b package with the declared `[code]` extra actually mapped
three real files totaling 83,590 bytes at source revision
`b36dc06d8e1b914e7a1318ee32310ef1d502007a`. It returned `ready`,
`source_complete=true` and `complete=true`, while retaining two static-analysis
gaps. Mapping took 419.650 ms; the complete CLI process took 679.983 ms and
exited zero. The consumer's private `code-map-supported-probe.json` retains
this result; `code-map-native-probe.json` retains the original crash. This
supported dependency selection is real parsing evidence, distinct from the
later guard's release CI and final consumed package revision.

The full CLI was also executed in the original unsupported 0.26.0/0.23.4
environment, launching the installed 778a7b6b distribution with an **explicit
working-source override** for this guard. It read the actual 51,192-byte
`csrc/torch_binding.cpp` at the same revision and exited 2 with `partial`,
`source_complete=true`, `complete=false` and one `parser_unavailable` gap.
The process confirmed `native_modules_loaded=False`; internal/whole-process
times were 285.350/471.992 ms. This proves the guarded source behavior in that
environment, not installation of a later release. The private consumer
`guard-code-map-cli-probe.json` binds the result to the two loaded source hashes.

Only the C++ fingerprint adds `native-version-policy-1`. A regression first
reproduced an old successful cache bypassing the new check; the corrected path
returns partial and preserves the last complete graph while reusing Python
parses. Restoring the supported pair reused both unchanged files with zero
body reads. The code-map/reference-CLI/relations group passed 48 tests in
15.92 s. Private `cpp-guard-cache-before.xml` retains the original failing
regression and `cpp-guard-final-related.xml` records the passing group.

The 778a7b6b twenty-query result remains valid for its recorded retrieval
inputs. The version-guard release and final consumer pin/checks belong to the
rolling consumer delivery record; neither is certified by the old MCP run.

## Delivery record and measurement boundaries

- K01–K13 implementation has the scoped evidence above: independent intake CI,
  package CI through PR 33, actual Grok export/live feed/local hourly execution
  and installed retrieval acceptance. The later parser-compatibility correction
  and final consumer selection require their own exact integration record in
  PR 169; they are not certified by the earlier successful retrieval run.
- The next scheduled Grok routine had not elapsed at the observation cutoff;
  its saved schedule is not presented as a completed future run.
- Scope limits remain explicit: authored query fixtures, synthetic scale
  vectors/chart values, partial whole-tree C++ mapping, a 64 GiB development
  host, native vision supplied by Codex, and no new hardware correctness claim.
- An earlier `.vaws-local/platform-tests-20260913.xml` contains a failing
  exploratory test and remains separate. The exact final aggregate CI above
  supplies final-version validation; that old result is not relabelled passing.

Private artifacts remain local. Public evidence includes no host coordinates,
user paths or credentials; public contribution still uses the package's
prepared redacted copy. None of these acceptance records replaces current
source evidence or an Agent's judgment.
