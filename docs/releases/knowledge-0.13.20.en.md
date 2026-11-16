# mindie-knowledge 0.13.20: release metadata cache and parser provenance

[中文](knowledge-0.13.20.md)

- Validate JSON at the public HTTP boundary before caching. Discard invalid legacy entries and fetch without their ETags so TTL/304 responses cannot prolong bad data.
- Reject bounded HTTP short reads using Content-Length. Preserve valid ETag caching, cache limits, and visible refresh failures.
- Classify confirmed HTTP JSON rejection as a remote feed failure. Preserve local ValueError/RecursionError and unproven invalid_release_json failures as internal faults.
- Apply the same validation boundary to plugin releases and the shared component Catalog, with behavioral regression coverage.

Related diagnostic: [mindie-agent#354](https://github.com/mindie-agent/mindie-agent/issues/354). Sanitized events cannot establish the historical response body or initial corruption cause; this release repairs the reproduced cache amplification path.
