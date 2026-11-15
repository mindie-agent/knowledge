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
from mindie_knowledge.filesystem import _windows_extended, _windows_logical
from mindie_knowledge.loop.documents import MAX_FILE_BYTES


def _io_path(path) -> str:
    raw = os.fspath(path)
    if os.name == 'nt':
        return _windows_extended(os.path.abspath(_windows_logical(raw)))
    return raw


def _within_root(real_path: str, real_root: str) -> bool:
    if os.name == 'nt':
        real_path = _windows_logical(real_path)
        real_root = _windows_logical(real_root)
    try:
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False


def _posix_rel_within_verified_root(
    path_str: str, root_str: str, root_id: tuple[int, int] | None
) -> bool:
    if root_id is None or os.name == 'nt':
        return False
    prefix = root_str.rstrip('/') + '/'
    if not path_str.startswith(prefix):
        return False
    rel = path_str[len(prefix):]
    if not rel:
        return False
    parts = rel.split('/')
    if any(p in ('', '.', '..') for p in parts):
        return False
    try:
        cur_root = os.stat(root_str)
        if (cur_root.st_dev, cur_root.st_ino) != root_id:
            return False
        cur = root_str.rstrip('/')
        for part in parts[:-1]:
            cur = cur + '/' + part
            if stat.S_ISLNK(os.lstat(cur).st_mode):
                return False
    except OSError:
        return False
    return True


def checked_bytes(
    path, root, *, _real_root: str | None = None, _root_id: tuple[int, int] | None = None
):
    path_str, root_str = _io_path(path), _io_path(root)
    st = os.lstat(path_str)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError('material source is not a regular file within its bound root')
    if not _posix_rel_within_verified_root(path_str, root_str, _root_id):
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
    __slots__ = ('_path', '_root', '_real_root', '_root_id', '_metadata', '_digest')

    def __init__(
        self,
        path,
        *,
        root,
        sha256=None,
        metadata=None,
        _real_root: str | None = None,
        _root_id: tuple[int, int] | None = None,
    ):
        self._path, self._root = _io_path(path), _io_path(root)
        self._real_root = _real_root if _real_root is not None else os.path.realpath(self._root)
        if _root_id is not None:
            self._root_id = _root_id
        else:
            try:
                rst = os.stat(self._real_root)
                self._root_id = (rst.st_dev, rst.st_ino)
            except OSError:
                self._root_id = None
        self._metadata = dict(metadata or {})
        if 'content' in self._metadata:
            raise ValueError('file source metadata cannot retain content')
        if sha256 is None:
            raw = checked_bytes(
                self._path, self._root, _real_root=self._real_root, _root_id=self._root_id
            )
            sha256 = hashlib.sha256(raw).hexdigest()
        self._digest = sha256
        self._metadata.setdefault('sha256', sha256)
        if self._metadata['sha256'] != sha256:
            raise ValueError('file source metadata differs from its bound digest')

    def read_verified_bytes(self) -> bytes:
        raw = checked_bytes(
            self._path, self._root, _real_root=self._real_root, _root_id=self._root_id
        )
        if hashlib.sha256(raw).hexdigest() != self._digest:
            raise ValueError('material source identity mismatch: bytes differ from the frozen digest')
        return raw

    def __getitem__(self, key):
        if key != 'content':
            return self._metadata[key]
        return self.read_verified_bytes().decode('utf-8')

    def __iter__(self):
        yield from self._metadata
        yield 'content'

    def __len__(self):
        return len(self._metadata) + 1

    def with_metadata(self, **metadata):
        value = FileText(self._path, root=self._root, sha256=self._digest,
                         metadata={**self._metadata, **metadata},
                         _real_root=self._real_root, _root_id=self._root_id)
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
        root_str = _io_path(root)
        real_root = os.path.realpath(root_str)
        return cls({
            path: FileText(
                os.path.join(root_str, path.replace('/', os.sep) if os.sep != '/' else path),
                root=root_str,
                metadata={'path': path},
                _real_root=real_root,
            )
            for path in paths
        })
