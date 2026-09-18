"""
Analiza taktyczna - helpery dla pro botow.

Wszystko przez pure functions ktore biora World i zwracaja info. Nic nie
modyfikuja stanu. Kazda z tych funkcji moze byc wywolana per tick albo raz
na kilka - zaleznie od bota.

Kluczowe funkcje:
    closest_players_to_ball(world)          - ranking
    my_rotation_index(world, self_car)      - 1st/2nd/3rd man in own team
    is_ball_heading_to_goal(world, team)    - shot detection
    time_to_ball(car, ball)                 - ETA per graczu
    ball_prediction_at(world, dt)           - wygodny wrapper na predict
    who_should_go_for_ball(world, team)     - team play logic
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from prism_sdk import constants as C
from prism_sdk.world import World, Car, Player, Ball
from prism_sdk.predict import ball_at, predict_ball


# ==================================================================
# Distance / ranking
# ==================================================================

def distance_car_to_ball(car: Car, ball: Ball) -> float:
    """Euklidesowy dystans center-to-center."""
    return float(np.linalg.norm(ball.position - car.physics.position))


def distance_to_own_goal(car: Car) -> float:
    goal_center = (C.BLUE_GOAL_CENTER if car.physics.position[1] < 0
                   else C.ORANGE_GOAL_CENTER)
    return float(np.linalg.norm(np.array(goal_center) - car.physics.position))


def closest_players_to_ball(world: World) -> List[Tuple[Player, float]]:
    """Ranking (Player, distance) posortowany rosnaco."""
    scored = []
    for p in world.players:
        if p.car is None:
            continue
        d = distance_car_to_ball(p.car, world.ball)
        scored.append((p, d))
    scored.sort(key=lambda t: t[1])
    return scored


def my_rotation_index(world: World, self_pri_addr: int) -> int:
    """W ktorej rotacji jestem w mojej druzynie (0=first, 1=second, 2=third).
    Zwraca 0 gdy jestem sam / brak druzyny."""
    me = next((p for p in world.players if p.pri_address == self_pri_addr), None)
    if me is None or me.team_num < 0:
        return 0
    team_mates = [p for p in world.players
                  if p.team_num == me.team_num and p.car is not None]
    if len(team_mates) <= 1:
        return 0
    ranked = sorted(team_mates, key=lambda p: distance_car_to_ball(p.car, world.ball))
    for i, p in enumerate(ranked):
        if p.pri_address == self_pri_addr:
            return i
    return 0


# ==================================================================
# Time-to-ball / interception
# ==================================================================

def _rough_time_to_reach(car: Car, target_pos: np.ndarray) -> float:
    """Bardzo prosty ETA: dystans / (predkosc + potential boost).

    Ignoruje kat skretu, koniecznosc skoku itd. Uzywane tylko do
    porownywania graczy miedzy soba (ranking), nie do precyzji."""
    dist = float(np.linalg.norm(target_pos - car.physics.position))
    current_speed = car.physics.speed
    # Zaklada boost jesli mamy > 20% i piłka daleko
    max_reachable = C.CAR_MAX_SPEED if car.boost > 0.2 else C.CAR_MAX_SPEED_NO_BOOST
    avg_speed = max(200.0, (current_speed + max_reachable) / 2)
    return dist / avg_speed


def time_to_ball(car: Car, ball: Ball, prediction_horizon: float = 3.0
                 ) -> Tuple[float, np.ndarray]:
    """Najlepszy ETA gdzie auto trafi w piłke.

    Iteruje po predykcji pilki, dla kazdej pozycji sprawdza czy auto zdaza
    - wybiera pierwszy moment gdy auto dotarloby w piłke.

    Zwraca (eta_sec, ball_pos_at_that_time). eta=INF gdy nie zdarzy w horizon.
    """
    step = 0.05
    t = 0.0
    while t < prediction_horizon:
        ball_pos, _ = ball_at(ball, t)
        car_eta = _rough_time_to_reach(car, ball_pos)
        if car_eta <= t:
            return t, ball_pos
        t += step
    # nie zdarzy - zwroc horizon i last known
    ball_pos, _ = ball_at(ball, prediction_horizon)
    return float("inf"), ball_pos


def who_should_go_for_ball(world: World, team_num: int) -> Optional[Player]:
    """Sposrod graczy team_num, kto ma najkrotszy ETA na piłke."""
    team_mates = [p for p in world.players if p.team_num == team_num and p.car]
    if not team_mates:
        return None
    scored = []
    for p in team_mates:
        eta, _ = time_to_ball(p.car, world.ball, prediction_horizon=2.0)
        scored.append((eta, p))
    scored.sort(key=lambda t: t[0])
    return scored[0][1]


# ==================================================================
# Shot detection
# ==================================================================

@dataclass
class ShotInfo:
    """Wynik analizy trajektorii pilki - czy leci do bramki."""
    is_shot: bool = False           # True gdy trajektoria konczy sie w bramce
    is_on_own_goal: bool = False    # True gdy leci na wlasna bramke
    target_team: int = -1           # 0/1 = ktora bramka
    time_to_goal: float = float("inf")
    goal_position: Optional[np.ndarray] = None   # gdzie wjedzie


def is_ball_heading_to_goal(world: World, from_team_perspective: int = -1,
                            horizon: float = 4.0) -> ShotInfo:
    """Sprawdz czy trajektoria pilki koncze sie w ktorejkolwiek bramce.

    from_team_perspective:
      * -1 = zwroc info o dowolnej bramce
      *  0 = perspektywa blue - `is_on_own_goal` gdy pilka jedzie do bramki blue
      *  1 = perspektywa orange
    """
    info = ShotInfo()
    step = 1.0 / 60.0
    traj = predict_ball(world.ball, dt_max=horizon, step=step)
    for t, pos, vel in traj:
        # Blue goal (y = -FIELD_HALF_LENGTH)
        if C.is_in_goal(pos, team_num=0, tolerance=10.0):
            info.is_shot = True
            info.target_team = 0
            info.time_to_goal = t
            info.goal_position = pos.copy()
            if from_team_perspective == 0:
                info.is_on_own_goal = True
            return info
        # Orange goal
        if C.is_in_goal(pos, team_num=1, tolerance=10.0):
            info.is_shot = True
            info.target_team = 1
            info.time_to_goal = t
            info.goal_position = pos.copy()
            if from_team_perspective == 1:
                info.is_on_own_goal = True
            return info
    return info


# ==================================================================
# Positioning helpers
# ==================================================================

def is_in_own_half(car: Car) -> bool:
    """True gdy auto jest w polowie boiska swojej druzyny."""
    if car is None:
        return False
    # team_num nie jest w Car - inferruj po pozycji lub weź z powiązanego Player
    y = car.physics.position[1]
    # bezpieczna heurystyka: bez wiedzy o teamie - najbliższa bramka
    return abs(y) > 100 and (y < 0) == (car.physics.position[1] < 0)


def defensive_position(world: World, team_num: int) -> np.ndarray:
    """Punkt gdzie powinien stac ostatni obrońca danego teamu."""
    own_goal = np.array(C.BLUE_GOAL_CENTER if team_num == 0 else C.ORANGE_GOAL_CENTER,
                        dtype=np.float64)
    ball = world.ball.position
    # linia miedzy bramka a piłka, 1500 uu przed bramka
    direction = ball - own_goal
    d = np.linalg.norm(direction)
    if d == 0:
        return own_goal.copy()
    unit = direction / d
    return own_goal + unit * 1500.0


def shadow_position(world: World, team_num: int, distance_from_ball: float = 1500.0
                    ) -> np.ndarray:
    """Pozycja shadow: miedzy piłka a wlasna bramka, na okreslonym dystansie od piłki."""
    own_goal = np.array(C.BLUE_GOAL_CENTER if team_num == 0 else C.ORANGE_GOAL_CENTER,
                        dtype=np.float64)
    ball = world.ball.position
    direction = own_goal - ball
    d = np.linalg.norm(direction)
    if d == 0:
        return ball.copy()
    unit = direction / d
    return ball + unit * distance_from_ball


# ==================================================================
# Convenience wrapper na predyc
# ==================================================================

def ball_prediction_at(world: World, dt: float) -> np.ndarray:
    """Krotkie: gdzie pilka bedzie za `dt` sekund."""
    pos, _ = ball_at(world.ball, dt)
    return pos
