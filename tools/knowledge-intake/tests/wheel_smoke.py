"""Install the wheel alone, outside a checkout; actually run its Markdown CLI."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    wheel = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        environment = root / "environment"
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, timeout=60)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "--no-index", str(wheel)], check=True, timeout=60, cwd=root)
        subprocess.run([str(python), "-I", "-c", "import importlib.util,knowledge_intake; assert importlib.util.find_spec('mindie_knowledge') is None; assert importlib.util.find_spec('docx') is None"], check=True, timeout=10, cwd=root)
        feed = python.parent / ("knowledge-feed.exe" if sys.platform == "win32" else "knowledge-feed")
        help_result = subprocess.run([str(feed), "--help"], capture_output=True, text=True, encoding="utf-8", check=True, timeout=10, cwd=root)
        assert "schedule" in help_result.stdout and "sync" in help_result.stdout
        source = root / "HCCL.md"
        source.write_text("# HCCL reference\n\nSynthetic wheel installation fixture", encoding="utf-8")
        config = root / "intake.json"
        config.write_text(json.dumps({"state_root": str(root / "state"), "output_root": str(root / "output"), "sources": [str(source)]}), encoding="utf-8")
        for converted in (1, 0):
            completed = subprocess.run([str(python), "-I", "-m", "knowledge_intake.cli", str(config)], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30, cwd=root)
            result = json.loads(completed.stdout)
            assert result["status"] == "ok", result
            assert result["sources"][0]["converted"] == converted, result
        notes = list((root / "output").rglob("*.md"))
        assert len(notes) == 1
        assert "Synthetic wheel" in notes[0].read_text(encoding="utf-8")
    print(json.dumps({"status": "passed", "wheel": wheel.name, "no_vaws_dependency": True, "actual_markdown_conversion": True, "unchanged_conversions": 0, "installed_feed_entrypoint": True}))


if __name__ == "__main__":
    main()
