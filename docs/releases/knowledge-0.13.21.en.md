# mindie-knowledge 0.13.21: component asset cache and HTTP framing validation

[中文](knowledge-0.13.21.md)

- Validate component checksum and benchmark assets before caching using their size, digest and content. Discard invalid legacy asset entries and propagate forced refreshes to assets.
- Classify malformed Content-Length only at the HTTP framing boundary, allowing legacy entries to be replaced. Local JSON parser faults remain internal.
- Cover the actual Catalog asset path for new responses, legacy caches and forced refreshes, plus malformed length headers on real HTTPResponse objects.

Independent review follow-up for [mindie-agent#354](https://github.com/mindie-agent/mindie-agent/issues/354). Asset integrity failures remain reportable; sanitized historical events cannot identify the initial cause.
