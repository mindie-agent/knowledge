"""Small real material-package helpers for mechanism tests, without models."""
from contextlib import closing
from pathlib import Path
import tempfile

from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.loop.documents import FIELDS


def package_for(doc):
    """Bind a manual test document to its actual Markdown package identity."""
    with tempfile.TemporaryDirectory(prefix="mindie-test-package-") as directory, \
            closing(MaterialStore(Path(directory), doc["domain"])) as store:
        bound = store.put_document({key: doc[key] for key in FIELDS})
        package = store.export_task(doc["entry_id"], revision=bound["revision"])
    doc.update(bound)
    return package


def install_documents(store, docs, *, feed_ident, source_revision=None):
    return store.install_feed((package_for(doc) for doc in docs), feed_ident=feed_ident,
                              source_revision=source_revision)


def write_package(repo, package):
    root = Path(repo) / "tasks" / package["task_id"]
    for relative, text in package["files"].items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
