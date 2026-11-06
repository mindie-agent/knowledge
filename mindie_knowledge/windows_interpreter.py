"""[Role] Validate the executable used at a Windows GUI launch boundary.

[Invariants] A filename is not proof of IMAGE_SUBSYSTEM_WINDOWS_GUI (2).
[Timeouts & Locks] Reads only fixed-size PE headers; no processes or locks.
[Side-Effect Policy] Read-only; never repairs or substitutes an interpreter.
"""
from pathlib import Path
import struct


def require_windowless(python):
    """Reject missing, unreadable, malformed and console-subsystem launchers.

    PE32 and PE32+ both place Subsystem at optional-header offset 68.
    Task Scheduler's Hidden setting hides task metadata, not console windows.
    """
    from .loop.service_host import ServiceHostError
    try:
        with Path(python).open("rb") as stream:
            dos = stream.read(64)
            if len(dos) != 64 or dos[:2] != b"MZ":
                raise ValueError()
            offset = struct.unpack_from("<I", dos, 60)[0]
            if offset < 64:
                raise ValueError()
            stream.seek(offset)
            pe = stream.read(24)
            optional = stream.read(70)
            if (len(pe) != 24 or pe[:4] != b"PE\0\0"
                    or struct.unpack_from("<H", pe, 20)[0] < 70
                    or len(optional) != 70
                    or struct.unpack_from("<H", optional)[0] not in {0x10B, 0x20B}
                    or struct.unpack_from("<H", optional, 68)[0] != 2):
                raise ValueError()
    except FileNotFoundError:
        raise ServiceHostError("windowless_interpreter_missing", stage="interpreter") from None
    except TimeoutError:
        raise
    except (OSError, ValueError):
        raise ServiceHostError("windowless_interpreter_invalid", stage="interpreter") from None
