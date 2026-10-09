"""Development formats may restart; released state may never be reset.

RELEASE_VERSION is set to the normal package version in the release commit.
Internal package versions and Git pins alone do not declare a release.
FORMAT changes only when the persistent representation changes. Released
format changes require an explicit migration before selecting a new runtime.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import stat

FORMAT = 1
RELEASE_VERSION = None
DATABASE = 'state-v4.sqlite3'


def _version(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value)):
        raise ValueError('state release version must be a normal version or null during development')
    return value


def _read(base):
    from .owned_state import _present
    path = base / 'state-layout.json'
    if not _present(path):
        return None
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('knowledge state layout must be a regular file')
    with path.open('rb') as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError('knowledge state layout exceeds its metadata bound')
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'schema', 'format', 'release_version'}
            or value['schema'] != 'mindie-knowledge-layout/1'
            or type(value['format']) is not int or value['format'] < 1):
        raise ValueError('knowledge state layout is invalid; existing state was preserved')
    _version(value['release_version'])
    return value


def state_root(root, domain):
    """Read-only compatibility check, shared by calls, status and updater."""
    from .loop.documents import DOMAIN_RE
    if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
        raise ValueError('invalid domain')
    base = Path(root).absolute() / domain
    previous = _read(base)
    _version(RELEASE_VERSION)
    if previous is None and any(path.is_dir() and re.fullmatch(r'state-v\d+', path.name)
                                for path in base.glob('state-v[0-9]*')):
        raise ValueError('knowledge state layout is missing; existing state was preserved')
    if previous is not None:
        prior_root = base / ('state-v' + str(previous['format']))
        if not (prior_root / DATABASE).is_file():
            raise ValueError('initialized knowledge state is missing; existing layout was preserved')
        if previous['format'] != FORMAT and previous['release_version'] is not None:
            raise ValueError('released knowledge state requires an explicit format migration; existing state was preserved')
    return (base / ('state-v' + str(FORMAT))).resolve()


@contextmanager
def prepare_layout(root, domain):
    """Publish the selected layout only after its database opens successfully."""
    from .loop.locks import StartLock
    from .markdown import _atomic_write_text
    # Validate the configured path before mkdir/resolve can follow a dangling
    # link and turn a broken existing root into a new empty target.
    base = Path(root).absolute() / domain
    _read(base)
    base.mkdir(parents=True, exist_ok=True)
    lock = StartLock(base / 'state-layout.lock')
    lock.acquire(wait=None)
    try:
        selected = state_root(root, domain)
        previous = _read(base)
        yield selected
        release = RELEASE_VERSION
        if previous is not None and previous['format'] == FORMAT:
            release = release or previous['release_version']
        value = dict(schema='mindie-knowledge-layout/1', format=FORMAT, release_version=release)
        if value != previous:
            _atomic_write_text(base / 'state-layout.json', json.dumps(value, sort_keys=True) + '\n')
    finally:
        lock.release()
