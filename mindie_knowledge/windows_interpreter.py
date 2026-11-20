"""[Role] Validate the executable used at a Windows GUI launch boundary.

[Invariants] A filename is not proof of IMAGE_SUBSYSTEM_WINDOWS_GUI (2); both
the target launcher and any base `home\\pythonw.exe` declared by `pyvenv.cfg`
must carry PE OptionalHeader.Subsystem == 2.
[Timeouts & Locks] Reads only fixed-size PE headers and `pyvenv.cfg`; no processes or locks.
[Side-Effect Policy] Read-only; never repairs or substitutes an interpreter.
"""
import configparser
from pathlib import Path
import struct


def _verify_pe_gui(path: Path) -> None:
    from .loop.service_host import ServiceHostError
    try:
        with Path(path).open("rb") as stream:
            dos = stream.read(64)
            if len(dos) != 64 or dos[:2] != b"MZ":
                raise ValueError()
            offset = struct.unpack_from("<I", dos, 60)[0]
            if offset < 64 or offset > 16 * 1024 * 1024:
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


def require_windowless(python):
    """Reject missing, unreadable, malformed and console-subsystem launchers.

    PE32 and PE32+ both place Subsystem at optional-header offset 68.
    Task Scheduler's Hidden setting hides task metadata, not console windows.
    """
    from .loop.service_host import ServiceHostError
    target = Path(python)
    _verify_pe_gui(target)
    for cfg in (target.parent / "pyvenv.cfg", target.parent.parent / "pyvenv.cfg"):
        try:
            with cfg.open("r", encoding="utf-8-sig", errors="replace") as stream:
                raw = stream.read(65536)
        except FileNotFoundError:
            continue
        except TimeoutError:
            raise
        except OSError:
            raise ServiceHostError("windowless_interpreter_invalid", stage="interpreter") from None
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string("[pyvenv]\n" + raw)
        except configparser.Error:
            raise ServiceHostError("windowless_interpreter_invalid", stage="interpreter") from None
        home_val = parser.get("pyvenv", "home", fallback="").strip()
        if home_val:
            _verify_pe_gui(Path(home_val) / "pythonw.exe")
        break
