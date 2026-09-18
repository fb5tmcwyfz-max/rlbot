"""
Named pipe server dla PrismUI. Sluchamy na \\.\pipe\prism_bot, przyjmujemy
JSON linebased commands, aplikujemy do zywego bota bez restartu.

Format:
    {"cmd":"set","key":"air_scale","val":0.18}
    {"cmd":"set","key":"ground_scale","val":1.0}
    {"cmd":"set","key":"dead_zone","val":0.04}
    {"cmd":"set","key":"hz","val":240}
    {"cmd":"attach","pid":12345}       # zmien target RL process

Wysylane z powrotem (telemetria):
    {"evt":"status","hz":240,"connected":true}

Uzycie:
    from prism_sdk.ipc.pipe_server import PipeServer
    server = PipeServer(on_command=lambda cmd, val: apply(cmd, val))
    server.start()
    ...
    server.stop()
"""
from __future__ import annotations

import json
import struct
import threading
import time
from typing import Callable, Optional

import ctypes
import ctypes.wintypes as wt


PIPE_NAME = r"\\.\pipe\prism_bot"

# Windows API
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PIPE_ACCESS_DUPLEX      = 0x00000003
PIPE_TYPE_BYTE          = 0x00000000
PIPE_READMODE_BYTE      = 0x00000000
PIPE_WAIT               = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
INVALID_HANDLE_VALUE    = ctypes.c_void_p(-1).value

_k32.CreateNamedPipeW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
    wt.DWORD, wt.DWORD, ctypes.c_void_p
]
_k32.CreateNamedPipeW.restype = wt.HANDLE

_k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
_k32.ConnectNamedPipe.restype = wt.BOOL

_k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
_k32.DisconnectNamedPipe.restype = wt.BOOL

_k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                          ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.ReadFile.restype = wt.BOOL

_k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                           ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.WriteFile.restype = wt.BOOL

_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL


class PipeServer:
    """Named pipe server (blocking IO w dedykowanym watku). Wywoluje `on_command`
    dla kazdej otrzymanej komendy z pipe (thread: server, aplikuj thread-safe)."""

    def __init__(self, on_command: Callable[[dict], None], verbose: bool = False):
        self.on_command = on_command
        self.verbose = verbose
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pipe_handle: Optional[int] = None
        self._connected = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="PrismPipeServer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._pipe_handle:
            try:
                _k32.DisconnectNamedPipe(self._pipe_handle)
                _k32.CloseHandle(self._pipe_handle)
            except Exception:
                pass
            self._pipe_handle = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def send_json(self, obj: dict) -> bool:
        """Wyslij JSON linebased do klienta UI. False jesli nie polaczony."""
        if not self._connected or not self._pipe_handle:
            return False
        try:
            data = (json.dumps(obj) + "\n").encode("utf-8")
            written = wt.DWORD(0)
            ok = _k32.WriteFile(self._pipe_handle, data, len(data),
                                ctypes.byref(written), None)
            return bool(ok) and written.value == len(data)
        except Exception:
            return False

    def _loop(self) -> None:
        """Main server loop - accept connections and read commands."""
        while not self._stop.is_set():
            try:
                self._pipe_handle = _k32.CreateNamedPipeW(
                    PIPE_NAME,
                    PIPE_ACCESS_DUPLEX,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                    PIPE_UNLIMITED_INSTANCES,
                    4096, 4096,  # out/in buffer size
                    0,           # default timeout
                    None,        # default security
                )
                if not self._pipe_handle or self._pipe_handle == INVALID_HANDLE_VALUE:
                    err = ctypes.get_last_error()
                    if self.verbose:
                        print(f"[PipeServer] CreateNamedPipe failed: {err}")
                    time.sleep(0.5)
                    continue

                # ConnectNamedPipe blokuje az klient sie polaczy
                if self.verbose:
                    print(f"[PipeServer] czekam na klienta @ {PIPE_NAME}")
                ok = _k32.ConnectNamedPipe(self._pipe_handle, None)
                if not ok and ctypes.get_last_error() != 535:  # 535 = ERROR_PIPE_CONNECTED (juz polaczony)
                    _k32.CloseHandle(self._pipe_handle)
                    self._pipe_handle = None
                    continue

                self._connected = True
                if self.verbose:
                    print(f"[PipeServer] klient polaczony")

                # Read loop - JSON linebased
                buf = bytearray()
                read_buf = (ctypes.c_char * 512)()
                read_count = wt.DWORD(0)

                while not self._stop.is_set():
                    ok = _k32.ReadFile(self._pipe_handle, read_buf, 512,
                                       ctypes.byref(read_count), None)
                    if not ok or read_count.value == 0:
                        break
                    buf.extend(read_buf[:read_count.value])
                    # Parse complete lines
                    while b"\n" in buf:
                        line, _, rest = buf.partition(b"\n")
                        buf = bytearray(rest)
                        line_s = line.decode("utf-8", errors="ignore").strip()
                        if not line_s:
                            continue
                        try:
                            cmd = json.loads(line_s)
                            if self.verbose:
                                print(f"[PipeServer] RX: {cmd}")
                            self.on_command(cmd)
                        except json.JSONDecodeError as e:
                            if self.verbose:
                                print(f"[PipeServer] bad JSON: {line_s!r} - {e}")

                self._connected = False
                if self.verbose:
                    print(f"[PipeServer] klient rozlaczony")
                try:
                    _k32.DisconnectNamedPipe(self._pipe_handle)
                    _k32.CloseHandle(self._pipe_handle)
                except Exception:
                    pass
                self._pipe_handle = None

            except Exception as e:
                if self.verbose:
                    print(f"[PipeServer] loop error: {e}")
                self._connected = False
                if self._pipe_handle:
                    try: _k32.CloseHandle(self._pipe_handle)
                    except Exception: pass
                    self._pipe_handle = None
                time.sleep(0.5)
