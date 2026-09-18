"""
Ball - pilka w RL.

Trzyma:
  * fizyke (position, quaternion, linear/angular velocity, radius)
  * ostatnie odbicie (touch info)
  * trajektorie z silnika gry (`PredictedPositions` - policzone przez RL)

Trajektoria z silnika: gra sama liczy do 60+ punktow ~120 Hz w Freeplay.
W meczu zwykle wylaczone (bEnabled=False) - wtedy `predicted_positions` bedzie
puste i trzeba uzyc naszej wlasnej predykcji z `prism_sdk.predict`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from prism_sdk.low.driver import Driver
from prism_sdk.low        import offsets as O
from prism_sdk.world._common import (
    Vec3, Quat, read_rbstate, read_tarray_ptrs,
)


@dataclass
class BallHit:
    """Ostatnie odbicie pilki od auta (jesli dostepne)."""
    car_addr:  int = 0        # adres auta ktore uderzylo (do resolve po lookup)
    team_num:  int = -1       # 0=blue, 1=orange, -1=nieznane
    world_time: float = 0.0   # timestamp w sekundach od startu meczu


@dataclass
class Ball:
    """Snapshot pilki. Immutable po utworzeniu przez `Ball.read()`."""

    #: Adres obiektu Ball_TA w pamieci (dla debugowania)
    address: int = 0

    #: Pozycja (x, y, z) w unreal units. y+ = orange goal side.
    position: Vec3 = field(default_factory=lambda: np.zeros(3))

    #: Quaternion (x, y, z, w) - orientacja
    quaternion: Quat = field(default_factory=lambda: np.array([0., 0., 0., 1.]))

    #: Predkosc liniowa (uu/s). Max ~6000 uu/s.
    linear_velocity: Vec3 = field(default_factory=lambda: np.zeros(3))

    #: Predkosc katowa (rad/s)
    angular_velocity: Vec3 = field(default_factory=lambda: np.zeros(3))

    #: Promien pilki (unreal units, standardowo ~92)
    radius: float = 92.75

    #: Ostatnie odbicie (None jesli nie bylo)
    last_hit: Optional[BallHit] = None

    #: Trajektoria z silnika gry - lista pozycji do przodu (jesli
    #: PredictedPositions/TrajectoryComponent wlaczone). Pusta gdy niedostepne.
    predicted_positions: List[Vec3] = field(default_factory=list)

    # ---- API ----

    @property
    def speed(self) -> float:
        """Chwilowa predkosc skalarna w uu/s."""
        return float(np.linalg.norm(self.linear_velocity))

    @classmethod
    def read(cls, drv: Driver, ball_addr: int, max_prediction_points: int = 60) -> "Ball":
        """Zbuduj snapshot pilki z pamieci."""
        if ball_addr == 0:
            return cls()

        pos, quat, lv, av = read_rbstate(drv, ball_addr)

        radius = drv.read_f32(ball_addr + O.Ball.RADIUS)
        if radius <= 0:
            radius = 92.75

        # Ostatnie odbicie: `HitTeamNum` @ 0x891 (uint8). CurrentAffector @ 0x9A0
        # jako pointer do UCar_TA (najswieziejszy). LastHitWorldTime @ 0x8B0.
        hit_team = drv.read_u8(ball_addr + O.Ball.HIT_TEAM_NUM)
        hit_time = drv.read_f32(ball_addr + O.Ball.LAST_HIT_WORLD_TIME)
        car_ptr  = drv.read_ptr(ball_addr + O.Ball.CURRENT_AFFECTOR)
        last_hit = None
        if car_ptr != 0 or hit_time > 0:
            last_hit = BallHit(
                car_addr=car_ptr,
                team_num=hit_team if hit_team in (0, 1) else -1,
                world_time=hit_time,
            )

        # Predykcja trajektorii - z Ball.PredictedPositions albo TrajectoryComponent
        predicted = _read_predicted_positions(drv, ball_addr, max_prediction_points)

        return cls(
            address=ball_addr,
            position=pos,
            quaternion=quat,
            linear_velocity=lv,
            angular_velocity=av,
            radius=radius,
            last_hit=last_hit,
            predicted_positions=predicted,
        )


def _read_predicted_positions(drv: Driver, ball_addr: int, max_count: int) -> List[Vec3]:
    """Sprobuj odczytac trajektorie z jednego z dwoch mozliwych zrodel.

    1) `Ball.PredictedPositions` (TArray<FPredictedPosition>) - istnieje zawsze,
       zapelniane gdy gra aktywnie liczy trajektorie.
    2) `Ball.TrajectoryComponent -> TrajectoryPoints` (TArray<FVector>) - stare,
       tylko w Freeplay z widocznym targetem.

    Wybierzemy to co ma > 0 punktow.
    """
    # Sprobuj PredictedPositions najpierw (nowsza droga)
    positions = _read_predicted_positions_array(drv, ball_addr + O.Ball.PREDICTED_POSITIONS,
                                                 max_count, stride=12)
    if positions:
        return positions

    # Fallback: BallTrajectoryComponent.TrajectoryPoints
    traj_comp = drv.read_ptr(ball_addr + O.Ball.TRAJECTORY_COMPONENT)
    if traj_comp == 0:
        return []
    return _read_predicted_positions_array(drv, traj_comp + O.BallTrajectory.TRAJECTORY_POINTS,
                                            max_count, stride=12)


def _read_predicted_positions_array(drv: Driver, tarray_addr: int, max_count: int,
                                     stride: int) -> List[Vec3]:
    """Generic TArray<Vec3-like> reader. `stride` = rozmiar jednego wpisu w bajtach.

    FPredictedPosition ma inny layout niz FVector ale pierwsze 12 bajtow to
    zawsze pozycja. Uzywamy tylko tego przedrostka.
    """
    ptr, count, _ = drv.read_tarray(tarray_addr)
    if ptr == 0 or count <= 0:
        return []
    count = min(count, max_count)
    data = drv.read_bytes(ptr, count * stride)
    if not data:
        return []
    out: List[Vec3] = []
    for i in range(count):
        off = i * stride
        if off + 12 > len(data):
            break
        x, y, z = struct.unpack_from("<fff", data, off)
        out.append(np.array([x, y, z], dtype=np.float64))
    return out
