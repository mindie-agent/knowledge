# Independent maintenance

Use an independent native agent for open judgments such as topic organization,
research, compatible experience reuse and cross-task digests. Prefer the user's
Grok Bot when available. It has its own cloud computer and routines; a task
agent neither dispatches nor waits for these jobs during ordinary VA work.

Select a bounded set of original notes or existing useful summaries. No task
transcript scraping or second per-task summary is needed. Fix relevant source
revisions; a merged PR, a model answer or a title match is not execution
evidence. PR cases retain base/head, scope, changes and remaining uncertainty.

`python -m mindie_knowledge curation prepare --help` describes the local handoff.
It takes a brief, source files and a topic subdirectory in the writable candidate
mount. Project/shared mounts remain read-only through the package; requested
direct edits to project Markdown use native file tools. The curation tool
records source hashes, output baselines, a time/file/byte budget and a readable
`TASK.md`. The agent edits ordinary Markdown copies under `output/`. Optional
`Retrieval queries` / `检索问法` sections become aliases bound to the exact new
body; no JSON schema is an authoring requirement.

`curation apply` accepts those Markdown files (or a downloaded result directory)
only while the source and target snapshots remain valid. It preserves optional
conditions and prior evidence, records before/after revisions, and refuses to
overwrite another editor's work. `status`, `cancel` and `undo` operate on the
returned job reference. Cancel stops accepting the job's result; stop a running
Grok task through Grok too. Source changes require a new bounded preparation.
The next catalog refresh makes the result searchable; no generative call occurs
inside query. Ordinary direct Markdown edits remain supported.

For cloud work, transfer only explicitly selected inputs within the user's
scope. The local handoff is not a public redaction step. Public sources can be
read directly in Grok; private material requires the authorized prepared-copy
path before public contribution. Never forward credential files. Use the
already configured native GitHub login for authorized repository operations.

For a public-source cloud feed, `curation-export --source-root ROOT --output-root
EXPORT` prepares only `topics/`, `cases/` and `maintenance/` by default. It
returns a generation directory and manifest hash. Verify it with
`curation-export --verify-root GENERATION --manifest-sha256 HASH` before transport.
Only the verified generation plus its `current.json` pointer belong in a
dedicated feed branch. Raw source snapshots, job history, credentials and private
sidecars stay out. The shared pattern-based redaction profile is a mechanical
check, not a classifier of every possible private fact; feed inputs stay within
the selected public-source scope. This does not change canonical corpus human
review and merge. The receiving independent intake tool verifies the feed before
placing ordinary Markdown in a configured mount.

First run a concrete task and inspect the returned artifacts, then save the
working brief as a Grok routine. A reasonable bounded configuration is daily
PR intake and weekly topics/digests, with an explicit timezone, source list and
time budget. Successful source reads and completed output advance checkpoints;
failures preserve earlier notes. Keep unchanged runs quiet and report meaningful
changes, failures or needed user action. Do not recursively spawn maintainers.

For images and scans, the independent intake tool emits a hash-bound native
vision request and keeps the original image/page. Read the actual image, retain
units, axes and missing conditions, and return the description against the
requested hash and allowed references. OCR is optional and on demand. A diagram
or synthetic fixture cannot establish measured hardware performance.

Digests link selected existing summaries, identify the covered interval and
separate recurring observations from proposed generalizations. Related notes
with incompatible hardware, software revisions or topologies remain distinct.
Topics and aliases are navigation aids; neither publication nor maintenance
raises their authority. Include an explicit cross-topic query when broader
reference material would help.
