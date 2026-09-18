"""Bundled Markdown remains readable from a wheel outside the checkout."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from mindie_knowledge import corpus
from mindie_knowledge.markdown import parse_markdown

REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_wheel(out_dir: Path) -> Path:
    if shutil.which("uv"):
        subprocess.run(
            ["uv", "build", "--out-dir", str(out_dir)],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "-w",
                str(out_dir),
                str(REPO_ROOT),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    wheels = sorted(out_dir.glob("mindie_knowledge-*.whl"))
    if not wheels:
        raise AssertionError(f"no wheel produced in {out_dir}")
    return wheels[-1]


class CheckoutCorpus(unittest.TestCase):
    def test_all_reference_notes_have_readable_content(self):
        files = list(corpus.iter_entry_files())
        self.assertGreaterEqual(len(files), 65)
        self.assertEqual(corpus.corpus_root().resolve(), (REPO_ROOT / "corpus").resolve())
        for path in files:
            title, body = parse_markdown(path.read_text(encoding="utf-8"))
            self.assertTrue(title, path)
            self.assertTrue(body, path)
            self.assertEqual(path.suffix, ".md")
        self.assertFalse(list(corpus.corpus_root().rglob("*.yaml")))

    def test_measurement_conditions_and_exact_values_survive(self):
        note = (corpus.corpus_root() / "references" /
                "ascend910b4-single-card-dense-matmul-sustained-2026-06-03.md").read_text(encoding="utf-8")
        for value in ("232.332682", "231.833233", "319.337134", "0.95", "0.65",
                      "8192x8192x8192", "torch_npu.npu_quant_matmul", "2.10.0",
                      "The manifest itself is not public", "were not recorded"):
            self.assertIn(value, note)


class WheelShipsCorpus(unittest.TestCase):
    def test_installed_wheel_exposes_identical_markdown(self):
        checkout = {p.relative_to(corpus.corpus_root()).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in corpus.iter_entry_files()}
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            wheel = _build_wheel(tmp / "dist")
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                for retired in ("bot/", "sync/", "conformance/", "schemas/",
                                "canonical.py", "export.py", "validate.py"):
                    self.assertFalse(any(name.startswith("mindie_knowledge/" + retired) for name in names))
            venv = tmp / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", "--no-index", str(wheel)], check=True)
            script = """
import hashlib, json, sys
from mindie_knowledge import corpus
assert "mindie_diagnostics" not in sys.modules
root = corpus.corpus_root()
print(json.dumps({
    "packaged": "site-packages" in root.as_posix() and "/data/corpus" in root.as_posix(),
    "documents": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in corpus.iter_entry_files()},
}))
"""
            result = subprocess.run([str(python), "-I", "-c", script], cwd=tmp,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["packaged"])
            self.assertEqual(payload["documents"], checkout)


if __name__ == "__main__":
    unittest.main()
