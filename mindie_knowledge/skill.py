"""Read or install the knowledge package's optional curation skill."""

from __future__ import annotations

import argparse
from importlib.resources import files
from pathlib import Path
import sys

SKILL_NAME = "curate-knowledge"
RESOURCE_FILES = ("SKILL.md", "agents/openai.yaml", "references/independent-maintenance.md")


def skill_files() -> dict[str, bytes]:
    root = files("mindie_knowledge").joinpath("skills", SKILL_NAME)
    return {name: root.joinpath(name).read_bytes() for name in RESOURCE_FILES}


def install_skill(directory: Path, *, force: bool = False) -> Path:
    """Copy only package-owned files into an explicitly chosen skill root."""
    target = directory / SKILL_NAME
    contents = skill_files()
    # Check every destination before writing any file.
    for name, content in contents.items():
        path = target / name
        if path.exists() and path.read_bytes() != content and not force:
            raise FileExistsError(f"{path} differs; use --force to replace the installed skill")
    for name, content in contents.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", type=Path, help="optional native client skill directory")
    parser.add_argument("--force", action="store_true", help="replace differing installed skill files")
    args = parser.parse_args(argv)
    if args.force and args.install_dir is None:
        parser.error("--force requires --install-dir")
    if args.install_dir is None:
        sys.stdout.write(skill_files()["SKILL.md"].decode("utf-8"))
        return 0
    try:
        target = install_skill(args.install_dir.expanduser(), force=args.force)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(target)
    return 0
