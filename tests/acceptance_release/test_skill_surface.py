from pathlib import Path

from mindie_knowledge.skill import skill_files

ROOT = Path(__file__).resolve().parents[2] / "mindie_knowledge" / "skills" / "curate-knowledge"


def _text() -> str:
    return "\n".join(
        [
            skill_files()["SKILL.md"].decode("utf-8"),
            skill_files()["references/independent-maintenance.md"].decode("utf-8"),
            skill_files()["agents/openai.yaml"].decode("utf-8"),
        ]
    )


def test_live_mcp_and_cli_are_current_loop():
    text = _text()
    assert "knowledge_query(query, session_id" in text
    assert "knowledge_explain(ref)" in text
    assert "knowledge_use(ref, session_id, application, evidence)" in text
    assert "python -m mindie_knowledge.content_migration" in text
    assert "python -m mindie_knowledge import" in text
    assert "python -m mindie_knowledge publish" in text
    assert "python -m mindie_knowledge attach" in text
    assert "python -m mindie_knowledge withdraw" in text
    assert "Do not call `knowledge_query` in order to attach" in text
    assert "allow_implicit_invocation: false" in text


def test_dead_entries_are_not_live_commands():
    text = _text()
    assert "knowledge_query(text)" not in text
    assert "knowledge_capture(title, content)" not in text
    assert "Grok Bot" not in text
    assert "health --config" not in text
    assert "contribution prepare" not in text
    assert "Do not invent" in text
    assert "There is no live" in text


def test_packaged_skill_files_match_tree():
    for name, content in skill_files().items():
        assert (ROOT / name).read_bytes() == content
