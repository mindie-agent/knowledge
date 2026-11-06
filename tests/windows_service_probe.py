"""Transcript adapter probe for an isolated native scheduled-service test."""
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import sys

import win32gui
import win32process


_pids = {os.getpid(), os.getppid()}
_visible = []


def _descendants():
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("image", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    processes = []
    try:
        entry = Entry(size=ctypes.sizeof(Entry))
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            processes.append(dict(pid=entry.pid, parent=entry.parent, image=entry.image))
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)
    owned = set(_pids)
    while True:
        found = {p["pid"] for p in processes if p["parent"] in owned} - owned
        if not found:
            return [p for p in processes if p["pid"] in owned]
        owned.update(found)


def _window(hwnd, _):
    if win32gui.IsWindowVisible(hwnd):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in _pids:
            _visible.append(hwnd)


win32gui.EnumWindows(_window, None)
Path(os.environ["MINDIE_WINDOWLESS_PROBE"]).write_text(json.dumps(dict(
    pid=os.getpid(), parent=os.getppid(), prefix=sys.prefix,
    executable=sys.executable, argv=sys.argv,
    console=int(ctypes.windll.kernel32.GetConsoleWindow()), visible=_visible,
    processes=_descendants(),
)), encoding="utf-8")

_spec = importlib.util.spec_from_file_location(
    "mindie_native_probe_transcript", Path(__file__).with_name("transcript_double.py"))
_adapter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _adapter
_spec.loader.exec_module(_adapter)
FileIdentity = _adapter.FileIdentity
identify = _adapter.identify
read_material = _adapter.read_material
