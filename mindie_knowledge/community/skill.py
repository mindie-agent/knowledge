"""Skill candidates: deterministic selection, one bounded generation, plugin PR.

Candidate rule (a work-scheduling heuristic, not a quality proof): at least two
independent non-producer root-task up votes on the same entry revision. Work is
deduplicated by a material digest covering the source entry revision, the
relevant feedback and the target Skill — the same material is attempted at most
once, and genuinely new material is what becomes eligible again.

Generation calls the maintainer-configured adapter once (bounded input/output,
deadline, owned process); the result is validated against the native Skill
package rules (frontmatter metadata, ``agents/openai.yaml`` with
``policy.allow_implicit_invocation: false``, reference files) and published as
an ordinary plugin-repository PR under the configured Skill prefix
(``bot.skill_prefix``, default ``plugins/mindie-agent/skills``) — only when a
separately configured plugin repository and its own credential are actually
present. Absent permission is reported as ``pending``, never as success.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml

from . import gitops
from .batch import validate_feedback
from .common import (
    CommunityError,
    Deadline,
    digest,
    run_argv,
)
from .entrydoc import parse_entry
from .ledger import Ledger
from .settings import validate_settings
from .transport import Transport, transport_from_settings

SCHEMA_SKILL = "mindie-skill/1"
MIN_INDEPENDENT_UPS = 2
SLUG_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_REFERENCE_BYTES = 64 * 1024
MAX_REFERENCES = 8
DEFAULT_SKILL_PREFIX = "plugins/mindie-agent/skills"
#: Frontmatter metadata key carrying comma-joined source entry ids (string
#: value, per the supported SKILL.md metadata mapping).
SOURCE_METADATA_KEY = "mindie_source_entries"


def skill_prefix(settings: Mapping[str, Any]) -> str:
    return (settings.get("bot") or {}).get("skill_prefix") or DEFAULT_SKILL_PREFIX


def check_skill_package_path(name: str, prefix: str) -> None:
    """Allowed plugin PR paths: <prefix>/<slug>/{SKILL.md, agents/openai.yaml,
    references/*.md} and nothing else — no executables, hooks or workflows."""
    pure = PurePosixPath(name)
    parts = pure.parts
    prefix_parts = PurePosixPath(prefix).parts
    if parts[: len(prefix_parts)] != prefix_parts:
        raise CommunityError(f"{name}: Skill packages live under {prefix}/<slug>/ only")
    tail_parts = parts[len(prefix_parts):]
    if len(tail_parts) < 2:
        raise CommunityError(f"{name}: Skill packages live under {prefix}/<slug>/ only")
    tail = tail_parts[-1]
    if tail == "SKILL.md" and len(tail_parts) == 2:
        return
    if tail == "openai.yaml" and tail_parts[-2] == "agents" and len(tail_parts) == 3:
        return
    if tail_parts[-2] == "references" and tail.endswith(".md") and len(tail_parts) == 3:
        return
    raise CommunityError(
        f"{name}: not an allowed Skill package path (SKILL.md, agents/openai.yaml, references/*.md)"
    )


# --------------------------------------------------------------------------- #
# Skill package validation (native format; shared with the review runner)
# --------------------------------------------------------------------------- #


def validate_skill_markdown(text: str) -> dict[str, Any]:
    """Strict SKILL.md shape: name/description frontmatter, metadata refs."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("SKILL.md must be nonempty")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        raise ValueError("SKILL.md exceeds the size limit")
    if not normalized.startswith("---\n"):
        raise ValueError("SKILL.md must start with a YAML frontmatter block")
    closing = normalized.find("\n---\n", 4)
    if closing == -1:
        raise ValueError("SKILL.md frontmatter is not closed")
    meta = yaml.safe_load(normalized[4:closing])
    if not isinstance(meta, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    name = meta.get("name")
    if not isinstance(name, str) or not SLUG_RE.fullmatch(name.strip()):
        raise ValueError("SKILL.md frontmatter needs a slug-shaped name")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("SKILL.md frontmatter needs a description")
    if len(description) > 1024:
        raise ValueError("SKILL.md description is too long")
    metadata = meta.get("metadata") or {}
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        raise ValueError("frontmatter metadata must map strings to strings")
    raw_refs = metadata.get(SOURCE_METADATA_KEY, "")
    refs = [r.strip() for r in raw_refs.split(",") if r.strip()]
    body = normalized[closing + 5 :].strip()
    if not body:
        raise ValueError("SKILL.md body must be nonempty")
    if re.search(r"/(?:home|Users)/[^\s]+", body):
        raise ValueError("SKILL.md must not embed author absolute paths")
    return {"name": name.strip(), "description": description.strip(),
            "source_entries": refs, "body": body}


def validate_openai_yaml(text: str) -> None:
    """The native agents/openai.yaml policy: implicit invocation explicitly off.

    The harness default is TRUE when the policy is absent, so a missing or
    misplaced key silently yields an implicit Skill — reject both.
    """
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("agents/openai.yaml must be a mapping")
    policy = data.get("policy")
    if not isinstance(policy, dict) or policy.get("allow_implicit_invocation") is not False:
        raise ValueError(
            "agents/openai.yaml must set policy.allow_implicit_invocation: false explicitly"
        )


def render_openai_yaml() -> str:
    return "policy:\n  allow_implicit_invocation: false\n"


# --------------------------------------------------------------------------- #
# Candidate selection from the content repository
# --------------------------------------------------------------------------- #


def _load_repo_state(repo, ref, transport, deadline) -> tuple[dict[str, dict], dict[tuple[str, str], list]]:
    """Entries by id and up-votes per (entry_id, revision) from real Git content."""
    entries: dict[str, dict] = {}
    votes: dict[tuple[str, str], list] = {}
    for prefix, kind in (("cases/", "experience"), ("topics/", "knowledge")):
        for path in transport.list_files(repo, prefix, ref, deadline)[:400]:
            text = transport.get_file(repo, path, ref, deadline)
            if not text:
                continue
            try:
                doc = parse_entry(text)
            except (ValueError, TypeError, RuntimeError):
                continue
            entries[doc["entry_id"]] = {**doc, "path": path}
    for path in transport.list_files(repo, "feedback/", ref, deadline)[:400]:
        text = transport.get_file(repo, path, ref, deadline)
        if not text:
            continue
        try:
            feedback = validate_feedback(text, path)
        except CommunityError:
            continue
        for vote in feedback["votes"]:
            votes.setdefault((vote["entry_id"], vote["revision"]), []).append(vote)
    return entries, votes


def select_candidates(entries: Mapping[str, dict], votes: Mapping[tuple[str, str], list]) -> list[dict[str, Any]]:
    """Deterministic cheap selection: >=2 independent non-producer root ups."""
    out = []
    for (entry_id, revision), entry_votes in sorted(votes.items()):
        entry = entries.get(entry_id)
        if entry is None or entry.get("status") != "active":
            continue
        if entry.get("revision") != revision:
            continue  # votes bind to the exact content revision
        producers = set(entry.get("producers") or [])
        supporters = sorted({v["root_id"] for v in entry_votes
                             if v["rating"] == "up" and v["root_id"] not in producers})
        if len(supporters) >= MIN_INDEPENDENT_UPS:
            out.append({"entry": entry, "revision": revision, "supporters": supporters})
    return out


def material_digest(entry: Mapping[str, Any], revision: str, supporters: list[str], target_skill: str | None) -> str:
    return digest({
        "schema": SCHEMA_SKILL,
        "entry_id": entry["entry_id"],
        "revision": revision,
        "supporters": sorted(supporters),
        "target_skill": target_skill,
    })


def _find_target_skill(plugin_repo, plugin_ref, entry_id, prefix, transport, deadline) -> str | None:
    """Prefer updating an existing Skill that already references the entry."""
    for path in transport.list_files(plugin_repo, prefix + "/", plugin_ref, deadline)[:400]:
        if not path.endswith("SKILL.md"):
            continue
        if "profiling-analysis" in path:
            continue  # explicitly deferred Skill: never inspected
        text = transport.get_file(plugin_repo, path, plugin_ref, deadline)
        if not text:
            continue
        try:
            meta = validate_skill_markdown(text)
        except (ValueError, TypeError):
            continue
        if entry_id in meta["source_entries"] or entry_id in text:
            return meta["name"]
    return None


# --------------------------------------------------------------------------- #
# One bounded generation + plugin PR
# --------------------------------------------------------------------------- #


def _generate_skill(argv: list[str], candidate: Mapping[str, Any], existing_skill: str | None,
                    bot: Mapping[str, Any]) -> dict[str, Any]:
    entry = candidate["entry"]
    payload = {
        "task": "mindie-skill-consolidation",
        "instructions": (
            "Turn this validated community experience into a reusable Skill package. "
            "Reply with ONE JSON object: {\"schema\":\"mindie-skill/1\",\"slug\":...,"
            "\"skill_md\":..., \"references\": {name: markdown}}. The SKILL.md must "
            "carry name/description frontmatter plus a metadata mapping with "
            f"{SOURCE_METADATA_KEY} listing the source entry id; agents/openai.yaml "
            "is generated deterministically, do not produce one. Keep a short entry "
            "point and method; reference the entry id for detail. No scripts, no "
            "workflows, no author machine paths, no made-up tooling."
        ),
        "entry": {
            "entry_id": entry["entry_id"],
            "title": entry["title"],
            "summary": entry["summary"],
            "conditions": entry.get("conditions") or {},
            "content": entry["content"],
        },
        "supporters": candidate["supporters"],
        "existing_skill": existing_skill,
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    limit = bot.get("review_input_bytes", 64 * 1024)
    if len(raw) > limit:
        payload["entry"]["content"] = payload["entry"]["content"].encode("utf-8")[: limit // 2].decode(
            "utf-8", "ignore") + "\n...[truncated]"
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")[:limit]
    timeout = bot.get("review_timeout_seconds", 300)
    result = run_argv([str(a) for a in argv], timeout=timeout,
                      max_output=bot.get("review_output_bytes", 128 * 1024), input_bytes=raw)
    if result.timed_out:
        raise CommunityError(f"skill generation exceeded the {timeout}s deadline; attempt consumed")
    if result.code != 0:
        raise CommunityError(f"skill generation exited {result.code}; attempt consumed")
    try:
        data = json.loads(result.out_text)
    except json.JSONDecodeError:
        raise CommunityError("skill generation did not return JSON; attempt consumed")
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_SKILL:
        raise CommunityError("skill generation must answer with schema mindie-skill/1")
    return data


def validate_skill_package(data: Mapping[str, Any], entry_id: str) -> dict[str, Any]:
    slug = data.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug.strip()):
        raise CommunityError("skill slug must be lowercase slug text")
    slug = slug.strip()
    skill_md = data.get("skill_md")
    if not isinstance(skill_md, str):
        raise CommunityError("skill_md must be text")
    meta = validate_skill_markdown(skill_md)
    if entry_id not in meta["source_entries"] and entry_id not in meta["body"]:
        raise CommunityError("SKILL.md must reference the source entry id")
    # agents/openai.yaml is never model output: deterministic native policy.
    openai_yaml = render_openai_yaml()
    validate_openai_yaml(openai_yaml)
    references = data.get("references") or {}
    if not isinstance(references, Mapping) or len(references) > MAX_REFERENCES:
        raise CommunityError("references must be a mapping of at most 8 files")
    checked_refs: dict[str, str] = {}
    for name, content in references.items():
        pure = PurePosixPath(str(name))
        if pure.is_absolute() or ".." in pure.parts or not str(name).endswith(".md"):
            raise CommunityError(f"reference name {name!r} is not an allowed markdown file")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_REFERENCE_BYTES:
            raise CommunityError(f"reference {name!r} exceeds the size limit")
        checked_refs[pure.name] = content
    return {"slug": slug, "skill_md": skill_md, "openai_yaml": openai_yaml,
            "references": checked_refs}


def publish_skill_pr(settings, state_dir, transport, deadline, package, entry, ledger) -> dict[str, Any]:
    """Open or update the pending Skill PR in the separately configured plugin repo."""
    bot = settings.get("bot") or {}
    plugin_repo = bot.get("plugin_repository")
    if not plugin_repo:
        return {"status": "pending",
                "detail": "plugin repository is not configured (bot.plugin_repository); "
                          "skill package validated but not published"}
    token_env = bot.get("plugin_token_env") or settings.get("token_env", "GH_TOKEN")
    if settings.get("transport", "gh") == "gh" and not os.environ.get(token_env):
        return {"status": "pending",
                "detail": f"plugin credential env {token_env} is not set; "
                          "skill package validated but not published"}
    prefix = skill_prefix(settings)
    slug = package["slug"]
    branch = f"mindie-skill/{slug}"
    remote_url = _remote_url(settings, plugin_repo)
    work = gitops.ensure_clone(remote_url, state_dir / "skill" / plugin_repo.replace("/", "_"), deadline)
    base_ref = f"origin/{bot.get('plugin_branch', 'main')}"
    prior = transport.find_pull_requests(plugin_repo, head_branch=branch, deadline=deadline)
    open_prior = next((p for p in prior if p.get("state") == "open"), None)
    if open_prior:
        if not gitops.checkout_existing(work, branch, deadline):
            raise CommunityError("pending Skill PR branch missing on the plugin remote",
                                 status="needs_review")
    else:
        gitops.checkout_new(work, branch, base_ref, deadline)
    files = [{"path": f"{prefix}/{slug}/SKILL.md", "content": package["skill_md"]},
             {"path": f"{prefix}/{slug}/agents/openai.yaml", "content": package["openai_yaml"]}]
    for name, content in package["references"].items():
        files.append({"path": f"{prefix}/{slug}/references/{name}", "content": content})
    gitops.apply_files(work, files)
    message = (f"mindie-skill: consolidate {entry['entry_id']} into {slug}\n\n"
               f"source-entry: {entry['entry_id']}@{entry['revision'][:12]}\n")
    committed = gitops.stage_and_commit(work, [f["path"] for f in files], message, deadline)
    if committed is None and open_prior:
        return {"status": "unchanged", "pr_url": open_prior.get("html_url"),
                "detail": "pending Skill PR already carries this package"}
    if committed is None:
        return {"status": "unchanged", "detail": "skill package already on the plugin branch"}
    gitops.push_branch(work, branch, deadline)
    if open_prior:
        return {"status": "updated", "pr_url": open_prior.get("html_url"),
                "head_sha": committed, "detail": "updated the pending Skill PR in place"}
    pr = transport.create_pull_request(
        plugin_repo,
        title=f"[mindie] skill: {slug} (from {entry['entry_id'][:12]})",
        body=(f"Consolidated from community entry `{entry['entry_id']}` revision "
              f"`{entry['revision']}`.\n\nPaths: `{prefix}/{slug}/` only. "
              "Explicit invocation only (`policy.allow_implicit_invocation: false`)."),
        head=branch, base=bot.get("plugin_branch", "main"), deadline=deadline,
    )
    return {"status": "submitted", "pr_url": pr.get("html_url"), "head_sha": committed,
            "detail": f"opened plugin PR #{pr.get('number')}"}


def scan_skill_candidates(settings: dict, state_dir: Path, *, transport: Transport | None = None,
                          cancel: Any = None) -> dict[str, Any]:
    """Evaluate candidates from real repo content; generate/publish when warranted."""
    settings = validate_settings(settings) if settings.get("schema") else dict(settings)
    repo = settings.get("repository")
    if not repo:
        raise CommunityError("skill scan requires a configured content repository")
    state_dir = Path(state_dir)
    ledger = Ledger(state_dir)
    try:
        deadline = Deadline(settings.get("transaction_seconds", 120) * 3,
                            settings.get("operation_limit", 60), cancel=cancel)
        transport = transport or transport_from_settings(settings, state_dir)
        bot = settings.get("bot") or {}
        branch = settings.get("branch", "main")
        prefix = skill_prefix(settings)
        entries, votes = _load_repo_state(repo, branch, transport, deadline)
        candidates = select_candidates(entries, votes)
        results = []
        for candidate in candidates[:10]:
            entry = candidate["entry"]
            plugin_repo = bot.get("plugin_repository")
            target = None
            if plugin_repo:
                target = _find_target_skill(plugin_repo, bot.get("plugin_branch", "main"),
                                            entry["entry_id"], prefix, transport, deadline)
            mat = material_digest(entry, candidate["revision"], candidate["supporters"], target)
            seen = ledger.get_skill_material(mat)
            if seen is not None:
                results.append({"entry_id": entry["entry_id"], "status": seen["status"],
                                "detail": "same material already attempted once; not repeating"})
                continue
            ledger.record_skill_material(mat, status="attempted", detail=entry["entry_id"])
            skill_argv = bot.get("skill_grok_argv")
            if not skill_argv:
                ledger.record_skill_material(mat, status="pending",
                                             detail="no skill generation adapter configured")
                results.append({"entry_id": entry["entry_id"], "status": "pending",
                                "detail": "bot.skill_grok_argv not configured; generation deferred"})
                continue
            try:
                generated = _generate_skill(skill_argv, candidate, target, bot)
                package = validate_skill_package(generated, entry["entry_id"])
                published = publish_skill_pr(settings, state_dir, transport, deadline,
                                             package, entry, ledger)
            except CommunityError as exc:
                ledger.record_skill_material(mat, status=exc.status if exc.status != "unknown" else "failed",
                                             detail=str(exc))
                results.append({"entry_id": entry["entry_id"], "status": "failed",
                                "detail": str(exc)})
                continue
            ledger.record_skill_material(mat, status=published["status"],
                                         detail=published.get("detail", ""))
            results.append({"entry_id": entry["entry_id"], "material": mat, **published})
        return {"candidates": len(candidates), "results": results,
                "operations": deadline.operations_used}
    finally:
        ledger.close()


def retirement_corrections(settings: dict, state_dir: Path, *, transport: Transport | None = None) -> dict[str, Any]:
    """Locate Skills referencing retired entries and open bounded correction PRs."""
    settings = validate_settings(settings) if settings.get("schema") else dict(settings)
    repo = settings.get("repository")
    bot = settings.get("bot") or {}
    plugin_repo = bot.get("plugin_repository")
    if not repo or not plugin_repo:
        return {"status": "pending", "corrections": [],
                "detail": "retirement correction needs both content and plugin repositories"}
    state_dir = Path(state_dir)
    transport = transport or transport_from_settings(settings, state_dir)
    deadline = Deadline(settings.get("transaction_seconds", 120) * 2,
                        settings.get("operation_limit", 60))
    branch = settings.get("branch", "main")
    prefix = skill_prefix(settings)
    entries, _ = _load_repo_state(repo, branch, transport, deadline)
    retired = {eid: e for eid, e in entries.items() if e.get("status") == "retired"}
    corrections = []
    plugin_ref = bot.get("plugin_branch", "main")
    for path in transport.list_files(plugin_repo, prefix + "/", plugin_ref, deadline)[:400]:
        if not path.endswith("SKILL.md") or "profiling-analysis" in path:
            continue
        text = transport.get_file(plugin_repo, path, plugin_ref, deadline)
        if not text:
            continue
        try:
            meta = validate_skill_markdown(text)
        except (ValueError, TypeError):
            continue
        hits = [eid for eid in meta["source_entries"] if eid in retired]
        if not hits:
            continue
        corrections.append({"skill": meta["name"], "path": path, "retired_entries": hits})
    return {"status": "identified", "corrections": corrections[:20],
            "detail": "dependent Skills located; bounded correction PRs are opened by the "
                      "maintainer-approved plugin path"}


def _remote_url(settings: Mapping[str, Any], repo: str) -> str:
    dev = settings.get("dev_remotes") or {}
    if repo in dev:
        return dev[repo]
    return f"https://github.com/{repo}.git"
