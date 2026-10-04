# knowledge

## Mandatory design-principles review before PR submission

All MindIE Agent development tasks MUST read the current [design principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md) and review the final diff and affected behavior against all nine principles before creating any PR, including a draft. This covers code, configuration, dependencies, CI and project documentation. Review subsequent PR changes against the affected principles again.

Errors MUST remain visible to the caller. Never turn a required-step failure into success, empty data, first use, user disablement or an undocumented fallback. Distinguish completed work, failure and uncertain external effects; do not repeat an uncertain write or hide the original error behind a later failure.

Fix principle violations before submission. In the PR description, record the reviewed commit and principles revision, concrete conclusions and applicability, findings and fixes, and actual validation or remaining gaps. Tests passing, checkboxes, blanket N/A or “all principles followed” are not a review. If the principles cannot be read or the review is incomplete, report that explicitly and do not create or update the development PR until review is complete. Pure knowledge/feedback contributions follow their content rules; development of those features remains subject to this review.
