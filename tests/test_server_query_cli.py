"""CLI for query/explain over Markdown."""

from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from mindie_knowledge.local.backend import MemoryBackend
from mindie_knowledge.server.capture import capture
from mindie_knowledge.server.capture_cli import main as capture_main
from mindie_knowledge.server.query import main


def _run_cli(*argv: str) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    text = out.getvalue().strip()
    payload = json.loads(text) if text else {}
    return code, payload, err.getvalue()


class QueryCli(unittest.TestCase):
    def test_capture_saves_without_starting_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = support.build_config(candidate=tmp, shared=False, project=False)
            out = io.StringIO()
            with mock.patch("mindie_knowledge.server.capture_cli.load_config", return_value=config), \
                    mock.patch("mindie_knowledge.server.capture.backend_for_config") as backend, \
                    redirect_stdout(out):
                code = capture_main(["--title", "Existing finding", "--content", "Useful observation."])
            payload = json.loads(out.getvalue())
            self.assertEqual(0, code)
            self.assertTrue(pathlib.Path(payload["path"]).is_file())
            self.assertFalse(payload["degraded"])
            backend.assert_not_called()

    def test_text_query_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = support.build_config(candidate=tmp, shared=False, project=False)
            config.retrieval = MemoryBackend()
            capture(title="cli note", content="hostname missing from etc hosts", config=config)
            cfg = pathlib.Path(tmp) / "cfg.json"
            cfg.write_text("{}", encoding="utf-8")
            with mock.patch("mindie_knowledge.server.layers.load_config", return_value=config):
                code, payload, _err = _run_cli("--config", str(cfg), "--text", "hostname missing")
            self.assertEqual(0, code, payload)
            self.assertGreaterEqual(payload.get("count", 0), 1)

    def test_text_required(self) -> None:
        code, _payload, err = _run_cli()
        self.assertEqual(2, code)
        self.assertIn("--text", err)

    def test_help_needs_no_environment_coordinate(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as exc:
            main(["--help"])
        self.assertEqual(0, exc.exception.code)
        self.assertNotIn("--soc", out.getvalue())
        self.assertNotIn("--cann", out.getvalue())

    def test_invalid_limit_does_not_start_the_index(self) -> None:
        with mock.patch("mindie_knowledge.server.layers.load_config") as load:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--text", "context", "--limit", "0"])
            load.assert_not_called()
