"""Damage actual SQLite paths without pretending Windows permits open-file deletion."""
import os

import pytest


def damage_database(db, path, *, read, replacement=None):
    def damage():
        if replacement is None:
            path.unlink()
        else:
            os.replace(replacement, path)

    closed = os.name == 'nt'
    if closed:
        before = read()
        with pytest.raises(PermissionError) as caught:
            damage()
        assert caught.value.winerror in {5, 32}
        assert read() == before  # Native SQLite sharing keeps this owner valid.
        # Release only the native SQLite handle, retaining the exact guarded
        # owner and recorded identity. The next check must report path damage,
        # not merely "Cannot operate on a closed database".
        db.close()
    damage()
    if replacement is None:
        with pytest.raises(FileNotFoundError):
            db.assert_authority()
    else:
        with pytest.raises(ValueError, match='identity changed'):
            db.assert_authority()
    return closed
