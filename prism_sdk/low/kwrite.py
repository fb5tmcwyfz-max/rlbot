"""
PK_IOCTL_KWRITE_* wrappers - kernel-side auto-writer.

Driver spawnuje system thread ktory pisze zawartosc "slotow" do procesu ofiary
co ~1ms. Zero pollingu z user-mode; Python tylko:
  - kwrite_start(pid, addr, payload)  -> slot_id
  - kwrite_update(slot_id, payload)
  - kwrite_stop(slot_id)

Layout musi zgadzac sie z PrismKernel.h (KR_KWRITE_* structs).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct

_DEVICE_PATH = r"\\.\WdiSvcMon"
_FILE_DEVICE_UNKNOWN = 0x22
_METHOD_BUFFERED     = 0
_FILE_ANY_ACCESS     = 0


def _CTL_CODE(dev, func, method, access):
    return (dev << 16) | (access << 14) | (func << 2) | method


IOCTL_KWRITE_START  = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x806, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
IOCTL_KWRITE_UPDATE = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x807, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
IOCTL_KWRITE_STOP   = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x808, _METHOD_BUFFERED, _FILE_ANY_ACCESS)

KWRITE_MAX_PAYLOAD  = 64
KWRITE_INVALID_SLOT = 0xFFFFFFFF

# Layouts (PrismKernel.h #pragma pack(1)):
#   KR_KWRITE_START_REQ : Q(8) Q(8) I(4) I(4) 64s(64) I(4) I(4) = 96B
#   KR_KWRITE_UPDATE_REQ: I(4) I(4) 64s(64)                     = 72B
#   KR_KWRITE_STOP_REQ  : I(4) I(4)                             = 8B
_START_FMT  = "<QQII64sII"
_UPDATE_FMT = "<II64s"
_STOP_FMT   = "<II"
_START_SIZE  = struct.calcsize(_START_FMT)
_UPDATE_SIZE = struct.calcsize(_UPDATE_FMT)
_STOP_SIZE   = struct.calcsize(_STOP_FMT)
assert _START_SIZE == 96, f"start size {_START_SIZE}"
assert _UPDATE_SIZE == 72, f"update size {_UPDATE_SIZE}"
assert _STOP_SIZE == 8, f"stop size {_STOP_SIZE}"


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, ctypes.c_void_p]
_k32.CreateFileW.restype  = wt.HANDLE
_k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 ctypes.c_void_p, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.DeviceIoControl.restype  = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype  = wt.BOOL

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class KwriteError(RuntimeError):
    pass


class KwriteHandle:
    """RAII: trzymaj otwarty jeden handle do device przez cala sesje sendera.
    Uzywaj przez `with KwriteHandle() as kw: kw.start(...)`."""

    def __init__(self):
        self._h = None

    def open(self):
        GENERIC_READ  = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        h = _k32.CreateFileW(_DEVICE_PATH, GENERIC_READ | GENERIC_WRITE,
                             0, None, OPEN_EXISTING, 0, None)
        if not h or h == _INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise KwriteError(f"CreateFile({_DEVICE_PATH}) err {err} - driver zaladowany? admin?")
        self._h = h
        return self

    def close(self):
        if self._h:
            _k32.CloseHandle(self._h)
            self._h = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()

    # -- Operations --

    def _ioctl(self, code: int, in_buf: bytes, out_size: int) -> bytes:
        if self._h is None:
            raise KwriteError("handle nie otwarty")
        buf = ctypes.create_string_buffer(max(len(in_buf), out_size))
        ctypes.memmove(buf, in_buf, len(in_buf))
        returned = wt.DWORD(0)
        ok = _k32.DeviceIoControl(self._h, code, buf, len(in_buf),
                                  buf, len(buf), ctypes.byref(returned), None)
        if not ok:
            err = ctypes.get_last_error()
            hint = {
                5:    "ACCESS_DENIED (nie admin?)",
                87:   "STATUS_INVALID_PARAMETER - zle args",
                6:    "STATUS_INVALID_HANDLE - slot nieaktywny",
                122:  "STATUS_BUFFER_TOO_SMALL",
            }.get(err, "")
            raise KwriteError(f"IOCTL {hex(code)} err {err} {hint}")
        return bytes(buf.raw[:out_size])

    def start(self, pid: int, addr: int, payload: bytes) -> int:
        """Alokuje slot. Zwraca slot_id. Driver zaczyna pisac natychmiast."""
        if len(payload) == 0 or len(payload) > KWRITE_MAX_PAYLOAD:
            raise ValueError(f"payload size {len(payload)} out of [1, {KWRITE_MAX_PAYLOAD}]")
        pad = payload + b"\x00" * (KWRITE_MAX_PAYLOAD - len(payload))
        req = struct.pack(_START_FMT, pid, addr, len(payload), 0, pad, 0, 0)
        resp = self._ioctl(IOCTL_KWRITE_START, req, _START_SIZE)
        _, _, _, _, _, slot, _ = struct.unpack(_START_FMT, resp)
        if slot == KWRITE_INVALID_SLOT:
            raise KwriteError("driver zwrocil INVALID_SLOT (max slots?)")
        return slot

    def update(self, slot_id: int, payload: bytes) -> None:
        """Atomic update payloadu istniejacego slota."""
        if len(payload) == 0 or len(payload) > KWRITE_MAX_PAYLOAD:
            raise ValueError(f"payload size {len(payload)} out of [1, {KWRITE_MAX_PAYLOAD}]")
        pad = payload + b"\x00" * (KWRITE_MAX_PAYLOAD - len(payload))
        req = struct.pack(_UPDATE_FMT, slot_id, len(payload), pad)
        self._ioctl(IOCTL_KWRITE_UPDATE, req, 0)

    def stop(self, slot_id: int) -> None:
        req = struct.pack(_STOP_FMT, slot_id, 0)
        self._ioctl(IOCTL_KWRITE_STOP, req, 0)
