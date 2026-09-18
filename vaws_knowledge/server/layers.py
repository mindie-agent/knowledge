"""Locate the shared, project and candidate Markdown directories.

Layers label where reference material comes from; they do not rank its truth
or decide whether a task may proceed. Shared material is read-only. Capture
writes to the local candidate directory, which may start empty.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Only YAML configuration files need this optional import.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    yaml = None  # type: ignore

LAYERS: tuple[str, ...] = ("shared", "project", "candidate")

#: The write path exists for exactly one layer. See capture.py.
WRITABLE_LAYERS = frozenset({"candidate"})

ENV_CONFIG = "VAWS_KNOWLEDGE_CONFIG"
ENV_CORPUS = "VAWS_KNOWLEDGE_CORPUS"
ENV_LAYER_ROOTS = {
    "shared": "VAWS_KNOWLEDGE_SHARED_ROOTS",
    "project": "VAWS_KNOWLEDGE_PROJECT_ROOTS",
    "candidate": "VAWS_KNOWLEDGE_CANDIDATE_ROOT",
}
ENV_ENABLED_LAYERS = "VAWS_KNOWLEDGE_LAYERS"
ENV_IDENTITY = {
    "contributor": "VAWS_KNOWLEDGE_CONTRIBUTOR",
    "origin_repo": "VAWS_KNOWLEDGE_ORIGIN_REPO",
}
ENV_BACKEND = "VAWS_KNOWLEDGE_BACKEND"
ENV_STATE = "VAWS_KNOWLEDGE_STATE"

CONFIG_BASENAMES = ("vaws-knowledge.json", "vaws-knowledge.yaml", "vaws-knowledge.yml")

YAML_MISSING_HINT = (
    "PyYAML is required to read this YAML configuration. "
    "Use a JSON configuration or install PyYAML."
)

DEFAULT_IDENTITY = {
    "contributor": "anonymous",
    "origin_repo": "local/unpublished",
}

SOURCE_REPO = "mindie-agent/knowledge"


class ConfigError(Exception):
    """Raised only for a config file that cannot be interpreted at all."""


def resolve_shared_from_corpus(raw: str | Path) -> tuple[Path, ...]:
    """Accept a corpus directory or a checkout containing ``corpus/``."""

    path = Path(raw).expanduser()
    return (path / "corpus" if (path / "corpus").is_dir() else path,)


def default_shared_roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    env = os.environ if env is None else env
    corpus = env.get(ENV_CORPUS)
    if corpus:
        return resolve_shared_from_corpus(corpus)
    from vaws_knowledge.corpus import corpus_root

    return resolve_shared_from_corpus(corpus_root())


def shared_source() -> dict[str, str | None]:
    """Pin the packaged corpus to the installed commons commit, when known."""

    from vaws_knowledge.corpus import installed_commit

    return {"source_ref": installed_commit(), "source_repo": SOURCE_REPO}


def default_candidate_root() -> Path:
    """Local, untracked capture directory.

    Deliberately outside the checkout: candidate notes are per-developer state,
    not repository content, and a default inside the repo would eventually be
    committed by somebody.
    """

    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "vaws-knowledge" / "candidate"


@dataclass(frozen=True)
class Mount:
    """One layer, resolved against the filesystem."""

    layer: str
    roots: tuple[Path, ...] = ()
    read_only: bool = True
    configured: bool = False
    present: bool = False
    absent_reason: str | None = None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "layer": self.layer,
            "configured": self.configured,
            "present": self.present,
            "read_only": self.read_only,
            "roots": [str(p) for p in self.roots],
        }
        if self.absent_reason:
            out["absent_reason"] = self.absent_reason
        return out


@dataclass
class ServiceConfig:
    """Everything the query and capture paths need to run."""

    mounts: dict[str, Mount] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_IDENTITY))
    config_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    backend: str = "openviking"
    state_root: Path | None = None
    retrieval: Any = None
    publishing: dict[str, Any] = field(default_factory=dict)
    shared_sync: dict[str, Any] = field(default_factory=dict)
    catalog_options: dict[str, Any] = field(default_factory=dict)
    selection: dict[str, Any] = field(default_factory=dict)
    sources: list[Any] = field(default_factory=list)
    curation: dict[str, Any] = field(default_factory=dict)

    def mount(self, layer: str) -> Mount:
        return self.mounts.get(layer, Mount(layer=layer, absent_reason="unknown layer"))

    def _layer_available(self, layer: str) -> bool:
        mount = self.mount(layer)
        if mount.present and (layer == "candidate" or any(root.is_dir() for root in mount.roots)):
            return True
        # Explicitly disabled layers have no roots. An enabled shared source
        # may disappear after its independent OVPack has been imported.
        if layer != "shared" or not mount.roots:
            return False
        from vaws_knowledge.local.instance import instance_for_config
        from vaws_knowledge.local.shared import current_shared

        return bool(current_shared(instance_for_config(self).state_root))

    def available_layers(self) -> list[str]:
        return [name for name in LAYERS if self._layer_available(name)]

    def absent_layers(self, layers: Sequence[str] | None = None) -> dict[str, str]:
        wanted = [name for name in (layers or LAYERS) if name in LAYERS]
        out: dict[str, str] = {}
        for name in wanted:
            mount = self.mounts.get(name)
            if mount is None:
                out[name] = "not configured"
            elif not self._layer_available(name):
                out[name] = mount.absent_reason or "not present"
        return out

    def degraded(self, layers: Sequence[str] | None = None) -> bool:
        """True when a consulted layer is missing.

        Layers that were not requested do not affect completeness.
        """

        return bool(self.absent_layers(layers))

    def consulted(self, layers: Sequence[str] | None = None) -> dict[str, Any]:
        wanted = [name for name in (layers or LAYERS) if name in LAYERS]
        absent = self.absent_layers(wanted)
        return {
            "layers_available": [name for name in wanted if name not in absent],
            "layers_absent": absent,
            "degraded": bool(absent),
        }

    def describe(self) -> dict[str, Any]:
        from vaws_knowledge import package_version

        out = {
            "version": package_version(),
            "config_path": str(self.config_path) if self.config_path else None,
            "layers": {name: self.mount(name).describe() for name in LAYERS},
            "layers_available": self.available_layers(),
            "layers_absent": self.absent_layers(),
            "degraded": self.degraded(),
            "identity": dict(self.identity),
            "warnings": list(self.warnings),
        }
        out.update(shared_source())
        return out


def _read_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    if yaml is None:
        raise ConfigError(f"{path.name}: {YAML_MISSING_HINT}")
    return yaml.safe_load(text)


def find_config_file(env: Mapping[str, str], start: Path | None = None) -> Path | None:
    """Locate a config file: explicit env var first, then well-known names."""

    explicit = env.get(ENV_CONFIG)
    if explicit:
        return Path(explicit).expanduser()

    candidates: list[Path] = []
    cwd = start or Path.cwd()
    xdg = env.get("XDG_CONFIG_HOME")
    config_home = Path(xdg) if xdg else Path.home() / ".config"
    candidates.append(cwd / ".vaws-local" / "knowledge" / "service.json")
    for basename in CONFIG_BASENAMES:
        candidates.append(cwd / basename)
        candidates.append(cwd / ".vaws" / basename)
        candidates.append(config_home / "vaws-knowledge" / basename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _split_paths(raw: str) -> list[str]:
    parts: list[str] = []
    for chunk in raw.split(os.pathsep):
        for piece in chunk.split(","):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return parts


def _resolve(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def _candidate_root_problem(path: Path) -> str | None:
    """None when the root is an empty-or-readable write target.

    A path that has never been created is the normal initial state. A path
    that exists but cannot be listed is a real gap.
    """

    try:
        if not path.exists():
            return None
        if not path.is_dir():
            return f"path is not a directory: {path}"
        os.listdir(path)
    except OSError as exc:
        return f"cannot read {path}: {exc}"
    return None


def _candidate_mount(
    roots: tuple[Path, ...],
    *,
    read_only: bool,
    configured: bool,
) -> Mount:
    problems = [reason for path in roots if (reason := _candidate_root_problem(path))]
    if problems and len(problems) == len(roots):
        return Mount(
            layer="candidate",
            roots=roots,
            read_only=read_only,
            configured=True,
            present=False,
            absent_reason="; ".join(problems),
        )
    return Mount(
        layer="candidate",
        roots=roots,
        read_only=read_only,
        configured=configured or True,
        present=True,
        absent_reason=None if not problems else "some configured roots cannot be read: " + "; ".join(problems),
    )


def _layer_spec(raw: Any) -> dict[str, Any]:
    """Normalize the several shapes a layer may be written in."""

    if raw is None:
        return {}
    if isinstance(raw, str):
        return {"roots": [raw]}
    if isinstance(raw, (list, tuple)):
        return {"roots": list(raw)}
    if isinstance(raw, Mapping):
        spec = dict(raw)
        roots = spec.pop("roots", None)
        single = spec.pop("root", None)
        collected: list[str] = []
        if isinstance(roots, str):
            collected.append(roots)
        elif isinstance(roots, (list, tuple)):
            collected.extend(str(item) for item in roots)
        if isinstance(single, str):
            collected.append(single)
        spec["roots"] = collected
        return spec
    raise ConfigError(f"layer configuration must be a string, list or mapping, got {type(raw).__name__}")


def load_config(
    mapping: Mapping[str, Any] | None = None,
    *,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    base_dir: str | os.PathLike[str] | None = None,
) -> ServiceConfig:
    """Build a :class:`ServiceConfig`.

    Precedence, lowest first: built-in defaults, config file, ``mapping``,
    environment variables. Relative paths resolve against ``base_dir`` when
    supplied, otherwise the config file's directory or the process cwd.

    Never raises for a missing or absent layer. Only a config file that cannot
    be parsed at all raises :class:`ConfigError`.
    """

    env = dict(os.environ if env is None else env)
    warnings: list[str] = []

    config_path: Path | None = None
    file_data: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path).expanduser()
    elif mapping is None:
        config_path = find_config_file(env, start=Path(base_dir) if base_dir else None)

    if config_path is not None:
        if config_path.is_file():
            try:
                loaded = _read_structured(config_path)
            except ConfigError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, Mapping):
                raise ConfigError(f"config {config_path} must contain a mapping")
            file_data = dict(loaded)
        else:
            warnings.append(f"config file not found, using defaults: {config_path}")
            config_path = None

    data: dict[str, Any] = dict(file_data)
    if mapping:
        for key, value in mapping.items():
            if key == "layers" and isinstance(value, Mapping) and isinstance(data.get("layers"), Mapping):
                merged = dict(data["layers"])
                merged.update(value)
                data["layers"] = merged
            else:
                data[key] = value

    if base_dir is not None:
        base = Path(base_dir).expanduser()
    elif config_path is not None:
        base = config_path.parent
    else:
        base = Path.cwd()

    identity = dict(DEFAULT_IDENTITY)
    for key in DEFAULT_IDENTITY:
        value = (data.get("identity") or {}).get(key)
        if value is not None:
            identity[key] = str(value)
    for key, var in ENV_IDENTITY.items():
        if env.get(var):
            identity[key] = env[var]

    layers_cfg_raw = data.get("layers") or {}
    if not isinstance(layers_cfg_raw, Mapping):
        raise ConfigError("'layers' must be a mapping of layer name to configuration")

    enabled_filter: set[str] | None = None
    if env.get(ENV_ENABLED_LAYERS) is not None:
        enabled_filter = {name.strip() for name in env[ENV_ENABLED_LAYERS].split(",") if name.strip()}

    for unknown in set(layers_cfg_raw) - set(LAYERS):
        warnings.append(f"ignoring unknown layer in config: {unknown}")

    mounts: dict[str, Mount] = {}
    for layer in LAYERS:
        try:
            spec = _layer_spec(layers_cfg_raw.get(layer))
        except ConfigError as exc:
            warnings.append(f"{layer}: {exc}; treating layer as unconfigured")
            spec = {}

        roots_raw = [str(item) for item in spec.get("roots", []) if str(item).strip()]
        configured = bool(roots_raw) or "enabled" in spec

        env_var = ENV_LAYER_ROOTS[layer]
        if env_var in env:
            env_value = env[env_var]
            if env_value.strip() == "":
                mounts[layer] = Mount(
                    layer=layer,
                    configured=True,
                    read_only=layer not in WRITABLE_LAYERS,
                    absent_reason=f"disabled by {env_var} (empty value)",
                )
                continue
            roots_raw = _split_paths(env_value)
            configured = True

        enabled = spec.get("enabled", True)
        if enabled_filter is not None and layer not in enabled_filter:
            mounts[layer] = Mount(
                layer=layer,
                configured=configured,
                read_only=layer not in WRITABLE_LAYERS,
                absent_reason=f"disabled by {ENV_ENABLED_LAYERS}",
            )
            continue
        if not enabled:
            mounts[layer] = Mount(
                layer=layer,
                configured=True,
                read_only=layer not in WRITABLE_LAYERS,
                absent_reason="disabled in configuration",
            )
            continue

        if not roots_raw:
            if layer == "shared":
                roots = default_shared_roots(env)
            elif layer == "candidate":
                roots = (default_candidate_root(),)
            else:
                mounts[layer] = Mount(
                    layer=layer,
                    configured=False,
                    read_only=True,
                    absent_reason="not configured (no roots supplied)",
                )
                continue
        else:
            roots = tuple(_resolve(item, base) for item in roots_raw)

        existing = tuple(p for p in roots if p.is_dir())
        # Capture writes only to candidate; shared and project are read-only
        # through the service.
        read_only = bool(spec.get("read_only", layer not in WRITABLE_LAYERS))
        if layer != "candidate":
            read_only = True

        if layer == "candidate":
            mounts[layer] = _candidate_mount(roots, read_only=read_only, configured=configured)
            continue

        if existing:
            mounts[layer] = Mount(
                layer=layer,
                roots=existing,
                read_only=read_only,
                configured=configured or layer == "shared",
                present=True,
                absent_reason=None
                if len(existing) == len(roots)
                else "some configured roots do not exist: "
                + ", ".join(str(p) for p in roots if p not in existing),
            )
        else:
            mounts[layer] = Mount(
                layer=layer,
                roots=roots,
                read_only=read_only,
                configured=configured or layer == "shared",
                present=False,
                absent_reason="path does not exist: " + ", ".join(str(p) for p in roots),
            )

    backend = str(
        data.get("backend") or env.get(ENV_BACKEND) or "openviking"
    ).strip().lower() or "openviking"
    state_raw = env.get(ENV_STATE) or data.get("state_root")
    if state_raw:
        state_root = _resolve(str(state_raw), base)
    else:
        candidate = mounts.get("candidate")
        if candidate and candidate.roots:
            state_root = Path(candidate.roots[0]).parent / "instance"
        else:
            state_root = default_candidate_root().parent / "instance"

    return ServiceConfig(
        mounts=mounts,
        identity=identity,
        config_path=config_path,
        warnings=warnings,
        backend=backend,
        state_root=state_root,
        publishing=dict(data["publishing"]) if isinstance(data.get("publishing"), Mapping) else {},
        shared_sync=dict(data["shared_sync"]) if isinstance(data.get("shared_sync"), Mapping) else {},
        catalog_options=dict(data["catalog"]) if isinstance(data.get("catalog"), Mapping) else {},
        selection=dict(data["selection"]) if isinstance(data.get("selection"), Mapping) else {},
        sources=list(data["sources"]) if isinstance(data.get("sources"), list) else [],
        curation=dict(data["curation"]) if isinstance(data.get("curation"), Mapping) else {},
    )
