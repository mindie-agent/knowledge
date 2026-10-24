"""Independent updates use captured official Release/tag/source protocol data."""
import copy
import json
from pathlib import Path

import pytest

from mindie_knowledge.component_releases import Catalog, runtime_identity, select

FIXTURE = Path(__file__).with_name("fixtures") / "component-releases"


class CapturedCatalog(Catalog):
    def __init__(self):
        self.reads = []
        self.release = json.loads((FIXTURE / "knowledge-release.json").read_text())

    def read(self, path, *, force=False):
        self.reads.append(path)
        if path.endswith("/releases/latest"):
            return self.release
        if "/git/ref/" in path:
            file = "knowledge-ref.json"
        elif "/git/tags/" in path:
            file = "knowledge-tag.json"
        else:
            file = "knowledge-project.json"
        return json.loads((FIXTURE / file).read_text())

    def asset(self, name, tag, filename):
        return (FIXTURE / ("knowledge-" + filename)).read_bytes()


def test_annotated_release_resolves_to_the_actual_source_package():
    catalog = CapturedCatalog()
    released = catalog.latest("mindie-knowledge")
    assert released == dict(repository="mindie-agent/knowledge", version="0.13.2",
                            revision="b616a38f77ca17573e4ed22ee23c08c09adefac7")
    assert any("/git/tags/" in path for path in catalog.reads)
    current = {"mindie-knowledge": dict(released, version="0.13.1",
                                        revision="2e5cf79050a6634a53b9c2cff52d98ca87850e50")}
    assert select(current, catalog.latest) == {"mindie-knowledge": released}


@pytest.mark.parametrize("version", ["0.13.2", "0.13.3"])
def test_an_older_or_identical_release_never_downgrades_installed_runtime(version):
    catalog = CapturedCatalog()
    released = catalog.latest("mindie-knowledge")
    current = {"mindie-knowledge": dict(released, version=version)}
    assert select(current, catalog.latest) == current


def test_a_draft_cannot_authorize_a_component_install():
    catalog = CapturedCatalog()
    catalog.release["draft"] = True
    with pytest.raises(ValueError, match="not formal"):
        catalog.latest("mindie-knowledge")
    assert len(catalog.reads) == 1


def test_a_published_release_with_missing_package_assets_cannot_replace_a_runtime():
    catalog = CapturedCatalog()
    catalog.release["assets"] = [row for row in catalog.release["assets"] if not row["name"].endswith(".whl")]
    with pytest.raises(ValueError, match="artifacts are incomplete"):
        catalog.latest("mindie-knowledge")
    assert len(catalog.reads) == 1


def test_a_new_plugin_baseline_cannot_downgrade_an_independently_updated_component():
    released = CapturedCatalog().latest("mindie-knowledge")
    current = {"mindie-knowledge": released}
    older = {"mindie-knowledge": dict(released, version="0.13.1",
                                      revision="2e5cf79050a6634a53b9c2cff52d98ca87850e50")}
    assert select(current, lambda _: older["mindie-knowledge"], baseline=older) == current


def test_component_runtime_receipt_keeps_source_identity_and_selected_vcs_identity_distinct():
    released = CapturedCatalog().latest("mindie-knowledge")
    baseline = dict(source_sha256="a" * 64, runtime={"mindie-knowledge": "b" * 40},
                    native_version="0.1.2", requirements_sha256="c" * 64)
    components = dict(schema="mindie-component-runtime/1", adapter_source_sha256="a" * 64,
                      lock_sha256="d" * 64, runtime={"mindie-knowledge": released})
    original = copy.deepcopy(baseline)
    updated = runtime_identity(baseline, components)
    assert updated["native_version"] == "0.1.2"
    assert updated["runtime"]["mindie-knowledge"] == released["revision"]
    assert updated["requirements_sha256"] == baseline["requirements_sha256"]
    assert updated["component_updates"] == components
    assert baseline == original
    with pytest.raises(ValueError, match="different adapter"):
        runtime_identity(dict(baseline, source_sha256="e" * 64), components)
