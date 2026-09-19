"""Helpers shared by the tools CLIs.

Only CLI errors, configuration-file loading and path rendering live here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

PACKAGE_ROOT = Path(__file__).resolve().parent

CORPUS_EXTENSIONS = (".md", ".markdown", ".yaml", ".yml", ".json")

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2


class ToolError(Exception):
    """A user-facing error. The CLI prints ``str(exc)`` and exits ``EXIT_USAGE``."""


def _dependency_message(module: str) -> str:
    return (
        f"error: the Python package '{module}' is not installed.\n"
        "  Install mindie-knowledge (which depends on it):\n"
        "    python3 -m pip install -e .\n"
    )


def require_yaml():
    try:
        import yaml  # noqa: F401
    except ImportError:
        raise ToolError(_dependency_message("PyYAML")) from None
    return yaml


def load_document(path: Path) -> Any:
    """Load one YAML or JSON file with the safe loader.

    JSON is a YAML subset, so a single loader is enough. Parse errors are
    reported with the file name; the caller decides whether that is fatal.
    """
    yaml = require_yaml()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"{path}: cannot read file: {exc.strerror}") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ToolError(f"{path}: not valid YAML/JSON: {exc}") from None


def iter_corpus_files(inputs: Sequence[str | os.PathLike[str]]) -> Iterator[Path]:
    """Yield corpus files under the given paths, in a stable order.

    Files are yielded sorted so that reports are deterministic. Dot-directories
    (e.g. ``.venv``) are skipped when walking a directory.
    """
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            files = sorted(
                p
                for p in path.rglob("*")
                if p.is_file()
                and p.suffix in CORPUS_EXTENSIONS
                and not any(part.startswith(".") for part in p.relative_to(path).parts)
            )
            yield from files
        elif path.is_file():
            yield path
        else:
            raise ToolError(f"{path}: no such file or directory")


def json_pointer(path: Iterable[Any]) -> str:
    """Render a path of keys/indices as ``entries[0].scope.torch.range.min``."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return out or "<document>"


def relpath(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def configure_utf8_stdio() -> None:
    """CLI stdout/stderr are UTF-8, including when Windows pipes them."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        stream.flush()
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def run_cli(main) -> None:
    """Run ``main(argv) -> int`` and turn ``ToolError`` into a clean message."""
    configure_utf8_stdio()
    try:
        code = main(sys.argv[1:])
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        code = EXIT_USAGE
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)

