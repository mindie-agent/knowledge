"""Hash-bound file mappings that never retain a whole task's body in RAM.

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


def checked_bytes(path, root):
    path, root = Path(path), Path(root)
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('material source is not a regular file within its bound root')
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('material source must be a regular file')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
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
    def __init__(self, path, *, root, sha256=None, metadata=None):
        self._path, self._root = Path(path), Path(root)
        self._metadata = dict(metadata or {})
        if 'content' in self._metadata:
            raise ValueError('file source metadata cannot retain content')
        if sha256 is None:
            raw = checked_bytes(self._path, self._root)
            sha256 = hashlib.sha256(raw).hexdigest()
        self._digest = sha256
        self._metadata.setdefault('sha256', sha256)
        if self._metadata['sha256'] != sha256:
            raise ValueError('file source metadata differs from its bound digest')

    def __getitem__(self, key):
        if key != 'content':
            return self._metadata[key]
        raw = checked_bytes(self._path, self._root)
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
                         metadata={**self._metadata, **metadata})
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

    def file(self, path):
        return self._files[path]

    @classmethod
    def from_paths(cls, root, paths):
        root = Path(root)
        return cls({path: FileText(root / path, root=root, metadata={'path': path}) for path in paths})
