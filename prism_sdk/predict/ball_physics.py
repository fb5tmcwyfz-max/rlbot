"""
Wlasna symulacja fizyki pilki.

Uzywana gdy Ball.PredictedPositions z silnika jest puste (mecz online zwykle
wylacza engine prediction). Standard RL fizyka:
  * grawitacja Z = -650 uu/s^2
  * drag = 0.030305/s (multiplikatywny na velocity per sekunde)
  * bounce od scian: restitution 0.6 z tarciem
  * gdy predkosc < 200 uu/s pilka toczy sie (bez odbic Z)

Nie uwzglednia kolizji z autami - tylko czysty balllistic w arenie.

Wydajnosc: 120 Hz krok x 6 sec = 720 iteracji, ~1ms pure Python. Do speedu
mozna zwektoryzowac przez numpy.

Uzycie:
    from prism_sdk.predict import predict_ball
    trajectory = predict_ball(world.ball, dt_max=6.0, step=1/120)
    # trajectory[i] = (time, position, velocity)
    for t, pos, vel in trajectory:
        if pos[2] > 200: ...   # kiedy pilka bedzie 200 uu w powietrzu
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from prism_sdk import constants as C
from prism_sdk.field  import clamp_ball_to_arena, Surface
from prism_sdk.world.ball import Ball


@dataclass
class BallState:
    """Kopia stanu pilki do lokalnej symulacji (mutable)."""
    position: np.ndarray            # (3,)
    velocity: np.ndarray            # (3,)
    angular_velocity: np.ndarray    # (3,)

    @classmethod
    def from_ball(cls, ball: Ball) -> "BallState":
        return cls(
            position=ball.position.copy(),
            velocity=ball.linear_velocity.copy(),
            angular_velocity=ball.angular_velocity.copy(),
        )


def _step(s: BallState, dt: float) -> None:
    """Jedna klatka symulacji. Modyfikuje s IN-PLACE."""
    # 1. Gravity
    s.velocity[2] += C.GRAVITY * dt

    # 2. Drag - multiplikatywne dumping
    drag = math.exp(-C.BALL_DRAG * dt)
    s.velocity *= drag
    s.angular_velocity *= drag

    # 3. Integrate position
    s.position += s.velocity * dt

    # 4. Collisions - delegowane do field.clamp_ball_to_arena
    #    Uwzglednia podloge/sufit/side walls/back walls (z zachowaniem goal
    #    boxa) + corner ramps na 45°.
    old_pos = s.position.copy()
    s.position, hit = clamp_ball_to_arena(s.position, C.BALL_RADIUS)

    if hit is not None:
        # Rescinding velocity wzdluz normalnej powierzchni
        from prism_sdk.field import surface_normal
        n = surface_normal(hit, at_pos=s.position)
        v_along_n = float(np.dot(s.velocity, n))
        if v_along_n < 0:
            # zremuj skladowa normalnie + rewers z restitution
            s.velocity -= (1 + C.BALL_RESTITUTION) * v_along_n * n
            # tarcie na podlodze - ogranicz horizontal component
            if hit == Surface.FLOOR:
                s.velocity[0] *= 0.9
                s.velocity[1] *= 0.9
                if abs(s.velocity[2]) < 10.0:
                    s.velocity[2] = 0.0     # rolling
    # (gdy pilka wleci do goal boxa clamp_ball_to_arena nie clampuje wcale -
    # pozycja zostaje, symulacja moze dalej isc w netto bramki)


def predict_ball(ball: Ball, dt_max: float = 6.0, step: float = 1.0 / 120.0
                 ) -> List[Tuple[float, np.ndarray, np.ndarray]]:
    """Zwraca liste (t, pos, vel) w intervalach `step` az do `dt_max` sekund.

    Nie zapisuje kazdej klatki - default 120 Hz simulation ale trajectory zwracana
    co 1/60s (co 2 kroki). Reguluj step dla wiekszej dokladnosci.
    """
    state = BallState.from_ball(ball)
    traj = []
    t = 0.0
    sample_every = max(1, int((1.0 / 60.0) / step))
    i = 0
    while t <= dt_max:
        if i % sample_every == 0:
            traj.append((t, state.position.copy(), state.velocity.copy()))
        _step(state, step)
        t += step
        i += 1
    return traj


def ball_at(ball: Ball, at_time: float, step: float = 1.0 / 120.0
            ) -> Tuple[np.ndarray, np.ndarray]:
    """Zwraca (pozycja, predkosc) pilki za `at_time` sekund od teraz.

    Szybsza niz predict_ball gdy chcesz tylko jednym momencie."""
    state = BallState.from_ball(ball)
    t = 0.0
    while t < at_time:
        dt = min(step, at_time - t)
        _step(state, dt)
        t += dt
    return state.position.copy(), state.velocity.copy()


def ball_reaches_z(ball: Ball, target_z: float, dt_max: float = 6.0,
                   step: float = 1.0 / 120.0) -> Optional[float]:
    """Kiedy pilka osiagnie wysokosc target_z (unosi sie do niej albo spada)?

    Zwraca czas w sekundach, albo None jesli nie osiagnie w `dt_max`.
    Uzyteczne dla aerial timing - kiedy trzeba odpalic double jump.
    """
    state = BallState.from_ball(ball)
    prev_z = state.position[2]
    t = 0.0
    while t < dt_max:
        _step(state, step)
        t += step
        cur_z = state.position[2]
        # Sprawdz czy przekroczylismy target_z (interpolacja)
        if (prev_z < target_z <= cur_z) or (prev_z > target_z >= cur_z):
            frac = (target_z - prev_z) / (cur_z - prev_z) if cur_z != prev_z else 0.0
            return t - step + frac * step
        prev_z = cur_z
    return None
