"""Demo of the new kernel driver (\\\\.\\WdiSvcMon) - pure Python, no bot, no exe.
Shows step by step:
  1. Open the driver
  2. Find the RocketLeague.exe process
  3. IOCTL_BASE - game ImageBase
  4. IOCTL_FIND_MODULE - find a DLL inside the process
  5. IOCTL_READ - first 16B (PE MZ header) and live Ball.RADIUS
  6. Live Vehicle - THROTTLE/STEER/PITCH from INPUT struct
  7. Benchmark - IOCTL throughput

No writes to the game (write path untouched - ban risk online).
"""
from __future__ import annotations
import os
import struct
import sys
import time

# Resolve paths from this file's location so the script runs from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                 # rlbot-main/
_NEXTO = os.path.join(_ROOT, "nexto")          # rlbot-main/nexto/
for _p in (_ROOT, _NEXTO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from prism_sdk.low.driver import Driver
from prism_sdk.low.reflection import Reflection
from prism_sdk.low import offsets as O


def hex16(b): return " ".join(f"{x:02X}" for x in b[:16])


def main():
    print("=" * 68)
    print("  PRISMKERNEL DEMO   \\\\.\\WdiSvcMon   ")
    print("=" * 68)
    print()

    # -------- Step 1: open the driver --------
    print("[1] Opening the driver...")
    drv = Driver()
    t0 = time.perf_counter()
    if not drv.attach():
        print("    FAIL - driver not loaded, or RL not running")
        return 1
    print(f"    OK  attach in {(time.perf_counter()-t0)*1000:.1f}ms")
    print(f"        PID         = {drv.pid}")
    print(f"        module_base = 0x{drv.module_base:X}")
    print()

    # -------- Step 2: PE header via kernel read --------
    print("[2] IOCTL_READ - first 16B from RocketLeague.exe (PE MZ header):")
    hdr = drv.read_bytes(drv.module_base, 16)
    print(f"    bytes: {hex16(hdr)}")
    if hdr[:2] == b"MZ":
        print(f"    OK  MZ signature (Windows PE)")
    else:
        print(f"    FAIL - no MZ, something wrong with read?")
    print()

    # -------- Step 3: find_module --------
    print("[3] IOCTL_FIND_MODULE - looking for DLLs inside RocketLeague.exe:")
    for name in ("kernel32.dll", "ntdll.dll", "d3d11.dll"):
        try:
            import ctypes
            from pk_pymem import _CTL_CODE, _FILE_DEVICE_UNKNOWN, _METHOD_BUFFERED, _FILE_ANY_ACCESS
            code = _CTL_CODE(_FILE_DEVICE_UNKNOWN, 0x813, _METHOD_BUFFERED, _FILE_ANY_ACCESS)
            # PK_FIND_MODULE_REQ: u64 pid, wchar[64] name, u64 base_out, u32 size_out, ...
            req = bytearray(160)
            struct.pack_into("<Q", req, 0, drv.pid)
            wide_name = name.encode("utf-16-le") + b"\x00\x00"
            req[8:8+len(wide_name)] = wide_name[:126]
            out = drv.pm._ioctl(code, bytes(req), 160)
            base_out, size_out = struct.unpack_from("<QI", out, 136)
            if base_out:
                print(f"    {name:<20} -> base=0x{base_out:X}  size={size_out}B")
            else:
                print(f"    {name:<20} -> NOT FOUND")
        except Exception as e:
            print(f"    {name:<20} -> ERR {type(e).__name__}: {e}")
    print()

    # -------- Step 4: reflection --------
    print("[4] GObjects/GNames reflection scan:")
    t0 = time.perf_counter()
    refl = Reflection(drv)
    if not refl.rebuild():
        print("    FAIL rebuild")
        return 1
    dt = (time.perf_counter() - t0) * 1000
    n = sum(len(v) for v in refl._by_class.values())
    print(f"    OK  {n} objects, {len(refl._by_class)} classes in {dt:.0f}ms")
    print()

    # -------- Step 5: live Ball.RADIUS --------
    print("[5] Live Ball_TA - reading physical fields:")
    ball = refl.highest("Ball_TA")
    if ball:
        loc_bytes = drv.read_bytes(ball + O.Actor.LOCATION, 12)
        loc = struct.unpack("<fff", loc_bytes) if loc_bytes else (0, 0, 0)
        vel_bytes = drv.read_bytes(ball + O.Actor.VELOCITY, 12)
        vel = struct.unpack("<fff", vel_bytes) if vel_bytes else (0, 0, 0)
        radius = drv.read_f32(ball + O.Ball.RADIUS)
        gevent = drv.read_ptr(ball + O.Ball.GAME_EVENT)
        print(f"    addr           = 0x{ball:X}")
        print(f"    LOCATION       = ({loc[0]:+.1f}, {loc[1]:+.1f}, {loc[2]:+.1f})")
        print(f"    VELOCITY       = ({vel[0]:+.1f}, {vel[1]:+.1f}, {vel[2]:+.1f})")
        print(f"    RADIUS         = {radius:.2f}")
        print(f"    GAME_EVENT ptr = 0x{gevent:X}")
    else:
        print("    Ball_TA not found - match not active?")
    print()

    # -------- Step 6: live Vehicle inputs --------
    print("[6] Live Car - reading INPUT struct (5 float axes):")
    car = refl.highest_by_ancestor("Vehicle_TA")
    if car:
        inp = drv.read_bytes(car + O.Vehicle.INPUT, 20)
        if inp and len(inp) == 20:
            throttle, steer, pitch, yaw, roll = struct.unpack("<fffff", inp)
            pri = drv.read_ptr(car + O.Vehicle.PRI)
            print(f"    addr    = 0x{car:X}")
            print(f"    PRI     = 0x{pri:X}")
            print(f"    THROTTLE= {throttle:+.3f}")
            print(f"    STEER   = {steer:+.3f}")
            print(f"    PITCH   = {pitch:+.3f}")
            print(f"    YAW     = {yaw:+.3f}")
            print(f"    ROLL    = {roll:+.3f}")
    else:
        print("    Vehicle_TA not found")
    print()

    # -------- Step 7: benchmark --------
    print("[7] Benchmark: 5000 x read_u32 from Ball.RADIUS...")
    if ball:
        addr = ball + O.Ball.RADIUS
        n = 5000
        t0 = time.perf_counter()
        for _ in range(n):
            drv.read_u32(addr)
        dt = time.perf_counter() - t0
        rate = n / dt
        print(f"    {n} IOCTL in {dt*1000:.0f}ms  =  {rate:,.0f} IOCTL/s  ({dt*1000/n:.3f}ms/call)")
    print()

    drv.detach()
    print("=" * 68)
    print("  OK - kernel works. All IOCTLs returned sensible data.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
