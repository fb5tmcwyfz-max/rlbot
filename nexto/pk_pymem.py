"""
pk_pymem - drop-in replacement for `pymem` using our kernel-mode
driver PrismKernel (\\\\.\\WdiSvcMon).

The bot does NOT talk to the game. The bot talks to the driver, the driver
reads game memory through MmCopyVirtualMemory. No user-mode WinAPI touches
the game process.

Provides only what bot_nexto.py (and derivatives) use:
  - pymem.Pymem(name_or_pid)
      .read_bytes(addr, n) -> bytes
      .write_bytes(addr, data[, length])
      .process_id
      .process_handle   (dummy int - some scripts read the attribute)
  - pymem.process.module_from_name(handle, name).lpBaseOfDll -> int

Requires: PrismKernel driver loaded.
"""

import ctypes
import ctypes.wintypes as wt
import struct
import types

# ---------- WinAPI ----------

GENERIC_READ  = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_k32   = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi",    use_last_error=True)

_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, ctypes.c_void_p]
_k32.CreateFileW.restype  = wt.HANDLE

_k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 ctypes.c_void_p, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.DeviceIoControl.restype  = wt.BOOL

_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype  = wt.BOOL

_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype  = wt.HANDLE

_k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                            ctypes.POINTER(wt.DWORD)]
_k32.QueryFullProcessImageNameW.restype  = wt.BOOL

_psapi.EnumProcesses.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD,
                                 ctypes.POINTER(wt.DWORD)]
_psapi.EnumProcesses.restype  = wt.BOOL


# ---------- IOCTL (identical to driver/PrismKernel.h) ----------

def _CTL_CODE(dev, func, method, access):
    return (dev << 16) | (access << 14) | (func << 2) | method

_FILE_DEVICE_UNKNOWN = 0x22
_METHOD_BUFFERED     = 0
_FILE_ANY_ACCESS     = 0

PK_IOCTL_READ  = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x800, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
PK_IOCTL_WRITE = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x801, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
PK_IOCTL_BASE  = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x802, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
PK_IOCTL_FIND_PID = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x814, _METHOD_BUFFERED, _FILE_ANY_ACCESS)

PK_IOCTL_AUTOWRITE_START = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x810, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
PK_IOCTL_AUTOWRITE_SET   = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x811, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
PK_IOCTL_AUTOWRITE_STOP  = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x812, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
PK_IOCTL_HOOK_START      = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x820, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
PK_IOCTL_HOOK_STOP       = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x821, _METHOD_BUFFERED, _FILE_ANY_ACCESS)

DEVICE_PATH = r"\\.\WdiSvcMon"

_HOOK_REQ_FMT = "<IIQ"  # pid (4B) + pad (4B) + car_addr (8B) = 16B
_HOOK_REQ_SIZE = struct.calcsize(_HOOK_REQ_FMT)

_RW_REQ_FMT   = "<QII"
_RW_REQ_SIZE  = struct.calcsize(_RW_REQ_FMT)
_BASE_REQ_FMT = "<IIQ"
_BASE_REQ_SIZE= struct.calcsize(_BASE_REQ_FMT)

# KR_AUTOWRITE_START_REQ (see PrismKernel.h):
#   uint64 TargetAddress
#   uint32 Pid
#   uint32 Size
#   uint32 IntervalUs
#   uint32 _pad
# = 24 B, then Size bytes of initial data
_AW_START_FMT = "<QIIII"
_AW_START_SIZE = struct.calcsize(_AW_START_FMT)

KR_AUTOWRITE_MAX_SIZE = 128


# ---------- helpers ----------

_FIND_PID_FMT = "<128sII"
_FIND_PID_SIZE = struct.calcsize(_FIND_PID_FMT)
assert _FIND_PID_SIZE == 136


def _find_pid_by_name(name):
    """Kernel-side process lookup via IOCTL 0x814. No user-mode handle to RL."""
    target = name if name.lower().endswith(".exe") else (name + ".exe")

    h = _k32.CreateFileW(DEVICE_PATH,
                         GENERIC_READ | GENERIC_WRITE,
                         0, None, OPEN_EXISTING, 0, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise RuntimeError(
            "CreateFile('%s') failed err=%d. Is the driver loaded? "
            "(run build_install.ps1 as Administrator)"
            % (DEVICE_PATH, ctypes.get_last_error()))
    try:
        wide = (target + "\x00").encode("utf-16-le")
        wide = wide.ljust(128, b"\x00")[:128]
        req = struct.pack(_FIND_PID_FMT, wide, 0, 0)
        buf = ctypes.create_string_buffer(req, _FIND_PID_SIZE)
        returned = wt.DWORD(0)
        ok = _k32.DeviceIoControl(h, PK_IOCTL_FIND_PID,
                                  buf, _FIND_PID_SIZE,
                                  buf, _FIND_PID_SIZE,
                                  ctypes.byref(returned), None)
        if not ok:
            raise RuntimeError("IOCTL_FIND_PID err=%d" % ctypes.get_last_error())
        _, pid, _ = struct.unpack(_FIND_PID_FMT, buf.raw[:_FIND_PID_SIZE])
        return pid if pid != 0 else None
    finally:
        _k32.CloseHandle(h)


# ---------- Pymem ----------

class Pymem:
    """API-compatible fragment of pymem.Pymem, but I/O goes through the kernel driver."""

    def __init__(self, name_or_pid):
        if isinstance(name_or_pid, int):
            pid = name_or_pid
        else:
            pid = _find_pid_by_name(str(name_or_pid))
            if pid is None:
                raise RuntimeError(
                    "Process '%s' not found. Pass a PID (int) or a full .exe name."
                    % name_or_pid
                )

        self.process_id     = pid
        self.process_handle = 0xDEADBEEF  # dummy; module_from_name ignores it

        self._dev = _k32.CreateFileW(DEVICE_PATH,
                                     GENERIC_READ | GENERIC_WRITE,
                                     0, None, OPEN_EXISTING, 0, None)
        if not self._dev or self._dev == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise RuntimeError(
                "CreateFile('%s') failed err=%d. "
                "Is the PrismKernel driver loaded (release\\install.bat)?"
                % (DEVICE_PATH, err)
            )

        # remember for module_from_name (see `process` below)
        _register_active(self)

    def close(self):
        if getattr(self, "_dev", None) and self._dev != INVALID_HANDLE_VALUE:
            _k32.CloseHandle(self._dev)
            self._dev = None

    def __del__(self):
        try: self.close()
        except Exception: pass

    def _ioctl(self, code, in_buf, out_len):
        in_size  = len(in_buf)
        buf_size = max(in_size, out_len, 1)
        buf = ctypes.create_string_buffer(in_buf, buf_size)
        returned = wt.DWORD(0)
        ok = _k32.DeviceIoControl(self._dev, code, buf, in_size, buf, out_len,
                                  ctypes.byref(returned), None)
        if not ok:
            err = ctypes.get_last_error()
            raise OSError("DeviceIoControl(0x%08X) failed err=%d" % (code, err))
        return bytes(buf.raw[:returned.value])

    # --- pymem-compatible API ---

    def read_bytes(self, address, length):
        if length <= 0:
            return b""
        req = struct.pack(_RW_REQ_FMT, int(address) & 0xFFFFFFFFFFFFFFFF,
                          self.process_id, int(length))
        return self._ioctl(PK_IOCTL_READ, req, int(length))

    def write_bytes(self, address, data, length=None):
        if length is None:
            length = len(data)
        payload = bytes(data[:length])
        req = struct.pack(_RW_REQ_FMT, int(address) & 0xFFFFFFFFFFFFFFFF,
                          self.process_id, len(payload)) + payload
        self._ioctl(PK_IOCTL_WRITE, req, 0)

    def get_image_base(self):
        req = struct.pack(_BASE_REQ_FMT, self.process_id, 0, 0)
        out = self._ioctl(PK_IOCTL_BASE, req, _BASE_REQ_SIZE)
        _pid, _pad, base = struct.unpack(_BASE_REQ_FMT, out)
        return base

    # --- autowrite loop: kernel-side ticker that writes a buffer to the game every N us ---

    def autowrite_start(self, target_addr, initial_bytes, interval_us):
        """Start a kernel-side loop that copies `initial_bytes` (and later
        whatever autowrite_set provides) to `target_addr` in this Pymem's
        process every `interval_us` microseconds."""
        size = len(initial_bytes)
        if size == 0 or size > KR_AUTOWRITE_MAX_SIZE:
            raise ValueError("initial_bytes must be 1..%d B (got %d)"
                              % (KR_AUTOWRITE_MAX_SIZE, size))
        if interval_us < 100 or interval_us > 1_000_000:
            raise ValueError("interval_us must be 100..1_000_000 (got %d)" % interval_us)
        req = struct.pack(_AW_START_FMT,
                          int(target_addr) & 0xFFFFFFFFFFFFFFFF,
                          self.process_id, size, interval_us, 0) + bytes(initial_bytes)
        self._ioctl(PK_IOCTL_AUTOWRITE_START, req, 0)

    def autowrite_set(self, new_bytes):
        """Replace the buffer used by the kernel-side loop. Very light - one
        memcpy under a spinlock. The next worker tick sees the new values."""
        if not new_bytes:
            raise ValueError("autowrite_set: empty buffer")
        self._ioctl(PK_IOCTL_AUTOWRITE_SET, bytes(new_bytes), 0)

    def autowrite_stop(self):
        """Stop the loop. Safe to call repeatedly / without START."""
        try:
            self._ioctl(PK_IOCTL_AUTOWRITE_STOP, b"\x00", 0)
        except OSError:
            pass  # STATUS_DEVICE_NOT_READY = nothing was running, OK

    def hook_start(self, car_addr):
        """Start the kernel hook - a thread reads from shared memory and
        writes to FVehicleInputs via MmCopyVirtualMemory.

        car_addr: car address in game memory (car_addr from reflection)"""
        req = struct.pack(_HOOK_REQ_FMT, self.process_id, int(car_addr) & 0xFFFFFFFFFFFFFFFF)
        self._ioctl(PK_IOCTL_HOOK_START, req, 0)

    def hook_stop(self):
        """Stop the kernel hook."""
        try:
            self._ioctl(PK_IOCTL_HOOK_STOP, b"\x00", 0)
        except OSError:
            pass


# ---------- pymem.process.module_from_name ----------

# bot_nexto uses: `pymem.process.module_from_name(pm.process_handle, PROCESS_NAME).lpBaseOfDll`
# Our driver only knows the PID, not a handle. We keep the last created Pymem
# instance globally - bot_nexto creates one and immediately asks for the module.

_ACTIVE_PMS = []  # last = [-1]

def _register_active(pm):
    _ACTIVE_PMS.append(pm)
    if len(_ACTIVE_PMS) > 8:
        del _ACTIVE_PMS[:len(_ACTIVE_PMS) - 8]


class _ModuleInfo:
    """Equivalent of MODULEINFO from pymem.process.module_from_name."""
    def __init__(self, base):
        self.lpBaseOfDll  = base
        self.SizeOfImage  = 0
        self.EntryPoint   = 0
        self.name         = None
        self.filename     = None


def _module_from_name(handle, name):
    if not _ACTIVE_PMS:
        raise RuntimeError(
            "pk_pymem.process.module_from_name: create a Pymem() first."
        )
    pm = _ACTIVE_PMS[-1]
    base = pm.get_image_base()
    m = _ModuleInfo(base)
    m.name = name
    return m


# Assembled module - so `sys.modules['pymem.process'] = pk_pymem.process` works
process = types.ModuleType("pymem.process")
process.module_from_name = _module_from_name


# ---------- self-test ----------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python pk_pymem.py <name_or_pid>")
        sys.exit(2)
    arg = sys.argv[1]
    try:
        pid = int(arg)
    except ValueError:
        pid = arg
    pm = Pymem(pid)
    print("PID           = %d" % pm.process_id)
    print("ImageBase     = 0x%016X" % pm.get_image_base())
    print("read 16 B     =", pm.read_bytes(pm.get_image_base(), 16).hex(" "))
    pm.close()
