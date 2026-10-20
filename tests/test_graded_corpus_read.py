"""Local corpus reads preserve graded retrieval across checkout line endings."""
import pytest

from graded_fixture import OBSERVED, unit
from mindie_knowledge.graded.index import RoutingIndex


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_local_corpus_retrieves_child_and_inherited_rules(tmp_path, newline):
    parent = unit("unit-parent")
    child = unit("unit-child", parents=[parent.id])
    for value in (parent, child):
        path = tmp_path / value.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.render().replace("\n", newline).encode("utf-8"))

    index = RoutingIndex.from_root(tmp_path)
    result = index.query("graph", conditions=OBSERVED)
    assert child.id in {row["id"] for row in result["results"]}
    assert index.closure[child.id] == [parent.id]
    assert index.units[child.id].body == child.body
