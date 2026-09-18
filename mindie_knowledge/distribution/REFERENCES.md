# Public reference metadata and prepared local sources

Release schema `mindie-knowledge-release/1` accepts an optional `references`
asset. New builds produce `mindie-knowledge-references/1`; old body-only packs
continue to work and report `metadata_status=unavailable_legacy`. No re-embedding
is required to consume an existing valid dense pack.

The release manifest binds the reference asset filename, SHA256, byte size,
document count and source Git SHA. The asset lists only relative document paths,
final public body hashes, prepared aliases/topics, conditions and source-derived
context spans. Its document set must exactly match the released Markdown set.
Body hashes and contexts are checked against the actual Markdown in the OVPack.

`build_pack` materializes a fixed local Git commit, prepares each Markdown body
through the package's existing public-copy redaction, selects and redacts only
the supported retrieval/condition fields, then rebuilds contexts from the final
public body. Stale source-bound aliases are omitted. Raw source/evidence objects,
private local paths and unknown sidecar fields are not copied. The working tree
and original notes stay unchanged. `catalog.export_catalog` remains a portable
export API, **not** a substitute for this public preparation boundary.

`make_release`, public upload and GitHub download include and verify the extra
asset. Sync verifies the pack and references before activation. The Markdown
already inside the OVPack is reused to create a private, versioned prepared mount;
there is no second released body archive and no model/embedding call to extract
the source. A new prepared directory is completed before the single atomic active
pointer names both its matching vector URI root and source mount.

The active pointer adds:

- `prepared_root`: the private local immutable source generation.
- `prepared_manifest_sha256`: binds its file/body/metadata hashes.
- `references_sha256`: the public reference asset hash, or null for legacy packs.
- `metadata_status`: `available` or `unavailable_legacy`.

`prepared_shared_documents(state_root, current=pointer)` streams only that
generation and assigns `root_uri/relative_path` document identities. Reads are
bounded to the shared Markdown limits (4 MiB per body and 256 KiB per prepared
sidecar). Consume its iterator
inside the catalog transaction: a mid-stream failure must roll back the refresh.
Source/body
and metadata corruption fail closed for the affected source observation; they do
not erase earlier knowledge or make claims false. A verification pass can prepare
a repaired generation and switch the pointer while leaving existing readers'
generation intact. Imported reference catalog rows are a derived cache: consumers
filter them by the same active URI prefix, so a pointer switch cannot expose old
aliases under a new source version before catalog maintenance catches up.

New prepared metadata is private local state even when its contents are public.
Do not export this mount's machine paths or active-pointer fields publicly.
