# mindie-knowledge 0.13.7: Preserve Bootstrap Baselines During Native Upgrades

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.7/docs/releases/knowledge-0.13.7.md) · **English**

Version 0.13.6 changed `source_baseline` to include Kimi's active integration overrides by default. An older Kimi updater builds its candidate from immutable bootstrap requirements, then compares the selected component revisions against this baseline. Treating active pins as bootstrap pins made that comparison incorrectly skip the component build. Live upgrade acceptance exposed a Kimi 0.1.8 to 0.1.9 transition that activated older bootstrap components.

`source_baseline(source)` again returns only the bootstrap requirements. Native owners can explicitly request `source_baseline(source, active_overrides=True)` to layer the official, exact source pins from `runtime/active-overrides.txt`. This new path separates active integration floors from the bootstrap input understood by older controllers. An explicit request reads the file directly and reports missing or invalid pins. The return shape and source identity remain unchanged.

The matching Kimi repair moves its active overrides to that path without retaining the old top-level file, and compares the candidate interpreter's actual inventory with the selected components before deciding whether to build. Both changes are required: a new shared package alone cannot change code already running in an older parent's environment. An already damaged Kimi 0.1.9 installation whose bootstrap runtime lacks its required APIs needs the existing product recovery entry once; this release does not claim that instance recovered automatically.

Focused tests exercise bootstrap and active source selection, the old controller's candidate decision with both source layouts, and the existing formal release selection and runtime build contracts. The formal release workflow runs the exact tag's dependency checks, local contracts, graded Feed workload, and tokenization benchmark. No paid model evaluation or user configuration migration is introduced.
