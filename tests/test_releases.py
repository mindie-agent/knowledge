"""Release eligibility is pure and cannot select an unverified repository."""

from copy import deepcopy

import pytest

from mindie_knowledge.releases import (
    ReleaseValidationError, select_release, validate_compatibility,
    validate_release_metadata, supports_harness,
)


REPO = "mindie-agent/mindie-agent-codex"


def interval(minimum="0.153.4", maximum=None, *, inclusive=True, prerelease=False):
    return dict(minimum=minimum, maximum=maximum or minimum,
                maximum_inclusive=inclusive, include_prerelease=prerelease)


def compatibility(supported=None):
    supported = supported or [interval()]
    return dict(
        schema="mindie-harness-compatibility/1", harness="codex",
        supported=deepcopy(supported), used_interfaces=["hooks.stop"],
        interface_registry={"hooks.stop": dict(
            supported=deepcopy(supported), code_paths=["scripts/entry.py:stop"],
            test_ids=["tests/test_stop.py::test_native_stop"],
            evidence_refs=["https://github.com/openai/codex/tree/rust-v0.153.4"],
        )},
    )


def release(version="0.0.1", ident=1, supported=None):
    return dict(
        id=ident, tag_name="v" + version, draft=False, prerelease=False,
        metadata=dict(
            schema="mindie-plugin-release/1", version=version,
            source=dict(repository=REPO, sha="a" * 40),
            compatibility=compatibility(supported),
        ),
    )


def select(records, **kw):
    return select_release(records, repository=REPO, harness_name="codex",
                          harness_version=kw.pop("harness_version", "0.153.4"), **kw)


def assert_error(code, call):
    with pytest.raises(ReleaseValidationError) as caught:
        call()
    assert caught.value.code == code
    assert str(caught.value) == code


def test_highest_compatible_semver_not_input_or_release_id_order():
    records = [release("0.9.0", 300), release("0.10.0", 100),
               release("1.0.0", 200, [interval("0.163.0")])]
    result = select(records)
    assert result["status"] == "candidate"
    assert result["candidate"]["metadata"]["version"] == "0.10.0"


def test_draft_and_prerelease_api_records_need_no_release_metadata():
    records = [dict(id=8, draft=True, prerelease=False),
               dict(id=9, draft=False, prerelease=True),
               release("2.0.0-rc.1", 10), release()]
    assert select(records)["candidate"]["id"] == 1


def test_exact_observed_harnesses_do_not_imply_contiguous_compatibility():
    support = [interval(), interval("0.162.0-alpha.17.2", prerelease=True)]
    records = [release(supported=support)]
    assert select(records)["status"] == "candidate"
    assert select(records, harness_version="0.162.0-alpha.17.2")["status"] == "candidate"
    for host in ("0.154.0", "0.162.0-alpha.17.1", "0.162.0"):
        assert select(records, harness_version=host)["status"] == "no_compatible_release"


def test_harness_prerelease_requires_explicit_permission():
    stable_only = interval("0.153.4", "0.163.0", inclusive=False)
    records = [release(supported=[stable_only])]
    assert select(records, harness_version="0.162.0-alpha.17.2")["status"] == "no_compatible_release"
    stable_only["include_prerelease"] = True
    records = [release(supported=[stable_only])]
    assert select(records, harness_version="0.162.0-alpha.17.2")["status"] == "candidate"
    assert select(records, harness_version="0.163.0")["status"] == "no_compatible_release"


def test_harness_build_metadata_does_not_change_semver_precedence():
    assert select([release()], harness_version="0.153.4+vendor")["status"] == "candidate"


def test_installed_declaration_uses_same_strict_harness_rules():
    declared = compatibility()
    assert supports_harness(declared, harness_name="codex", harness_version="0.153.4")
    assert not supports_harness(declared, harness_name="codex", harness_version="0.153.5")
    assert_error("invalid_harness_version", lambda: supports_harness(
        declared, harness_name="codex", harness_version="unknown"))


@pytest.mark.parametrize("installed", ["0.0.1", "0.0.1+vendor", "0.9.0"])
def test_never_reinstalls_or_downgrades(installed):
    result = select([release()], installed_version=installed)
    assert result["status"] == "up_to_date"
    assert result["candidate"] is None


def test_unknown_or_malformed_harness_never_means_empty_catalog():
    assert_error("unknown_harness", lambda: select_release(
        [], repository=REPO, harness_name="other", harness_version="1.0.0"))
    assert_error("unknown_harness", lambda: select_release(
        [], repository=REPO, harness_name=[], harness_version="1.0.0"))
    for version in ("unknown", "codex 0.153.4", "0.153", "*", None):
        assert_error("invalid_harness_version",
                     lambda: select([], harness_version=version))


def test_fixed_official_repository_and_full_commit_only():
    assert_error("untrusted_repository", lambda: select_release(
        [], repository="someone/fork", harness_name="codex", harness_version="0.153.4"))
    for value, code in [("main", "invalid_source_sha"), ("a" * 7, "invalid_source_sha"),
                        ("A" * 40, "invalid_source_sha")]:
        record = release()
        record["metadata"]["source"]["sha"] = value
        assert_error(code, lambda: select([record]))
    record = release()
    record["metadata"]["source"]["repository"] = "someone/fork"
    assert_error("release_repository_mismatch", lambda: select([record]))


def test_unregistered_or_unproven_interface_rejects_release():
    declaration = compatibility()
    declaration["used_interfaces"].append("native.undocumented")
    assert_error("unregistered_interface",
                 lambda: validate_compatibility(declaration, harness_name="codex"))
    declaration = compatibility([interval("0.153.4", "0.163.0", inclusive=False)])
    declaration["interface_registry"]["hooks.stop"]["supported"] = [interval()]
    assert_error("interface_support_does_not_cover_release",
                 lambda: validate_compatibility(declaration, harness_name="codex"))


def test_interface_prerelease_contract_cannot_be_silently_expanded():
    declaration = compatibility([interval("0.153.4", "0.163.0", prerelease=True)])
    declaration["interface_registry"]["hooks.stop"]["supported"][0]["include_prerelease"] = False
    assert_error("interface_support_does_not_cover_release",
                 lambda: validate_compatibility(declaration, harness_name="codex"))


def test_registry_inclusive_upper_bound_covers_exclusive_not_reverse():
    declaration = compatibility([interval("0.153.4", "0.154.0", inclusive=False)])
    declaration["interface_registry"]["hooks.stop"]["supported"][0]["maximum_inclusive"] = True
    validate_compatibility(declaration, harness_name="codex")
    declaration["supported"][0]["maximum_inclusive"] = True
    declaration["interface_registry"]["hooks.stop"]["supported"][0]["maximum_inclusive"] = False
    assert_error("interface_support_does_not_cover_release",
                 lambda: validate_compatibility(declaration, harness_name="codex"))


@pytest.mark.parametrize("change", [
    {"minimum": "0.153"}, {"minimum": "0.154.0"}, {"maximum_inclusive": 1},
    {"include_prerelease": "true"}, {"maximum_inclusive": False},
    {"minimum": "0.153.4+build"}, {"extra": "ignored"},
])
def test_malformed_interval_cannot_be_treated_as_incompatible(change):
    record = release()
    record["metadata"]["compatibility"]["supported"][0].update(change)
    with pytest.raises(ReleaseValidationError):
        select([record])


def test_bad_published_candidate_does_not_hide_behind_good_older_release():
    damaged = release("9.0.0", 2)
    damaged["metadata"]["compatibility"]["interface_registry"] = {}
    assert_error("invalid_interface_registry", lambda: select([release(), damaged]))


def test_undeclared_old_release_is_counted_but_not_eligible():
    undeclared = release("9.0.0", 2)
    undeclared["metadata"] = None
    result = select([release(), undeclared])
    assert result["candidate"]["id"] == 1
    assert result["skipped_undeclared_releases"] == 1
    assert select([undeclared])["status"] == "no_compatible_release"


def test_duplicate_release_identity_and_build_precedence_are_ambiguous():
    assert_error("duplicate_release_id", lambda: select([release(), release("0.2.0")]))
    assert_error("ambiguous_release_version",
                 lambda: select([release(), release("0.0.1+second", 2)]))


def test_tag_and_manifest_version_must_agree():
    record = release()
    record["tag_name"] = "v8.0.0"
    assert_error("release_tag_version_mismatch", lambda: select([record]))


def test_returned_objects_cannot_mutate_inputs_and_validator_has_no_repair():
    record = release()
    original = deepcopy(record)
    validated = validate_release_metadata(record["metadata"], repository=REPO,
                                          harness_name="codex")
    validated["source"]["sha"] = "b" * 40
    selected = select([record])
    selected["candidate"]["metadata"]["source"]["sha"] = "c" * 40
    assert record == original
