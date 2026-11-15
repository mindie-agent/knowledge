"""Small real material-package helpers for mechanism tests, without models."""
import atexit
import copy
import json
from pathlib import Path
import tempfile

from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.loop.documents import FIELDS

_PACKAGE_TMP = tempfile.TemporaryDirectory(prefix="mindie-test-package-")
atexit.register(_PACKAGE_TMP.cleanup)
_PACKAGE_CACHE: dict[str, tuple[dict, dict]] = {}
_STORE_SEQ = 0


def package_for(doc):
    """Bind a manual test document to its actual Markdown package identity."""
    global _STORE_SEQ
    payload = {key: doc[key] for key in FIELDS}
    cache_key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    hit = _PACKAGE_CACHE.get(cache_key)
    if hit is not None:
        bound, package = hit
        doc.update(copy.deepcopy(bound))
        return copy.deepcopy(package)
    _STORE_SEQ += 1
    store = MaterialStore(Path(_PACKAGE_TMP.name) / str(_STORE_SEQ), doc["domain"])
    try:
        bound = store.put_document(payload)
        package = store.export_task(doc["entry_id"], revision=bound["revision"])
    finally:
        store.close()
    _PACKAGE_CACHE[cache_key] = (copy.deepcopy(bound), copy.deepcopy(package))
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
