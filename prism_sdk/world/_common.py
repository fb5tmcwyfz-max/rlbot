"""Wspolne helpery + typy do warstwy world/.

Nic tu nie eksportujemy do usera - to jest wewnetrzne.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from prism_sdk.low.driver  import Driver
from prism_sdk.low         import offsets as O


Vec3 = np.ndarray   # shape (3,) float64
Quat = np.ndarray   # shape (4,) float64 - (x, y, z, w)


def read_rbstate(drv: Driver, obj_addr: int) -> Tuple[Vec3, Quat, Vec3, Vec3]:
    """Odczyt FReplicatedRBState z RBActor_TA + 0x5D0.

    RBState to replikowany stan fizyczny - authoritative dla piłki i aut.
    Zwraca (position, quaternion(xyzw), linear_vel, angular_vel).

    Pojedynczy read 0x40 bytes zamiast 4 osobnych - jedno IOCTL.
    """
    base = obj_addr + O.RBActor.RB_STATE
    data = drv.read_bytes(base, O.RBState.SIZE)
    if len(data) != O.RBState.SIZE:
        return _zero_state()

    qx, qy, qz, qw = struct.unpack_from("<ffff", data, O.RBState.QUATERNION)
    px, py, pz     = struct.unpack_from("<fff",  data, O.RBState.LOCATION)
    vx, vy, vz     = struct.unpack_from("<fff",  data, O.RBState.LINEAR_VELOCITY)
    ax, ay, az     = struct.unpack_from("<fff",  data, O.RBState.ANGULAR_VELOCITY)

    return (
        np.array([px, py, pz], dtype=np.float64),
        np.array([qx, qy, qz, qw], dtype=np.float64),
        np.array([vx, vy, vz], dtype=np.float64),
        np.array([ax, ay, az], dtype=np.float64),
    )


# USUNIETE: read_actor_physics() - odczyt fizyki z pol AActor (Location 0x90,
# Rotation 0x9C), czyli stanu LOKALNEGO klienta. Nie mial ani jednego wywolania,
# a sam pomysl jest sprzeczny z zasada tego SDK: wszystko czytamy ze stanu
# REPLIKOWANEGO z serwera (FReplicatedRBState @ RBActor+0x5D0, read_rbstate).
# W meczu autorytatywny jest serwer - stan lokalny to tylko predykcja.


def rotator_to_matrix(pitch_rad: float, yaw_rad: float, roll_rad: float) -> np.ndarray:
    """FRotator (w radianach) -> macierz obrotu 3x3 (kolumny: fwd, right, up).
    Standardowy wzor RLBot/UE3."""
    cp, sp = math.cos(pitch_rad), math.sin(pitch_rad)
    cy, sy = math.cos(yaw_rad),   math.sin(yaw_rad)
    cr, sr = math.cos(roll_rad),  math.sin(roll_rad)

    forward = (cp * cy, cp * sy, sp)
    right   = (cy * sr * sp - cr * sy, sy * sr * sp + cr * cy, -sr * cp)
    up      = (-cr * cy * sp - sr * sy, -cr * sy * sp + sr * cy, cr * cp)

    return np.array([
        [forward[0], right[0], up[0]],
        [forward[1], right[1], up[1]],
        [forward[2], right[2], up[2]],
    ], dtype=np.float64)


def rotator_int32_to_radians(pi: int, yi: int, ri: int) -> Tuple[float, float, float]:
    scale = (2.0 * math.pi) / 65536.0
    return pi * scale, yi * scale, ri * scale


def quat_to_matrix(quat_xyzw: np.ndarray) -> np.ndarray:
    """Quaternion (x,y,z,w) -> macierz obrotu 3x3. Kolumny: fwd, right, up."""
    x, y, z, w = -quat_xyzw[0], -quat_xyzw[1], -quat_xyzw[2], -quat_xyzw[3]
    n = x*x + y*y + z*z + w*w
    if n == 0:
        return np.eye(3, dtype=np.float64)
    s = 2.0 / n

    fx = 1.0 - s * (y*y + z*z)
    fy = s * (x*y + z*w)
    fz = s * (x*z - y*w)
    rx = s * (x*y - z*w)
    ry = 1.0 - s * (x*x + z*z)
    rz = s * (y*z + x*w)
    ux = s * (x*z + y*w)
    uy = s * (y*z - x*w)
    uz = 1.0 - s * (x*x + y*y)

    return np.array([
        [fx, rx, ux],
        [fy, ry, uy],
        [fz, rz, uz],
    ], dtype=np.float64)


def _zero_state():
    return (
        np.zeros(3, dtype=np.float64),
        np.array([0., 0., 0., 1.], dtype=np.float64),
        np.zeros(3, dtype=np.float64),
        np.zeros(3, dtype=np.float64),
    )


def read_fstring(drv: Driver, addr: int, max_chars: int = 64) -> str:
    """FString = { WCHAR* Data; int32 Count; int32 Max }. Data konczy sie null."""
    data = drv.read_bytes(addr, 16)
    if len(data) != 16:
        return ""
    data_ptr, count, _ = struct.unpack("<Qii", data)
    if data_ptr == 0 or count <= 0 or count > max_chars:
        return ""
    raw = drv.read_bytes(data_ptr, min(count, max_chars) * 2)
    if not raw:
        return ""
    try:
        return raw.decode("utf-16-le").rstrip("\x00")
    except Exception:
        return ""


def read_tarray_ptrs(drv: Driver, tarray_addr: int, max_count: int = 128):
    """Odczyt TArray<T*> jako lista pointerów."""
    ptr, count, _ = drv.read_tarray(tarray_addr)
    if ptr == 0 or count <= 0:
        return ()
    count = min(count, max_count)
    return drv.read_ptr_array(ptr, count)
