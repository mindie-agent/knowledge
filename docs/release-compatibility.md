# Release eligibility and installed-operation locks

`mindie_knowledge.releases` selects releases; it does not install, activate,
persist a catalog, or change authorization. `semantic-version==2.10.0` supplies
SemVer parsing and precedence. The native Harness remains the installer.

## Public Release contract

Each official stable plugin Release publishes one asset named
`mindie-plugin-release.json`:

```json
{
  "schema": "mindie-plugin-release/1",
  "version": "0.0.1",
  "source": {
    "repository": "mindie-agent/mindie-agent-codex",
    "sha": "0123456789abcdef0123456789abcdef01234567"
  },
  "compatibility": {
    "schema": "mindie-harness-compatibility/1",
    "harness": "codex",
    "supported": [{
      "minimum": "0.153.4",
      "maximum": "0.153.4",
      "maximum_inclusive": true,
      "include_prerelease": false
    }],
    "used_interfaces": ["hooks.stop"],
    "interface_registry": {
      "hooks.stop": {
        "supported": [{
          "minimum": "0.153.4",
          "maximum": "0.153.4",
          "maximum_inclusive": true,
          "include_prerelease": false
        }],
        "code_paths": ["scripts/entry.py:stop"],
        "test_ids": ["tests/test_stop.py::test_native_stop"],
        "evidence_refs": ["publisher-verified source or native evidence"]
      }
    }
  }
}
```

The example illustrates the schema, not evidence that these symbols exist or
that a Release has been published. The publisher verifies every registered
call site, test and evidence reference against the release source. Each used
interface must be registered, and every declared release interval must fit
inside one supported interval of every used interface. Split declarations
at registry boundaries; this API does not solve arbitrary range expressions.

The minimum is inclusive. The maximum may be inclusive or exclusive.
A list means a union; two closed singletons do not declare the versions between
them supported. Codex prereleases, such as `0.162.0-alpha.17.2`, require an
interval with `include_prerelease: true`. Build metadata does not affect
SemVer precedence. Bounds cannot contain build metadata.

The fixed repository mapping is `codex` to
`mindie-agent/mindie-agent-codex` and `claude-code` to
`mindie-agent/mindie-agent-cc`. Unknown Harnesses or repositories fail.

```python
from mindie_knowledge.releases import select_release, supports_harness
from mindie_knowledge.releases_io import fetch_releases, verify_release_source

records = fetch_releases("mindie-agent/mindie-agent-codex")
result = select_release(
    records,
    repository="mindie-agent/mindie-agent-codex",
    harness_name="codex",
    harness_version="0.153.4",
    installed_version="0.0.1",
)
if result["status"] == "candidate":
    metadata = verify_release_source(
        result["candidate"]["metadata"],
        repository="mindie-agent/mindie-agent-codex",
        harness_name="codex",
    )
```

Selection returns `candidate`, `up_to_date`, or `no_compatible_release`.
It excludes draft and prerelease plugin Releases, chooses the highest
compatible stable version, and never downgrades or reinstalls an equal version.
Stable historical Releases with no declaration are represented explicitly as
`metadata=None`, excluded, and counted in `skipped_undeclared_releases`.
A declaration that exists but is corrupt, ambiguous, or inconsistent fails
the check. A valid newer Release for an unsupported Harness is simply
ineligible.

`supports_harness(compatibility, harness_name=..., harness_version=...)`
validates an installed source declaration with the same rules. Malformed
metadata or an unknown Harness is an error, not an unsupported-version result.

## Origin and integrity

Pure validation does not authenticate a Python dictionary or local index.
Use `fetch_releases` for public GitHub reads. It reads only the fixed official
repository endpoints and returns a complete catalog or a static failure.
It never reads authentication files, installs, writes state, or retries.

The reader allows at most 100 pages of 100 records, 8 MiB per page and 1 MiB
per metadata asset. A full page without a `next` link requires another page
read. Partial pages caused by transport errors, HTTP 401/429, malformed JSON,
unexpected pagination, or the catalog limit are failures, not empty catalogs.
Asset name, API/download identity and actual byte length must match; a GitHub
SHA256 asset digest is checked when provided. Redirects remain HTTPS on
GitHub's API, web and release-asset hosts. HTTP timeout is per read; the
adapter owns check coalescing and the background control budget.

`verify_release_source` checks the full immutable Git commit and the fixed
compatibility file at that commit: `plugins/mindie-agent/harness-compatibility.json`
for Codex and `harness-compatibility.json` for Claude Code. The response must
be a file with valid base64, size and Git blob hash, and its strict declaration
must equal the Release asset. This still does not prove installed bytes.
The adapter verifies the native installed source SHA and declaration before
activation. Release eligibility must never be inferred from an arbitrary
manifest URL, local untrusted index, or repository supplied by metadata.

Errors expose static `.code` values through `ReleaseValidationError` and
`ReleaseFetchError`; the latter also exposes an HTTP `.status` when known.
Adapters record these control failures locally without injecting update
messages or additional model turns. This module owns no user consent,
business result, active runtime binding, or uncertain native installation.

## One OS file-lock implementation

`mindie_knowledge.loop.locks` exports:

```python
lock_file(fd, exclusive=True, blocking=False)  # busy raises BlockingIOError
try_lock_file(fd, exclusive=False)            # False only for OS contention
unlock_file(fd)
```

The caller opens the existing adapter lock path and retains the descriptor
for the operation. POSIX uses `fcntl.flock`; Windows uses pywin32
`LockFileEx`/`UnlockFileEx` on byte `[0, 1)`, compatible with the existing
bootstrap byte-range lock. Shared readers overlap; an exclusive updater
cannot acquire while any reader holds the same lock. OS process exit releases
ownership without deleting the persistent lock file. Non-contention errors
remain errors.

Installed adapters use this shared implementation instead of duplicating the
Windows ABI. Runtime bootstrap retains its existing exclusive preparation
lock because Core is not yet available. The locks add no state table, lock
path migration, retry policy, or runtime selector.
