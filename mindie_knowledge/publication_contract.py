"""One public data contract shared by producers, validators and consumers.

The declaration is untrusted data. It cannot choose code for a client to run.
Trusted repository CI selects executable validator code from its base commit;
the product release separately pins the exact declaration bytes it accepts.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

CONTRACT_FILE = "publication-contract.json"
PUBLICATION_CONTRACT_FILE = CONTRACT_FILE
SCHEMA = "mindie-publication-contract/1"
REVIEW_CONTRACT = "mindie-content-review/2"
FORMATS = {
    "package": "mindie-material-package/1",
    "task": "mindie-material-task/1",
    "block": "mindie-material-block/1",
    "entry": "mindie-entry/3",
    "feedback": "mindie-feedback/1",
}
MAX_CONTRACT_BYTES = 16 * 1024


class ContractMismatch(ValueError):
    """A read declaration does not describe the selected product/data format."""

    code = "contract_mismatch"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractMismatch(f"duplicate publication contract field: {key}")
        result[key] = value
    return result


def validate_contract(value, domain):
    """Validate a declaration without downloading, executing or modifying it."""
    if not isinstance(value, dict) or set(value) != {"schema", "domain", "formats", "validator", "review"}:
        raise ContractMismatch("publication contract has unsupported fields")
    if value["schema"] != SCHEMA or value["domain"] != domain:
        raise ContractMismatch("publication contract schema or domain does not match")
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", domain):
        raise ContractMismatch("publication contract domain is invalid")
    if value["formats"] != FORMATS:
        raise ContractMismatch("publication contract formats do not match the installed runtime")
    validator = value["validator"]
    if (not isinstance(validator, dict) or set(validator) != {"repository", "revision"}
            or validator["repository"] != "mindie-agent/knowledge"
            or not isinstance(validator["revision"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", validator["revision"])):
        raise ContractMismatch("publication validator must name an exact reviewed knowledge commit")
    if value["review"] != {"contract": REVIEW_CONTRACT, "check": "publication-head"}:
        raise ContractMismatch("publication review contract does not match")
    return json.loads(json.dumps(value))


def make_contract(domain, validator_revision):
    return validate_contract({
        "schema": SCHEMA, "domain": domain, "formats": dict(FORMATS),
        "validator": {"repository": "mindie-agent/knowledge", "revision": validator_revision},
        "review": {"contract": REVIEW_CONTRACT, "check": "publication-head"},
    }, domain)


def render_contract(value):
    return json.dumps(validate_contract(value, value.get("domain")), ensure_ascii=False,
                      indent=2, sort_keys=True) + "\n"


def parse_contract(raw, domain, expected_sha256=None):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CONTRACT_BYTES:
        raise ContractMismatch("publication contract is missing or exceeds its byte envelope")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ContractMismatch("configured publication contract digest is invalid")
        if digest != expected_sha256:
            raise ContractMismatch("publication contract differs from the selected product combination")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractMismatch("publication contract is not valid UTF-8 JSON") from exc
    return {"contract": validate_contract(value, domain), "sha256": digest}


def read_git_contract(repo, revision, domain, *, expected_sha256=None, deadline=None, prefix="", env=None, cancel=None):
    """Read one plain Git blob at an exact commit, never a checkout or symlink."""
    from .community.common import run_argv
    from .gitread import CatFileBatch

    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ContractMismatch("publication contract requires an immutable Git commit")
    if prefix and (prefix.startswith("/") or any(part in {"", ".", ".."} for part in prefix.split("/"))):
        raise ContractMismatch("invalid publication contract prefix")
    path = (prefix + "/" if prefix else "") + CONTRACT_FILE
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise TimeoutError("publication contract read exceeded its deadline")
    result = run_argv(["git", "-C", str(Path(repo)), "ls-tree", "-z", "--long", revision, "--", path],
                      timeout=remaining, max_output=MAX_CONTRACT_BYTES,
                      input_bytes=b"", env=env, cancel=cancel)
    if result.timed_out:
        raise TimeoutError("publication contract tree read timed out")
    if result.code:
        raise OSError("cannot read publication contract tree")
    records = [record for record in result.out.split(b"\0") if record]
    if not records:
        raise ContractMismatch("publication-contract.json is required before publishing or synchronizing")
    if len(records) != 1:
        raise ContractMismatch("publication contract path is ambiguous")
    try:
        meta, actual_path = records[0].decode("utf-8").split("\t", 1)
        mode, kind, oid, size = meta.split()
    except (UnicodeError, ValueError) as exc:
        raise ContractMismatch("publication contract tree record is malformed") from exc
    if mode != "100644" or kind != "blob" or actual_path != path or not size.isdigit():
        raise ContractMismatch("publication contract must be a plain non-executable Git blob")
    if not 0 < int(size) <= MAX_CONTRACT_BYTES:
        raise ContractMismatch("publication contract exceeds its byte envelope")
    reader = CatFileBatch(repo, env=env)
    try:
        raw = reader.read(oid, deadline=deadline, max_bytes=MAX_CONTRACT_BYTES, cancel=cancel)
    finally:
        reader.close()
    if raw is None:
        raise OSError("the listed publication contract blob could not be read")
    return parse_contract(raw, domain, expected_sha256)
