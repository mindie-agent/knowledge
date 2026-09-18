# knowledge-intake

An independent, installable tool that converts selected sources to ordinary
Markdown, original assets and optional `.meta.json` provenance. It has no VAWS
or knowledge-service dependency. A knowledge system consumes its output using
an ordinary directory mount; importing this Python package performs no scan,
network request, model initialization or maintenance.

## Install and run

Python 3.11+ is required. From this repository:

```sh
uv tool install './tools/knowledge-intake[formats]'
knowledge-intake intake.json
```

Omit `[formats]` for Markdown, text, HTML, URL, Git and PR intake without
document-parser dependencies. `[images]` adds Pillow only. There is no local
embedding or caption model. `knowledge-intake --help` is inert.

```json
{
  "state_root": "./intake-state",
  "output_root": "./reference-material",
  "sources": [
    {"type": "local", "path": "./selected-reports"},
    {
      "type": "git",
      "url": "https://github.com/vllm-project/vllm-ascend",
      "ref": "HEAD",
      "paths": ["README.md", "docs/source"]
    },
    {
      "type": "github_pr",
      "url": "https://github.com/vllm-project/vllm-ascend/pull/16157"
    }
  ]
}
```

Paths resolve against the process working directory. Choose the source and
output directories once for independent maintenance; ordinary development
agents do not need intake commands or metadata authoring. These examples target
vLLM, Ascend NPU and AI infrastructure. The PR example is an observed open WIP,
not an accepted solution or execution result.

Python entry points accept plain mappings:

```python
from knowledge_intake import sync_source, sync_sources

result = sync_source({"path": "./reports"},
                     state_root="./intake-state",
                     output_root="./reference-material")
result = sync_sources(config, force=False)
```

## Supported material

| Source | Actual conversion | Evidence / limits |
|---|---|---|
| Markdown / UTF-8 text | Preserved text | Original bytes SHA256 and normalized span SHA256 |
| HTML | Standard-library parser retaining headings, tables, lists and code | Scripts/styles excluded; not browser execution or CSS layout |
| PDF | PDFium text extraction, page references, scan-page rendering | Scanned pages can use native OCR or Agent image interpretation |
| DOCX | Paragraphs/headings/tables in document order | Block locations; embedded images remain in original document and are marked unextracted |
| PPTX | Slide text/tables/speaker notes | Slide locations; embedded images retained in original and marked unextracted |
| XLSX | Sheet/cell coordinates and values/formulas | Formulas remain formulas; no invented recalculated values |
| PNG/JPEG/WebP/BMP/TIFF | Original image plus optional OCR/native caption | Multi-frame inputs are explicitly partial |
| HTTP(S) URL | Bounded download, then the same format parsers | ETag/Last-Modified observations and actual content hash |
| Local Git | Committed blobs at a resolved commit | No checkout, source execution, submodules or uncommitted-file substitution |
| GitHub repository | Resolved commit and bounded tree/blob API traversal | Selected `paths`; API truncation remains explicit |
| GitHub PR | Description, pinned base/head, file-change patch excerpts, issue/review comments | Only `vllm-project/vllm` and `vllm-project/vllm-ascend`; patch excerpts are not full diffs or test evidence |

GitHub transport reuses installed `gh api`, including its existing native
authentication. Without `gh`, public API requests work subject to GitHub's
unauthenticated limits. It does not create forks or inspect account secrets.
Other Git servers can be consumed through a local clone. Unsupported legacy
Office formats (`.doc`, `.ppt`, `.xls`) require conversion before intake.

## On-demand image interpretation

Images and scanned PDF pages remain linked to their original source. No model
is installed or started. Prefer an independent native Agent with image input:

```json
{
  "path": "./selected-reports/graph.png",
  "caption": {"provider": "native_agent", "directory": "./native-requests"}
}
```

The first explicit synchronization emits `<input_sha256>.request.json` and
reports `partial`/pending. The independent Agent (for example Grok Bot) reads
the referenced actual image and writes `<input_sha256>.result.json`:

```json
{
  "input_sha256": "copy the exact request hash",
  "text": "The Agent's actual source-grounded image description.",
  "refs": ["an exact entry from the request's allowed_refs"],
  "model": "the actual native model, if known"
}
```

An explicit rerun accepts this result only when its source/input hashes still
match. Unknown references, edited images, stale hashes and empty/excessive text
are rejected. The tool does not pretend a pending request has generated a
caption, poll native chats or launch an Agent. Independent maintenance owns
this handoff; it adds no step to development tasks.

Optional lightweight OCR uses an already installed capability:

```json
{"path": "./selected-reports", "ocr": {"provider": "windows", "language": "en-US"}}
```

Windows OCR invokes Windows PowerShell 5.1/WinRT in a hidden disposable child.
It uses installed language resources. Cross-platform Tesseract is available as
`{"ocr":{"provider":"tesseract","language":"eng"}}` when the executable
and language data already exist. Missing capabilities report an actual partial
result; there are no implicit language/model downloads. OCR and native caption
can be combined, with extraction kinds kept separate in metadata.

## Bounds, updates and failure behavior

Each source defaults to 100 imported files, 5,000 scanned entries (including
unsupported files), 16 MiB/file, 64 MiB read bytes, 120 seconds, 100 PDF pages or
slides, 100,000 spreadsheet cells, 16 million image pixels, 64 MiB expanded
Office archives and 500,000 extracted characters. Set positive overrides under
`limits`; unknown limit names fail clearly. Document parsing runs in a
disposable process with a 512 MiB memory budget and one BLAS/OMP thread.
Windows uses an OS Job Object; Linux uses an address-space limit. macOS uses
parent-side resident-memory sampling every 10 ms; brief overshoot is possible
between samples. Downloads use the same process memory budget. Process output
and wall time are also bounded. There are no background workers or resident
parsers.

The source journal hashes content and conversion configuration. A complete,
unchanged item performs **zero parser/OCR/caption calls**. Sources are still
read to validate their observed content; PRs still fetch their bounded API
snapshot. Git pins are resolved before fetching blobs. A successful item can
be resumed after a later source error. The source's successful cursor advances
only after a complete successful scan. Missing files remain intact and appear
as `missing`; a partial scan never implies deletion.

Markdown/metadata are staged and journaled before atomic replacement. An
interrupted pair write is recovered only if every observed old/new file hash
still matches. If a maintainer changes generated Markdown, its metadata or
assets, the tool preserves them and reports a conflict. `--force` does not
bypass this protection. Source symlinks are skipped, generated paths cannot
escape their output root, and state stays outside the published output tree.
Original source assets are content addressed. Journals and old staged snapshots
are local maintenance state, not material to mount or publish.

`status`, successful and observed `revision`, `updated`, `unchanged`,
`converted`, `preserved`, `missing`, `errors`, `truncated`, scan/byte counts and
elapsed time are returned as JSON. CLI exits 2 on partial/error. Imported PR
statements and generated descriptions remain reference claims with their
source limitations; they are never promoted to validated execution facts.

## Verification

```sh
python -m unittest discover -s tools/knowledge-intake/tests -v
```

Run with this directory on `PYTHONPATH` or after installation. Optional parser
tests need `[formats,test]`. Tests create actual DOCX/PPTX/XLSX/PDF/image
fixtures, verify source-span hashes, execute local HTTP and Git transports,
check interruption/ownership/bounds, and perform real Windows OCR when those
capabilities exist. Fixture measurements are explicitly synthetic. Native
result unit tests validate the file protocol only; actual Agent interpretation
is accepted separately. `tests/fixtures/make_fixtures.py DIRECTORY` creates a
reproducible format and synthetic chart corpus for independent acceptance.

See [the dated acceptance record](ACCEPTANCE-2026-09-13.md) for actual execution
and its limits.

## Prepared Markdown feed from an independent Agent

`knowledge-feed` pulls one explicitly configured Git feed into its own stable
Markdown directory. It uses no VAWS runtime package, models, MCP workers or
Codex automation. It can consume the prepared output of a separately operated
Grok Bot. The publisher remains responsible for package preparation and public
publication; this tool neither launches that Agent nor pushes Git changes.

Create a standalone `feed.json` outside the generated output directory:

```json
{
  "repository": "https://github.com/YOUR_PERSONAL_USER/mindie-knowledge",
  "ref": "codex/va-reference-feed",
  "export_path": "",
  "state_root": "./feed-state",
  "output_root": "./grok-reference-feed",
  "max_seconds": 120
}
```

Paths resolve relative to this configuration file. `repository` may also be an
existing local Git directory for offline use. `output_root` is a dedicated feed
directory that can be mounted as ordinary Markdown. Keep it separate from
project notes, state, and the publisher's checkout.

```powershell
knowledge-feed sync C:\reference-sync\feed.json
```

The same entry is `python -m knowledge_intake.feed_sync sync CONFIG`. GitHub
reads reuse `gh api` authentication when available, or the public GitHub API.
Every run resolves the branch once, then reads only committed blobs at that
revision. No checkout, history clone, repository execution or model download is
performed. GitHub directory listings are reused within this one pass.
An already verified, unchanged Git commit only resolves the branch and checks
local generated-file hashes; it downloads no source blobs again.
For a new commit, unchanged files are reused only after the new manifest,
current local hashes and that commit's Git blob identities agree. Only changed
Markdown or metadata blobs are downloaded.

The feed preserves the `vaws-curation-export/1` layout:

```text
current.json
generations/<32-hex-generation>/prepared.json
generations/<32-hex-generation>/topics/example.md
generations/<32-hex-generation>/topics/example.meta.json
```

The publisher copies the export's `current.json` and only its returned prepared
generation; it must never publish the export container's lock files or other
local state. Preserve exact Git blob bytes when committing prepared material
(disable newline conversion for that feed). `export_path` optionally places
this layout below a repository subdirectory. Older generations and unrelated
repository files are not imported.

The fixed commit's pointer binds the manifest SHA256. The consumer verifies
the supported public preparation profile (`r2`), snapshot hash, exact generation
file list, final Markdown/metadata hashes and retrieval source bindings before
changing any local file. The configured publisher is trusted: these checks
detect broken or mixed snapshots, but do not independently redo redaction or
turn reference claims into validated evidence. Limits are 1,024 documents, 4 MiB
per Markdown, 256 KiB per metadata, 2 MiB per manifest, 32 MiB prepared content and a bounded
transport/time budget. No source text is reformatted during import.

A journal proves ownership of each generated file. Unchanged feeds do not
rewrite Markdown or metadata. A verified complete feed can remove or rename
its own unchanged generated files; `missing` and `deleted` report these paths.
Unmanaged files and manual edits are preserved, and any conflict prevents the
whole new batch from being applied. Before changes, the tool writes recovery
copies; an I/O failure rolls back. After interruption, the next run restores
old bytes only when observed hashes still match the transaction. A concurrent
manual edit is preserved and reported for judgment. This is a recoverable
multi-file update, not an atomic directory snapshot for simultaneous readers.
Successful recovery copies are discarded; interrupted recovery stays in state.
`last-run.json` records the latest outcome without changing reference content.

Windows scheduling is optional and explicit:

```powershell
knowledge-feed schedule install C:\reference-sync\feed.json
knowledge-feed schedule status C:\reference-sync\feed.json
knowledge-feed schedule uninstall C:\reference-sync\feed.json
```

Install `knowledge-intake` into a persistent Python environment before
scheduling. Installation verifies that this interpreter can import the package
without `PYTHONPATH`; uninstall works even if the configuration file was removed.
The owned per-user task runs hourly and at that user's logon, while logged on,
with least privilege and no stored password. It ignores overlapping starts and
has a five-minute execution limit. `pythonw.exe` is used when available to avoid
opening a console. Reinstalling unchanged settings and repeated uninstall are
idempotent. A task name owned by something else is never overwritten or deleted.
This entry does not install a task until the explicit `schedule install` call.
On Windows, a feed rename that changes only letter case is conservatively
reported as a conflict; no existing file is deleted to resolve that ambiguity.

Feed-specific tests use real local Git snapshots and actual isolated CLI calls:

```sh
python -m unittest discover -s tools/knowledge-intake/tests -p 'test_feed*.py' -v
```

The Windows tests execute the actual PowerShell scheduling script against fake
task-service functions; they do not register a real scheduled task. A real
GitHub branch and actual scheduler execution require separate deployment
acceptance with the selected personal publisher and installed environment.
