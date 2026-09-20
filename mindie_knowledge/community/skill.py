"""Skill candidates: deterministic selection, one bounded generation, plugin PR.

Candidate rule (a work-scheduling heuristic, not a quality proof): at least two
independent non-producer root-task up votes on the same entry revision. Work is
deduplicated by a material digest covering the source entry revision, the
relevant feedback and the target Skill — the same material is attempted at most
once, and genuinely new material is what becomes eligible again.

Generation calls the maintainer-configured review CLI once, with bounded
input/output and a deadline; the result is validated (frontmatter, reference
IDs, allowed package paths, explicit-only invocation metadata) and published as
an ordinary plugin-repository PR — only when a separately configured plugin
repository and its own credential are actually present. Absent permission is
reported as ``pending``, never as success.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml

from . import gitops
from .batch import check_path, validate_feedback
from .common import (
    MAX_DETAIL,
    CommunityError,
    Deadline,
    digest,
    run_argv,
    sha256_text,
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


# --------------------------------------------------------------------------- #
# Skill markdown validation (shared with the review runner's plugin profile)
# --------------------------------------------------------------------------- #


def validate_skill_markdown(text: str) -> dict[str, Any]:
    """Strict SKILL.md shape: frontmatter metadata, no implicit invocation."""
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
    if meta.get("allow_implicit_invocation") is True:
        raise ValueError("Skill invocation must stay explicit (allow_implicit_invocation: false)")
    refs = meta.get("source_entries", [])
    if refs is None:
        refs = []
    if not isinstance(refs, list) or not all(isinstance(r, str) and r.strip() for r in refs):
        raise ValueError("source_entries must be a list of entry ids")
    body = normalized[closing + 5 :].strip()
    if not body:
        raise ValueError("SKILL.md body must be nonempty")
    if re.search(r"/(?:home|Users)/[^\s]+", body):
        raise ValueError("SKILL.md must not embed author absolute paths")
    return {"name": name.strip(), "description": description.strip(),
            "source_entries": list(refs), "body": body}


def validate_openai_yaml(text: str) -> None:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("agents/openai.yaml must be a mapping")
    invocation = data.get("allow_implicit_invocation", False)
    if invocation is not False:
        raise ValueError("agents/openai.yaml must set allow_implicit_invocation: false")


# --------------------------------------------------------------------------- #
# Candidate selection from the content repository
# --------------------------------------------------------------------------- #


def _load_repo_state(repo, ref, transport, deadline) -> tuple[dict[str, dict], dict[str, list]]:
    """Entries by id and up-votes per (entry_id, revision) from real Git content."""
    entries: dict[str, dict] = {}
    votes: dict[tuple[str, str], list[dict]] = {}
    for prefix, kind in (("cases/", "experience"), ("topics/", "knowledge")):
        for path in transport.list_files(repo, prefix, ref, deadline)[:400]:
            text = transport.get_file(repo, path, ref, deadline)
            if not text:
                continue
            try:
                doc = parse_entry(text)
            except (ValueError, TypeError):
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


def _find_target_skill(plugin_repo, plugin_ref, entry_id, transport, deadline) -> str | None:
    """Prefer updating an existing Skill that already references the entry."""
    for path in transport.list_files(plugin_repo, "skills/", plugin_ref, deadline)[:200]:
        if not path.endswith("SKILL.md"):
            continue
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
            "\"skill_md\":..., \"openai_yaml\":..., \"references\": {name: markdown}}. "
            "Keep a short entry point and method; reference the entry id for detail. "
            "No scripts, no workflows, no author machine paths, no made-up tooling."
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
    if entry_id not in meta["source_entries"]:
        raise CommunityError("SKILL.md must reference the source entry id in source_entries")
    openai_yaml = data.get("openai_yaml")
    if openai_yaml is not None:
        if not isinstance(openai_yaml, str):
            raise CommunityError("openai_yaml must be text")
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
    files = [{"path": f"skills/{slug}/SKILL.md", "content": package["skill_md"]}]
    if package.get("openai_yaml"):
        files.append({"path": f"skills/{slug}/agents/openai.yaml", "content": package["openai_yaml"]})
    for name, content in package["references"].items():
        files.append({"path": f"skills/{slug}/references/{name}", "content": content})
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
              f"`{entry['revision']}`.\n\nPaths: `skills/{slug}/` only. Explicit invocation only."),
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
        entries, votes = _load_repo_state(repo, branch, transport, deadline)
        candidates = select_candidates(entries, votes)
        results = []
        for candidate in candidates[:10]:
            entry = candidate["entry"]
            plugin_repo = bot.get("plugin_repository")
            target = None
            if plugin_repo:
                target = _find_target_skill(plugin_repo, bot.get("plugin_branch", "main"),
                                            entry["entry_id"], transport, deadline)
            mat = material_digest(entry, candidate["revision"], candidate["supporters"], target)
            seen = ledger.get_skill_material(mat)
            if seen is not None:
                results.append({"entry_id": entry["entry_id"], "status": seen["status"],
                                "detail": "same material already attempted once; not repeating"})
                continue
            ledger.record_skill_material(mat, status="attempted", detail=entry["entry_id"])
            grok_argv = bot.get("grok_argv")
            if not grok_argv:
                ledger.record_skill_material(mat, status="pending", detail="no generation model configured")
                results.append({"entry_id": entry["entry_id"], "status": "pending",
                                "detail": "bot.grok_argv not configured; generation deferred"})
                continue
            try:
                generated = _generate_skill(grok_argv, candidate, target, bot)
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
    entries, _ = _load_repo_state(repo, branch, transport, deadline)
    retired = {eid: e for eid, e in entries.items() if e.get("status") == "retired"}
    corrections = []
    plugin_ref = bot.get("plugin_branch", "main")
    for path in transport.list_files(plugin_repo, "skills/", plugin_ref, deadline)[:200]:
        if not path.endswith("SKILL.md"):
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
