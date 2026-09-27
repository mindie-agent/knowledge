"""Shared community-contribution settings (``mindie-community-config/1``).

One local JSON file shared by the adapter and the knowledge runtime. It is
re-read from disk at every gate — before transcript reading, before spawning a
model and before any outbound write — so flipping ``enabled`` or bumping
``generation`` takes effect without restarting anything. Missing, malformed or
unconfigured state fails **closed** for capture and contribution, but never
blocks read-only retrieval, plugin updates or knowledge sync.

This file is the persistent installation-level sharing configuration. A saved
explicit value (enabled or disabled) never expires: not on restart, upgrade,
fork, network failure or failure counts. Its honest state is reported
distinctly — ``unconfigured``, ``missing``, ``unreadable``, ``corrupt``,
``disabled`` or ``enabled`` — so a damaged file is never treated as a
never-configured installation (no fresh onboarding) and a missing one is never
treated as an explicit opt-out.

The optional ``consent_config`` extension key (absolute path) names the
profile-shared ``mindie-consent/1`` authority implemented by
``mindie_knowledge.consent_store``. When present, every gate additionally
requires that document's saved ``contribute`` choice: a missing, unreadable,
corrupt or non-contribute consent stops capture, model and write paths while
read-only helpers keep working, and the fault is reported as itself — never as
first-time onboarding. The field grants no permission by itself: an explicit
``enabled=false`` here always wins. Configs without the field keep their
previous read behavior; the owning adapter wires the field once at its
install/upgrade/entry boundary.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from pathlib import Path

from mindie_knowledge import consent_store

SCHEMA = "mindie-community-config/1"
CONSENT_FIELD = "consent_config"
DEFAULT_IDLE_SECONDS = 300
MAX_IDLE_SECONDS = 86400
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,120}")


def check_repository(value, name="repository"):
    if not isinstance(value, str) or not _REPOSITORY_RE.fullmatch(value):
        raise ValueError(f"{name} must look like owner/repo")
    return value


def check_branch(value):
    if (
        not isinstance(value, str)
        or not _BRANCH_RE.fullmatch(value)
        or ".." in value
    ):
        raise ValueError("branch name is not usable")
    return value


def normalized_roots(value):
    if not isinstance(value, list) or not all(
        isinstance(item, str) and Path(item).is_absolute() for item in value
    ):
        raise ValueError("'project_roots' must be absolute paths")
    return [
        str(Path(item).expanduser().resolve(strict=False)) for item in value
    ]


def bounded_idle(value):
    if value is None:
        return DEFAULT_IDLE_SECONDS
    if type(value) is not int or not 30 <= value <= MAX_IDLE_SECONDS:
        raise ValueError(f"idle_seconds must be 30..{MAX_IDLE_SECONDS}")
    return value


def normalize(data):
    """The ONE strict validator/normalizer for ``mindie-community-config/1``.

    Core, adapters (through the configured interpreter) and the community
    package all share this — no duplicated ranges. Unknown/incompatible data
    is rejected clearly with a ValueError naming the problem. Extension keys
    owned by sibling components pass through uninterpreted. A config change
    is data only: never a task deactivation or an automatic permission grant.
    """
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"sharing settings must declare schema {SCHEMA}")
    out = dict(data)
    enabled = data.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("'enabled' must be a boolean")
    out["enabled"] = enabled
    generation = data.get("generation")
    if generation is not None and not isinstance(generation, str):
        generation = str(generation)
    if enabled and not (isinstance(generation, str) and generation):
        raise ValueError("enabled sharing settings require a nonempty string generation")
    out["generation"] = generation
    enabled_at = data.get("enabled_at")
    if enabled and not (
        type(enabled_at) in (int, float)
        and math.isfinite(enabled_at)
        and enabled_at > 0
    ):
        raise ValueError("enabled sharing settings require a finite positive 'enabled_at'")
    out["enabled_at"] = enabled_at
    repository = data.get("repository")
    out["repository"] = check_repository(repository) if repository is not None else None
    out["branch"] = check_branch(str(data.get("branch") or "main"))
    out["project_roots"] = normalized_roots(data.get("project_roots", []))
    out["idle_seconds"] = bounded_idle(data.get("idle_seconds"))
    return out


class CommunitySettings:
    """One read of the shared settings file. Treat instances as ephemeral."""

    def __init__(self, path, data, error=None, state=None):
        self.path = Path(path) if path else None
        self.raw = data if isinstance(data, dict) else {}
        self.error = error
        self.schema_ok = error is None and self.raw.get("schema") == SCHEMA
        # The read boundary first separates a VALID config from a fault state
        # through the one existing normalizer — a schema-matching document
        # with malformed values (enabled of the wrong type, out-of-range
        # idle_seconds, bad roots, …) is damaged configuration, never an
        # explicit disable.
        self.valid = False
        if self.schema_ok:
            try:
                normalize(self.raw)
                self.valid = True
            except ValueError:
                self.valid = False
        enabled = self.valid and self.raw.get("enabled") is True
        self.enabled = bool(enabled)
        if state is not None:
            self.state = state
        elif not self.schema_ok:
            self.state = "corrupt"
        elif not self.valid:
            self.state = "corrupt"
        else:
            # "disabled" is reserved for an explicit boolean false in a valid
            # document — the only value that is an active user revocation.
            self.state = "enabled" if self.enabled else "disabled"
        generation = self.raw.get("generation")
        # The generation is a nonempty opaque string shared by all components.
        self.generation = generation if isinstance(generation, str) and generation else None
        enabled_at = self.raw.get("enabled_at")
        self.enabled_at = (
            enabled_at
            if type(enabled_at) in (int, float)
            and math.isfinite(enabled_at)
            and enabled_at > 0
            else None
        )
        repository = self.raw.get("repository")
        try:
            self.repository = (
                check_repository(repository) if repository is not None else None
            )
        except ValueError:
            self.repository = None
        try:
            self.branch = check_branch(str(self.raw.get("branch") or "main"))
        except ValueError:
            self.branch = "main"
        try:
            self.project_roots = [
                Path(item) for item in normalized_roots(self.raw.get("project_roots", []))
            ]
        except (ValueError, OSError):
            self.project_roots = []
        try:
            self.idle_seconds = bounded_idle(self.raw.get("idle_seconds"))
        except ValueError:
            self.idle_seconds = DEFAULT_IDLE_SECONDS
        # Consent gate extension: ``consent_config`` names the profile-shared
        # consent authority (absolute path). It is re-read fresh together with
        # this settings file, so a changed or damaged saved choice is observed
        # at the same boundaries. The key is distinguished by EXISTENCE: only
        # a wholly absent key is the legacy format; a present key holding
        # null, an empty/non-string value or a relative path is a malformed
        # field — ``state="invalid"`` — which fails closed like any other
        # unreadable authority.
        self.consent = None
        if self.schema_ok and CONSENT_FIELD in self.raw:
            consent_ref = self.raw.get(CONSENT_FIELD)
            if (
                not isinstance(consent_ref, str)
                or not consent_ref
                or not Path(consent_ref).is_absolute()
            ):
                self.consent = dict(
                    state="invalid", choice=None, reporting=None, path=None,
                    error="consent_config must be an absolute path string",
                )
            else:
                self.consent = consent_store.read(consent_ref)

    @property
    def configured(self):
        return self.schema_ok

    def _base_allows_capture(self):
        return (
            self.schema_ok
            and self.enabled
            and self.generation is not None
            and self.enabled_at is not None
            and bool(self.project_roots)
        )

    def consent_allows_contribution(self):
        """The consent half of the capture/contribution gate.

        Only a config carrying the ``consent_config`` extension consults the
        saved choice; a legacy config without the field keeps its previous
        read behavior (the owning adapter wires the field once at its
        install/upgrade/entry boundary — the legacy format is read
        compatibility, not a bypass). A configured authority must hold an
        explicit ``contribute`` choice: missing, unreadable, corrupt, an
        invalid field value or any other saved choice stops capture, model
        and outbound-write paths while read-only helpers keep working. The
        field never grants permission by itself — an explicit enabled=false
        always wins.
        """
        if self.consent is None:
            return True
        return (
            self.consent.get("state") == "ok"
            and self.consent.get("choice") == "contribute"
        )

    def allows_capture(self):
        """The whole capture/extraction/sanitization chain needs an explicit,
        well-formed on: schema, enabled, opaque generation, a finite positive
        authorization boundary and at least one absolute canonical root — plus,
        when the config names a consent authority, a saved ``contribute``
        choice. Any malformed enabled config authorizes nothing."""
        return self._base_allows_capture() and self.consent_allows_contribution()

    def contribution_block_reason(self):
        """Short static reason the capture/model/write paths are closed."""
        if self.allows_capture():
            return ""
        if not self._base_allows_capture():
            if self.state == "disabled":
                return "community contribution is disabled or unconfigured"
            return f"the community settings authority is {self.state}; contribution is stopped"
        consent = self.consent or {}
        state = consent.get("state")
        if state != "ok":
            return f"the saved consent authority is {state}; contribution is stopped"
        if consent.get("choice") is None:
            return "the consent authority holds no saved choice; contribution is stopped"
        return "the saved consent choice does not allow contribution"

    def capture_block_kind(self):
        """None when the gate is open, else ``"revoked"`` or ``"fault"``.

        ``revoked`` is an explicit saved off, and only that: a VALID config
        holding the boolean ``enabled=false``, or a valid consent document
        whose saved choice is one of ``read-only``/``later``/``disabled``.
        Already-received work keeps the revocation semantics (cancelled,
        unsent cancelled, never backfilled).

        ``fault`` is every other closed state: the settings file or the named
        consent document missing, unreadable, corrupt or malformed (including
        a present but null/non-string/relative ``consent_config`` value and a
        valid consent document with no saved choice — undetermined is not an
        opt-out). A fault is not a user revocation: already-received work and
        saved results must be kept locatable and resumed after the authority
        is restored and revalidates.
        """
        if self.allows_capture():
            return None
        if self._base_allows_capture():
            # The sharing config itself is open; only the consent gate blocks.
            consent = self.consent or {}
            if consent.get("state") == "ok" and consent.get("choice") in (
                "read-only", "later", "disabled"
            ):
                return "revoked"
            return "fault"
        # state "disabled" is only ever assigned to a valid document holding
        # the explicit boolean false (see __init__); anything else is a fault.
        return "revoked" if self.state == "disabled" else "fault"

    def in_scope(self, candidate):
        """Canonical containment check against the configured project roots.

        Scope checking happens before any path is opened. An enabled config
        without project roots authorizes nothing (fail closed).
        """
        if not self.project_roots or not candidate:
            return False
        try:
            target = Path(candidate).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            return False
        for root in self.project_roots:
            if target == root or root in target.parents:
                return True
        return False

    def as_dict(self):
        """Private settings handed to the community package per contract §4.

        Every key from the shared file survives the handoff — extension keys
        (fork/account/bot/transaction/operation/transport/…) are private
        validated configuration that other components own; dropping one would
        silently break a sibling. ``config_path`` is added for the live
        revocation reread and must never appear in public batch content."""
        data = dict(self.raw)
        data["schema"] = self.raw.get("schema")
        data["enabled"] = self.enabled
        data["generation"] = self.generation
        data["enabled_at"] = self.enabled_at
        data["repository"] = self.repository
        data["branch"] = self.branch
        data["project_roots"] = [str(root) for root in self.project_roots]
        data["idle_seconds"] = self.idle_seconds
        data["config_path"] = str(self.path) if self.path else None
        return data

    def public_status(self):
        """Secret-free operational status."""
        return dict(
            configured=self.schema_ok,
            state=self.state,
            enabled=self.enabled,
            generation_set=self.generation is not None,
            repository=self.repository,
            branch=self.branch,
            project_roots=len(self.project_roots),
            idle_seconds=self.idle_seconds,
            enabled_at=self.enabled_at,
            error=self.error,
            consent=(
                None
                if self.consent is None
                else dict(
                    state=self.consent.get("state"),
                    choice=self.consent.get("choice"),
                    reporting=self.consent.get("reporting"),
                    error=self.consent.get("error"),
                )
            ),
        )


def load(path) -> CommunitySettings:
    """Read the settings file once. Never raises; failures mean disabled.

    The reported ``state`` keeps a missing file, an unreadable one, a damaged
    one and an explicit saved choice strictly apart."""
    if not path:
        return CommunitySettings(
            None, {}, error="community_config is not configured",
            state="unconfigured",
        )
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return CommunitySettings(
            path, {}, error="settings file does not exist", state="missing",
        )
    except OSError:
        return CommunitySettings(
            path, {}, error="settings file is unreadable", state="unreadable",
        )
    if len(raw) > 64 * 1024:
        return CommunitySettings(
            path, {}, error="settings file exceeds the byte limit",
            state="corrupt",
        )
    try:
        data = json.loads(raw)
    except ValueError:
        return CommunitySettings(
            path, {}, error="settings file is not valid JSON", state="corrupt",
        )
    if not isinstance(data, dict):
        return CommunitySettings(
            path, {}, error="settings file must hold one object",
            state="corrupt",
        )
    if data.get("schema") != SCHEMA:
        return CommunitySettings(
            path, data, error="unsupported settings schema", state="corrupt",
        )
    return CommunitySettings(path, data)


def from_engine_config(config: dict) -> CommunitySettings:
    """Resolve the shared settings from one engine configuration document."""
    return load((config or {}).get("community_config"))


_MANAGED_KEYS = frozenset({
    "schema", "enabled", "generation", "enabled_at", "repository",
    "branch", "project_roots", "idle_seconds",
})


def _settings_lock_path(path):
    """The lock file anchoring one write boundary: sibling ``<name>.lock``."""
    target = Path(path)
    return target.with_name(target.name + ".lock")


class CommunityWriteContext:
    """The ONE explicit write boundary for a profile's community settings.

    Holds the profile's cross-process write lock (the consent store's bounded
    OS-lock implementation — no second protocol) exactly once per call chain;
    there is no thread-level "already locked" guessing and no nested
    acquisition, so an adapter holding this context must call its methods —
    not the module-level one-shot helpers — or the same-file flock would
    self-lock.

    Lock key rule: every writer sharing one profile — enable/disable,
    configure, extension stamps, legacy adoption/migration — anchors at the
    SAME lock key, the profile's canonical community path, even while the
    declared target is still a legacy file being adopted. Only then do a
    migration and a concurrent toggle serialize against each other. Inside
    the lock the caller resolves and re-reads the CURRENT authority
    (``read``) before writing, so a user's newer disable is never written
    back into a deprecated file.

    Lock timeout raises ``consent_store.ConsentError`` with
    ``state="locked"``.
    """

    def __init__(self, lock_key):
        self._lock = consent_store._UpdateLock(_settings_lock_path(lock_key))
        self._held = False

    def __enter__(self):
        self._lock.__enter__()
        self._held = True
        return self

    def __exit__(self, *exc):
        self._held = False
        self._lock.__exit__(*exc)

    def _require_held(self):
        if not self._held:
            raise RuntimeError(
                "community settings writes require holding CommunityWriteContext"
            )

    def read(self, path):
        """Honest read of the current authority, inside the lock."""
        self._require_held()
        return load(path)

    def write(self, path, *, enabled, repository, project_roots, branch="main",
              idle_seconds=DEFAULT_IDLE_SECONDS, **extensions):
        """Managed mutation: fresh ``generation`` per call, ``enabled_at`` on
        the off->on edge. Field preservation merges over the CURRENT on-disk
        document read inside this lock — never over a caller's stale
        pre-lock snapshot. A corrupt or unreadable existing file fails with
        ValueError and is left byte-identical (no implicit repair); a missing
        file is a first configuration."""
        self._require_held()
        if "previous" in extensions:
            raise TypeError(
                "write() no longer accepts a stale 'previous' snapshot; the "
                "merge base is the current document read inside this lock"
            )
        target = Path(path)
        state = load(target)
        if state.state in ("corrupt", "unreadable") and not state.schema_ok:
            # The bytes are not a parseable mindie-community-config/1 document
            # at all: fail and preserve them — never an implicit repair. A
            # document that parses but carries malformed VALUES is rewritten
            # normally: an explicit managed mutation with a fresh generation
            # is exactly how such a config is repaired.
            raise ValueError(
                f"cannot write over a {state.state} community settings file"
            )
        data = dict(state.raw)
        was_enabled = state.raw.get("enabled") is True
        data.update({
            "schema": SCHEMA,
            "enabled": bool(enabled),
            "generation": secrets.token_hex(16),
            "enabled_at": (
                time.time() if enabled and not was_enabled
                else state.raw.get("enabled_at") if enabled else None
            ),
            "repository": repository,
            "branch": branch,
            "project_roots": [str(Path(root).expanduser().resolve(strict=False))
                              for root in project_roots],
            "idle_seconds": idle_seconds,
        })
        for key, value in extensions.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        _atomic_write(target, data)
        return load(target)

    def update_extensions(self, path, **fields):
        """Extension-key stamp (e.g. wiring ``consent_config``): sets each
        field (None removes it) without touching ``generation``,
        ``enabled_at`` or any managed key, so already-accepted work is never
        revoked by a stamp. Managed keys are rejected. The current document
        must validate through the one existing normalizer — a missing,
        corrupt, unreadable or non-conforming document fails with ValueError
        and is preserved byte-identical; nothing is fabricated or silently
        repaired."""
        self._require_held()
        if not fields:
            raise ValueError("update_extensions requires at least one field")
        overlap = _MANAGED_KEYS.intersection(fields)
        if overlap:
            raise ValueError(
                f"managed keys {sorted(overlap)} must go through write(), not a stamp"
            )
        target = Path(path)
        state = load(target)
        if state.state not in ("enabled", "disabled"):
            raise ValueError(
                f"cannot stamp a {state.state} community settings file"
            )
        try:
            normalize(state.raw)
        except ValueError as exc:
            raise ValueError(
                f"cannot stamp a non-conforming community settings file: {exc}"
            ) from None
        data = dict(state.raw)
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        _atomic_write(target, data)
        return load(target)


def _atomic_write(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    from mindie_knowledge.markdown import _atomic_write_text

    _atomic_write_text(target, json.dumps(data, indent=2) + "\n")
    try:
        target.chmod(0o600)
    except OSError:
        pass


def write(path, *, enabled, repository, project_roots, branch="main",
          idle_seconds=DEFAULT_IDLE_SECONDS, **extensions):
    """One-shot managed mutation: acquires the write boundary once.

    Every mutation sets a fresh nonempty opaque string ``generation`` so
    concurrent runtimes observe the change on their next read. Extension keys
    already present in the file (fork/bot/transaction/…) are preserved across
    toggles; one component must not silently delete another component's
    private configuration. Adapters migrating or writing across a
    legacy/canonical pair must drive ``CommunityWriteContext`` anchored at
    the profile-canonical path instead of this per-path convenience entry.
    """
    with CommunityWriteContext(path) as ctx:
        return ctx.write(
            path, enabled=enabled, repository=repository,
            project_roots=project_roots, branch=branch,
            idle_seconds=idle_seconds, **extensions,
        )


def update_extensions(path, **fields):
    """One-shot extension stamp: acquires the write boundary once.

    See ``CommunityWriteContext.update_extensions``. Never refreshes
    ``generation``: a stamp can neither revoke already-accepted work nor lose
    a concurrent user enable/disable — exactly one ordering is observable,
    and the locked read-merge preserves the other writer's fields in both.
    """
    with CommunityWriteContext(path) as ctx:
        return ctx.update_extensions(path, **fields)
