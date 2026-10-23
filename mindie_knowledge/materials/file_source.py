"""Hash-bound file mappings that never retain a whole task's body in RAM.

[Role] Hash-bound file mappings that never retain a whole task body in memory.
[Invariants] Each access re-checks the regular-file identity, byte envelope and digest; a replaced or pruned source fails visibly.
[Timeouts & Locks] Reads are bounded per file and honor the caller cancellation; no lock is retained between files.
[Side-Effect Policy] Read-only access to caller-selected source files; no temp copies or network calls.

Only descriptor metadata is kept. Each access opens the exact source file,
checks its regular-file identity, byte envelope and digest, then yields one
UTF-8 file. Replacing or pruning the source therefore fails visibly instead
of reading another generation. These objects are private in-process values;
persisted/public descriptors never contain local paths.
"""
from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import stat
from mindie_knowledge.loop.documents import MAX_FILE_BYTES


def _within_root(real_path: str, real_root: str) -> bool:
    try:
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False


def checked_bytes(path, root, *, _real_root: str | None = None):
    path_str, root_str = os.fspath(path), os.fspath(root)
    st = os.lstat(path_str)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError('material source is not a regular file within its bound root')
    real_root = _real_root if _real_root is not None else os.path.realpath(root_str)
    if not _within_root(os.path.realpath(path_str), real_root):
        raise ValueError('material source is not a regular file within its bound root')
    if not stat.S_ISREG(st.st_mode):
        raise ValueError('material source must be a regular file')
    descriptor = os.open(path_str, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError('material source is not a supported regular file')
        # BufferedReader reserves the requested size before observing EOF.
        # Use this file's actual size, not the 100 MiB platform ceiling for
        # every 16 KiB block. The extra byte detects concurrent growth.
        raw = source.read(info.st_size + 1)
        if len(raw) != info.st_size:
            raise ValueError('material source changed size during its read')
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('material source exceeds the per-file platform envelope')
    return raw


class FileText(Mapping):
    __slots__ = ('_path', '_root', '_real_root', '_metadata', '_digest')

    def __init__(self, path, *, root, sha256=None, metadata=None, _real_root: str | None = None):
        self._path, self._root = os.fspath(path), os.fspath(root)
        self._real_root = _real_root if _real_root is not None else os.path.realpath(self._root)
        self._metadata = dict(metadata or {})
        if 'content' in self._metadata:
            raise ValueError('file source metadata cannot retain content')
        if sha256 is None:
            raw = checked_bytes(self._path, self._root, _real_root=self._real_root)
            sha256 = hashlib.sha256(raw).hexdigest()
        self._digest = sha256
        self._metadata.setdefault('sha256', sha256)
        if self._metadata['sha256'] != sha256:
            raise ValueError('file source metadata differs from its bound digest')

    def __getitem__(self, key):
        if key != 'content':
            return self._metadata[key]
        raw = checked_bytes(self._path, self._root, _real_root=self._real_root)
        if hashlib.sha256(raw).hexdigest() != self._digest:
            raise ValueError('material source identity mismatch: bytes differ from the frozen digest')
        return raw.decode('utf-8')

    def __iter__(self):
        yield from self._metadata
        yield 'content'

    def __len__(self):
        return len(self._metadata) + 1

    def with_metadata(self, **metadata):
        value = FileText(self._path, root=self._root, sha256=self._digest,
                         metadata={**self._metadata, **metadata},
                         _real_root=self._real_root)
        return value


class FileContents(Mapping):
    """Relative package path -> text, backed by hash-bound file descriptors."""
    def __init__(self, files):
        self._files = dict(files)

    def __getitem__(self, path):
        return self._files[path]['content']

    def __iter__(self):
        return iter(self._files)

    def __len__(self):
        return len(self._files)

    def __contains__(self, path):
        return path in self._files

    def file(self, path):
        return self._files[path]

    @classmethod
    def from_paths(cls, root, paths):
        root_str = os.fspath(root)
        real_root = os.path.realpath(root_str)
        return cls({
            path: FileText(os.path.join(root_str, path), root=root_str, metadata={'path': path}, _real_root=real_root)
            for path in paths
        })
