"""[Role] Normalize CANNBot OKF cards, skill runbooks and redacted transcript
notes into validated mindie-graded-unit/1 Markdown units.
[Invariants] Extraction and rendering are deterministic. A unit is returned
only after the graded parser accepts it; evidence strength is never upgraded
and private identifiers are scrubbed before validation.
[Timeouts & Locks] Pure CPU and bounded regular expressions; no I/O, locks,
network, model calls or subprocesses.
[Side-Effect Policy] Return values only. Invalid specs raise ValueError and
nothing is written to disk.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

import yaml

from . import units as _units
from .units import GradedUnit, TIER_CATEGORIES

_DEFAULT_STRENGTHS = frozenset({"hardware_spec", "software_contract", "reproduced_empirical",
                                "single_observation", "hypothesis"})
EVIDENCE_STRENGTHS = frozenset(getattr(_units, "EVIDENCE_STRENGTHS", getattr(_units, "STRENGTHS", _DEFAULT_STRENGTHS)))
SCHEMA = "mindie-graded-unit/1"
_CATEGORY_TO_TIER = {category: tier for tier, categories in TIER_CATEGORIES.items() for category in categories}

_CANN_CODE = re.compile(r"\bE[A-Z][0-9]{4}\b")
_RTS_CODE = re.compile(r"\b(?:507|107)[0-9]{3}\b")
_HCCL_CODE = re.compile(r"\bHCCL_E_[A-Z][A-Z0-9_]*\b")
_FAULT_PATTERNS = (("Notify wait timeout", r"\bnotify\s+wait\s+timeout\b"), ("p2p timeout", r"\bp2p\s+timeout\b"),
                   ("AICORE exception", r"\bAICORE\s+exception\b"), ("aic_error", r"\baic_error\b"),
                   ("vec_err_info", r"\bvec_err_info\b"), ("mte_err_info", r"\bmte_err_info\b"))
_FAULTS = tuple((re.compile(pattern, re.I), label) for label, pattern in _FAULT_PATTERNS)
_NPU_FIELD = re.compile(r"\b(?:device_id|stream_id|task_id)\b", re.I)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_USER_PATH = re.compile(r"(?<!\w)(?P<prefix>/export/home/|/data/home/|/Users/|/home/)"
                        r"(?P<name>[A-Za-z0-9._-][^/\s\"'`<>]*)")
_WINDOWS_PATH = re.compile(r"(?i)(?P<prefix>[A-Za-z]:[\\/]Users[\\/])(?P<name>[A-Za-z0-9._-][^\\/\s\"'<>]*)")
_TILDE_USER = re.compile(r"(?<![\w/])~(?P<name>[A-Za-z0-9._-][\w.-]*)")
_OKF_COMMENT = re.compile(r"<!--\s*OKF_META\b(?P<meta>.*?)-->", re.DOTALL | re.I)
_OKF_HEADING = re.compile(r"^##[ \t]+\[(?P<id>[^\]]+)\][ \t]*(?P<title>.*)$", re.MULTILINE)
_H2 = re.compile(r"^##[ \t]+", re.MULTILINE)
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")

_STRENGTH_ALIASES = {
    "hardware": "hardware_spec", "spec": "hardware_spec", "hardware_spec": "hardware_spec",
    "contract": "software_contract", "documented": "software_contract", "software_contract": "software_contract",
    "reproduced": "reproduced_empirical", "empirical": "reproduced_empirical", "verified": "reproduced_empirical",
    "reproduced_empirical": "reproduced_empirical", "single": "single_observation",
    "observed": "single_observation", "single_observation": "single_observation",
    "hypothesis": "hypothesis", "speculative": "hypothesis", "unverified": "hypothesis",
}
_CATEGORY_ALIASES = {"axiomatic": "axiom", "measure": "measurement", "procedure": "sop",
                     "standard_operating_procedure": "sop", "work_flow": "workflow", "mutation": "mutator"}
_QUALITY_CONFIDENCE = {"very_high": 0.95, "high": 0.85, "medium": 0.6, "low": 0.35,
                       "cold_start": 0.2, "unknown": 0.0}


def _sorted_unique(values): return sorted({str(value).strip() for value in values if str(value).strip()})


def _digest(value): return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _text(value):
    if value is None: return ""
    if isinstance(value, str): return value.strip()
    if isinstance(value, Mapping):
        for key in ("locator", "path", "url", "href", "source_locator", "value", "name"):
            if key in value: return _text(value[key])
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return ", ".join(_text(item) for item in value) if isinstance(value, (list, tuple, set)) else str(value).strip()


def _list(value, split=False):
    if value is None: return []
    if isinstance(value, (str, Mapping)): items = [value]
    elif isinstance(value, (set, frozenset)): items = sorted(value, key=str)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)): items = list(value)
    else: items = [value]
    out = []
    for item in items:
        if isinstance(item, Mapping):
            out.append(json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)); continue
        text = _text(item); parts = re.split(r"[,;]\s*", text) if split and isinstance(item, str) else [text]
        out.extend(part.strip() for part in parts if part.strip())
    return list(dict.fromkeys(out))


def _confidence(value):
    if isinstance(value, Mapping): value = next((value[key] for key in ("score", "value", "confidence") if key in value), 0)
    if isinstance(value, str):
        text = value.strip().lower()
        try: number = float(text[:-1]) / 100 if text.endswith("%") else float(text)
        except ValueError: return _QUALITY_CONFIDENCE.get(text.replace(" ", "_").replace("-", "_"), 0.0)
    else:
        try: number = float(value)
        except (TypeError, ValueError): return 0.0
    if number != number: return 0.0
    if 1 < number <= 100: number /= 100
    return max(0.0, min(1.0, number))


def _redact_value(value):
    if isinstance(value, str): return redact_private_identifiers(value)
    if isinstance(value, Mapping): return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_redact_value(item) for item in value]
    return value


def _normalized_keys(metadata):
    return {re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_"): value for key, value in metadata.items()}


def _pick(metadata, *names): return next((metadata[name] for name in names if metadata.get(name) not in (None, "")), None)


def _parse_okf_meta(raw):
    text = raw.strip().lstrip(":").strip()
    if not text: return {}
    try: value = yaml.safe_load(text)
    except yaml.YAMLError: value = None
    if not isinstance(value, dict):
        try: value = json.loads(text)
        except json.JSONDecodeError: value = None
    if isinstance(value, dict): return value
    result = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*[:=]\s*(.*)$", line.strip().rstrip(","))
        if match:
            item = match.group(2).strip().rstrip(",")
            result[match.group(1)] = item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'" else item
    if result: return result
    raise ValueError("OKF_META must contain a mapping")


def extract_ascend_error_signatures(text: str) -> dict[str, list[str]]:
    """Return sorted, deduplicated CANN/RTS/HCCL codes, fault phrases and NPU fields."""
    if not isinstance(text, str): raise TypeError("text must be a string")
    codes = [match.group(0) for pattern in (_CANN_CODE, _RTS_CODE, _HCCL_CODE) for match in pattern.finditer(text)]
    signatures = [label for pattern, label in _FAULTS if pattern.search(text)]
    keywords = [match.group(0).lower() for match in _NPU_FIELD.finditer(text)]
    return {"error_codes": _sorted_unique(codes), "error_signatures": _sorted_unique(signatures), "keywords": _sorted_unique(keywords)}


def _is_private_ipv4(value):
    try: octets = [int(part) for part in value.split(".")]
    except ValueError: return False
    if len(octets) != 4 or any(octet < 0 or octet > 255 for octet in octets): return False
    first, second = octets[0], octets[1]
    return (first == 10 or (first == 172 and 16 <= second <= 31) or (first == 192 and second == 168)
            or first == 127 or (first == 169 and second == 254) or (first == 100 and 64 <= second <= 127))


def _mask_ipv4(match):
    return f"<redacted:private-ipv4:{_digest(match.group(0))}>" if _is_private_ipv4(match.group(0)) else match.group(0)


def _mask_user_path(match): return f"{match.group('prefix')}<redacted:local-path:{_digest(match.group(0))}>"


def _mask_tilde_user(match): return f"~<redacted:local-path:{_digest(match.group(0))}>"


def redact_private_identifiers(text: str) -> str:
    """Deterministically scrub private IPv4 addresses and user-home paths."""
    if not isinstance(text, str): raise TypeError("text must be a string")
    text = _IPV4.sub(_mask_ipv4, text)
    text = _WINDOWS_PATH.sub(_mask_user_path, text)
    text = _USER_PATH.sub(_mask_user_path, text)
    return _TILDE_USER.sub(_mask_tilde_user, text)


def _version_list(value):
    if not isinstance(value, str): return _list(value)
    text = value.strip()
    return _list(re.split(r"[,;\n]+", text)) if text and not any(op in text for op in "<>=~^") else [text] if text else []


def parse_cannbot_okf_blocks(markdown_text: str) -> list[dict[str, Any]]:
    """Parse OKF_META cards, returning one normalized record per heading."""
    if not isinstance(markdown_text, str): raise TypeError("markdown_text must be a string")
    cards, used = [], set()
    for comment in _OKF_COMMENT.finditer(markdown_text):
        metadata = _normalized_keys(_parse_okf_meta(comment.group("meta")))
        heading = _OKF_HEADING.search(markdown_text, comment.end())
        if heading is None or heading.start() in used: continue
        following = _OKF_COMMENT.search(markdown_text, comment.end())
        if following is not None and following.start() < heading.start(): continue
        used.add(heading.start()); end = len(markdown_text)
        for match in (_OKF_COMMENT.search(markdown_text, heading.end()), _H2.search(markdown_text, heading.end())):
            if match is not None: end = min(end, match.start())
        body = re.sub(r"\n[ \t]*---[ \t]*$", "", markdown_text[heading.end():end].strip()).strip()
        heading_id, heading_title = heading.group("id").strip(), heading.group("title").strip()
        title = _text(_pick(metadata, "title", "name")) or heading_title or heading_id
        signals = extract_ascend_error_signatures("\n".join((comment.group("meta"), title, body)))
        cards.append({"okf_id": _text(_pick(metadata, "okf_id", "okfid", "id", "unit_id", "card_id")) or heading_id,
                      "title": title, "category": _text(_pick(metadata, "category", "kind", "type", "card_type")),
                      "confidence": _confidence(_pick(metadata, "confidence", "confidence_score", "score", "trust")),
                      "tags": _list(_pick(metadata, "tags", "tag", "labels", "topics"), split=True),
                      "source": _text(_pick(metadata, "source", "source_locator", "source_url", "locator", "url", "origin")),
                      "applicable_versions": _version_list(_pick(metadata, "applicable_versions", "applicable_version", "versions", "cann_versions")),
                      "body": body, **signals})
    return cards


def _required_text(value, field, single_line=False):
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"{field} requires nonempty text")
    return " ".join(value.split()) if single_line else value.strip()


def _tier(value):
    if value is None: return None
    text = str(value).strip().upper(); text = f"L{text}" if text in {"1", "2", "3", "4"} else text
    if text not in TIER_CATEGORIES: raise ValueError("tier must be L1, L2, L3 or L4")
    return text


def _category(value):
    if value is None: return None
    text = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return _CATEGORY_ALIASES.get(text, text)


def _strength(value):
    if value is None: return "single_observation"
    text = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if text not in _STRENGTH_ALIASES: raise ValueError("evidence_strength must map to " + ", ".join(sorted(EVIDENCE_STRENGTHS)))
    return _STRENGTH_ALIASES[text]


def _triggers(spec, title, body):
    raw = spec.get("triggers")
    if raw is None or raw == {}:
        found = extract_ascend_error_signatures("\n".join((title, body, _text(spec.get("raw_text")))))
        raw = {"error_signatures": found["error_codes"] + found["error_signatures"], "keywords": found["keywords"]}
    if isinstance(raw, (list, tuple)): raw = {"keywords": list(raw)}
    if not isinstance(raw, Mapping): raise ValueError("triggers must be a mapping")
    signatures = _list(raw.get("error_signatures"), split=True)
    for key in ("error_codes", "error_code", "signatures", "fault_signatures"): signatures += _list(raw.get(key), split=True)
    keywords = _list(raw.get("keywords"), split=True)
    for key in ("keyword", "fields"): keywords += _list(raw.get(key), split=True)
    return {"error_signatures": _sorted_unique(signatures), "keywords": _sorted_unique(keywords)}


def _inline(value):
    if value is None: return ""
    if isinstance(value, str): return " ".join(value.split())
    if isinstance(value, Mapping): return "; ".join(f"{key}: {_inline(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)): return "; ".join(_inline(item) for item in value)
    return str(value).strip()


def _render(value, numbered=False):
    if value is None: return ""
    if isinstance(value, str): return value.strip()
    if isinstance(value, Mapping): return "\n".join(f"- **{key}**: {_inline(item)}" for key, item in value.items() if _inline(item))
    if isinstance(value, (list, tuple)):
        return "\n".join((f"{index}. " if numbered else "- ") + _inline(item) for index, item in enumerate(value, 1) if _inline(item))
    return str(value).strip()


def _sections(value):
    if isinstance(value, Mapping): items = list(value.items())
    elif isinstance(value, str): items = [("", value)]
    elif isinstance(value, (list, tuple)):
        items = []
        for entry in value:
            if isinstance(entry, Mapping):
                heading = next((entry[key] for key in ("heading", "title", "name", "section") if key in entry), "")
                content = next((entry[key] for key in ("content", "body", "text", "value") if key in entry), "")
                if heading or content: items.append((heading, content))
            elif isinstance(entry, (list, tuple)) and len(entry) == 2: items.append(entry)
            else: items.append(("", entry))
    else: raise ValueError("body sections must be a mapping, list or text")
    parts = []
    for heading, content in items:
        text = _render(content, any(word in str(heading).lower() for word in ("procedure", "resolution", "step")))
        if not text: continue
        if heading:
            label = str(heading).strip()
            if not label.startswith("#"):
                label = "## " + " ".join(word if word.isupper() else word.capitalize() for word in label.replace("_", " ").split())
            parts.append(f"{label}\n\n{text}")
        else: parts.append(text)
    return "\n\n".join(parts).strip()


def _body(spec):
    for key in ("body", "body_markdown", "markdown_body", "content"):
        if spec.get(key):
            value = spec[key]
            return value.strip() if isinstance(value, str) else _sections(value)
    sections = spec.get("body_sections", spec.get("sections"))
    if sections: return _sections(sections)
    summary = spec.get("summary") or spec.get("description")
    items = [("Summary", summary)] if summary else []
    for key, heading in (("symptoms", "Symptoms"), ("root_cause", "Root Cause"), ("diagnosis", "Diagnosis"),
                         ("procedure", "Procedure"), ("steps", "Procedure"), ("resolution", "Resolution"),
                         ("verification", "Verification"), ("rollback", "Rollback"), ("notes", "Notes"), ("caveats", "Caveats")):
        if spec.get(key) not in (None, "", [], {}): items.append((heading, spec[key]))
    if items: return _sections(items)
    raise ValueError("distillation spec requires body or section content")


def _provenance(spec):
    raw = spec.get("provenance")
    if raw is None:
        raw = spec.get("source"); raw = {"source_locator": raw} if isinstance(raw, str) else raw
    if raw is None: raw = {}
    if not isinstance(raw, Mapping): raise ValueError("provenance must be a mapping")
    provenance = _redact_value(dict(raw))
    for key in ("source_type", "source_locator", "source_digest", "evidence_refs", "supersedes", "experiment_status", "redaction_profile"):
        if key not in provenance and key in spec: provenance[key] = _redact_value(spec[key])
    if "source_locator" not in provenance:
        for alias in ("locator", "url", "href", "source"):
            if alias in provenance: provenance["source_locator"] = _text(provenance.pop(alias)); break
    provenance["source_type"] = _text(provenance.get("source_type")) or "cannbot_okf"
    locator = _text(provenance.get("source_locator"))
    if not locator: raise ValueError("provenance.source_locator is required")
    provenance["source_locator"] = locator
    if "source_digest" in provenance and not _SHA256.fullmatch(_text(provenance["source_digest"])):
        raise ValueError("provenance.source_digest must be 64 lowercase hex characters")
    provenance["evidence_refs"] = _list(provenance.get("evidence_refs"), split=True)
    provenance["supersedes"] = _list(provenance.get("supersedes", []), split=True)
    return provenance


def _validate_unit_markdown(text):
    parser = getattr(_units, "parse_graded_unit_markdown", None)
    if callable(parser): parser(text)
    else: GradedUnit.from_text(text)


def synthesize_graded_unit_from_distillation(spec: Mapping[str, Any]) -> str:
    """Render and validate a mindie-graded-unit/1 unit from a distillation spec."""
    if not isinstance(spec, Mapping): raise ValueError("distillation spec must be a mapping")
    ident = _required_text(spec.get("id"), "id")
    tier, category = _tier(spec.get("tier")), _category(spec.get("category"))
    if tier is None and category is None: raise ValueError("tier or category is required")
    if tier is None: tier = _CATEGORY_TO_TIER.get(category)
    if category is None:
        candidates = TIER_CATEGORIES.get(tier, set())
        if len(candidates) != 1: raise ValueError("category is required for this tier")
        category = next(iter(candidates))
    if category not in TIER_CATEGORIES.get(tier, set()): raise ValueError("tier/category do not describe the same use")
    domain_path = _redact_value(_required_text(spec.get("domain_path"), "domain_path").rstrip("/"))
    title = _redact_value(_required_text(spec.get("title"), "title", True))
    body = _redact_value(_body(spec))
    if not isinstance(body, str) or not body.strip(): raise ValueError("distillation spec requires nonempty body content")
    revision = spec.get("revision", 1)
    if type(revision) is not int or revision < 1: raise ValueError("revision must be a positive integer")
    conditions = spec.get("conditions")
    if conditions is None: conditions = {"scope": "cannbot_distillation"}
    if not isinstance(conditions, Mapping) or not conditions: raise ValueError("conditions must be a nonempty mapping")
    metadata = {"schema": SCHEMA, "id": ident, "tier": tier, "category": category, "domain_path": domain_path,
                "title": title, "conditions": _redact_value(dict(conditions)),
                "inherits_from": _list(spec.get("inherits_from", []), split=True),
                "evidence_strength": _strength(spec.get("evidence_strength")), "provenance": _provenance(spec),
                "triggers": _redact_value(_triggers(spec, title, body)), "revision": revision}
    for key in ("status", "superseded_by"):
        if key in spec: metadata[key] = _redact_value(spec[key])
    unit = GradedUnit.from_parts(metadata, body, f"{domain_path}/{ident}.md")
    text = unit.render(); _validate_unit_markdown(text)
    return text
