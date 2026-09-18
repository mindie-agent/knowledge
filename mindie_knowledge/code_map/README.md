# Static code and reference APIs

These APIs run only for explicit source inspection/maintenance. They never fetch,
execute inspected code, start an index/model, or edit knowledge. Importing the
modules starts no work. The ordinary three knowledge tools need no extra calls.

```python
from mindie_knowledge.code_map import build_code_map, navigate, compare_maps

graph = build_code_map(
    source_root, state_root,
    revision=commit_or_none,
    paths=["vllm_ascend/ops/layernorm.py", "csrc/torch_binding.cpp"],
)
view = navigate(graph, "npu_gemma_rms_norm", depth=3, limit=40)
changes = compare_maps(previous_graph, graph)
```

`source_root` and `state_root` are explicit local directories. `revision` uses
local immutable Git objects; it does not fetch or require a clean checkout.
Without it, the identity covers actual bytes read from the worktree; the
observed HEAD is informational and does not claim those bytes were committed.
Omit `paths` to inspect supported source files under the root. Scope paths are
relative and cannot escape the root. Worktree symlinks are skipped, as are
`.git`, `.mindie-local`, environments, build output and common generated folders.
Pinned Git reads regular blobs only, never symlink targets.

`limits` optionally overrides `max_files` (3,000), `max_bytes` (64 MiB),
`max_file_bytes` (2 MiB), or `max_seconds` (30). Time/cancellation checks are
cooperative between files, not a hard subprocess/grammar execution deadline.
`cancelled` can be a no-argument predicate. Rebuildable per-file parse checkpoints
survive interruption. Repeating an immutable revision reuses parsed blobs without
reading source bytes; worktree mode hashes current bytes and reuses parsing.
An oversized scope returns explicit omissions; narrow `paths` or choose an
explicit larger maintenance budget instead of treating omissions as absence.

The returned graph is an internal artifact, **not suitable for printing in full**
to an Agent. A default CLI should show `status`, `stats`, scope, gap counts and
the snapshot id; `navigate` bounds the displayed nodes/edges. Full maps include
files, definitions, static imports/calls, PyTorch operator schemas/registrations,
Ascend registrations, and dynamic API name references. Evidence binds file,
lines, content hash and observed snapshot. Unresolved expressions stay terminal
in navigation, so common `x.dim()` spellings do not connect unrelated functions.

Python uses the standard-library AST and honors source encoding declarations.
C++ loads `tree-sitter==0.25.2` and `tree-sitter-cpp==0.23.4` lazily. Install them
through the optional `code` extra. If unavailable, C++ coverage reports a gap;
Python remains usable. Known PyTorch/Ascend macro shapes have labelled syntax
adapters. No preprocessor evaluation, arbitrary macro expansion, overload
resolution or runtime behavior is inferred. `EXEC_NPU_CMD(aclnnX, ...)` proves a
dynamic API name reference, not that a specific kernel executed.

`complete` concerns reported syntax coverage. `source_complete` independently
says whether the selected source files were all read: a C++ parse gap does not
mean the file was missing. Incomplete work does not overwrite a previous complete
map. `compare_maps` requires the same source/scope; disappearance is `removed`
only after a complete source observation, otherwise `unknown`. It records
changed symbol bodies separately so unchanged functions in a changed file do
not automatically affect their symbol-linked notes. Nothing is deleted or
declared false by a comparison.

```python
from mindie_knowledge.relations import build_relations, backlinks, affected_documents

relations = build_relations(loaded_documents, code_maps=[graph])
incoming = backlinks(relations, document_uri)
worklist = affected_documents(previous_relations, changes)
```

Inputs are existing `Document` instances and code maps, so no second source
fetch, catalog or scanner is introduced. Ordinary Markdown links (including
reference links) and optional existing `source`/`evidence` locations become
explicit associations. Fenced link examples are ignored. Local targets absent
from the supplied snapshots remain unknown; the builder does not probe shares
or follow arbitrary linked files. GitHub blob links resolve to code only for a
matching repository coordinate and exact pinned commit. Line fragments are
verified when the supplied code snapshot has that line range; other fragments
remain unchecked.

Code-span symbol mentions produce `symbol_candidate` relations, including all
bounded exact-name matches across source roots. They are not automatic judgments
about applicability, conflicts or runtime behavior. Backlinks and affected-note
lists are mechanical inputs for independent maintenance. Topic synthesis,
experience merging and Skill edits remain with the native maintenance Agent.
All generated cache/provenance here is private local state, not a prepared
public export.
