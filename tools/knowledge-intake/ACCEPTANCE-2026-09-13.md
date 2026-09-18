# Independent intake acceptance — 2026-09-13

Status: dated execution evidence; independent native Agent caption accepted

The independent tool was executed on Windows with Python 3.12.14 and the
already installed PDFium 5.13.0, python-docx 1.2.0, python-pptx 1.0.2,
openpyxl 3.1.5 and Pillow 12.3.0. ReportLab 4.4.9 created synthetic fixtures.
No package/model download or new model service was used.

- 20 tests were discovered: 19 passed and the unavailable optional Tesseract
  capability was explicitly skipped, in 9.917 seconds. These include actual document conversion,
  local HTTP/Git, interrupted write recovery, exact normalized span hashes,
  preservation of edited output, symlink boundaries and subprocess limits.
  A real trickling HTTP response verifies the source's absolute download
  deadline, including transport waits, through a disposable process.
- Both source distribution and wheel built successfully. The wheel installed
  alone into a fresh environment; isolated import confirmed that neither
  `mindie_knowledge` nor document parsers were present. Installed CLI help and
  an actual Markdown synchronization succeeded with no optional dependencies.
- Independent CI is authored for Ubuntu Python 3.11/3.13 and Windows/macOS
  Python 3.13. It installs only this tool's `[formats,test]` extras, runs the
  real format/HTTP/Git/native-handoff tests, and installs the wheel without
  dependencies in another fresh environment. An installed OCR provider runs;
  an unavailable OS capability or language is explicitly skipped. No OCR model
  download is configured. Remote CI has not yet run for this change.
- Windows native OCR read `HCCL graph replay 910B` from an actual image and
  from a scanned PDF rendered page. The test compared the returned OCR text.
- Eight persistent format fixtures (Markdown, HTML, DOCX, PPTX, XLSX, text PDF,
  PNG chart, scanned PDF) consumed 144,746 source bytes, converted in 3.687
  seconds, and repeated with 0 conversions / 8 unchanged in 0.047 seconds.
  These are observed local runs, not cross-platform latency promises.
- A real public snapshot of
  [vllm-ascend PR #16157](https://github.com/vllm-project/vllm-ascend/pull/16157)
  was fetched in 4.579 seconds: base
  `fb2820b9b6598f888d7dc56260c03d3352cc08e9`, head
  `debfa9bd48fefcc90added07b28540daee8b5435`, source updated
  `2026-09-09T09:24:15Z`. It was open and unmerged when observed. Five changed
  files, API patch excerpts and discussion retain their source links. Repeat
  fetched the source snapshot and performed 0 conversions in 4.343 seconds.
- A real GitHub repository snapshot selected `README.md` at
  `fb2820b9b6598f888d7dc56260c03d3352cc08e9`, using 50 bounded tree entries and
  60,564 bytes in 3.110 seconds. A direct public raw Markdown URL at the same
  revision imported 10,056 bytes in 0.828 seconds.
- Native image interpretation emitted a real hash-bound request for the
  synthetic HCCL graph chart. It first correctly returned pending and left its
  successful source cursor unset. An independent Codex Agent then actually
  viewed the image and supplied a description: axis values 0/60/120/180
  tokens/s, synthetic 910B/910C bars at 120/180, no measured performance and
  unspecified batch/software conditions. The exact request/result hash was
  `caa0d6dc5c00b613b1bd8d32269063d1fbf0f336ff04b90e00dc977e870c243b`.
  Intake accepted the real result in 0.844 seconds; repeat performed zero
  conversions. This establishes native Agent image handoff, not a Grok Bot run.
- The final nine-note ordinary mount was indexed with the current catalog
  implementation in 26.909 ms. Public `query` retrieved the image description,
  and public `explain` returned `native_caption` span evidence with its original
  image hash. Vector retrieval was deliberately unavailable; the response
  accurately retained `degraded=true`. No vector/model service was started.
  This is a small-corpus integration check, not the separate scale benchmark.

Local run artifacts are under the component worktree's untracked
`.mindie-local/knowledge-intake-acceptance/`: `formats-result.json`,
`public-result.json`, `native-result.json`, `retrieval-result.json`, `sources/`,
`native-requests/` and `state/`. The final ordinary mount is `final/output/`;
historical `output/` retains earlier run artifacts. Fixture values are
synthetic and are labelled in both original media and converted material.

The first exploratory run exposed excessive BLAS thread-stack reservation
under the memory ceiling; setting the disposable worker's numerical thread
counts to one fixed it. A Windows reflection invocation typo was also fixed
before the passing run. The final tests enforce the actual worker path.
Linux/macOS, Tesseract and arbitrary production documents were not executed
in this Windows acceptance. Embedded Office images and multi-frame image
content are explicitly partial; originals remain linked.
