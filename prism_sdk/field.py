"""
Geometria areny + helpery zapytan przestrzennych.

Wszystko czysto matematyczne - nie czyta z pamieci gry. Bot uzywa dla decyzji
'gdzie jestem', 'gdzie sciana', 'gdzie odbije sie pilka'.

Kluczowe funkcje:
    surface_at(pos)              - jaka powierzchnia: FLOOR/CEILING/SIDE/BACK/CORNER
    nearest_wall_normal(pos)     - wektor normalnej najblizszej sciany (dla car alignment)
    distance_to_wall(pos)        - najkrotszy dystans do dowolnej sciany
    is_in_corner(pos)            - true jesli w rogu (na corner ramp)
    is_in_own_half(pos, team)    - true jesli w polowie druzyny
    is_in_goal(pos, team)        - true jesli w bramce druzyny (bramka to team_num
                                   defenderskiej druzyny - blue defende blue goal)
    project_to_wall(pos)         - najblizszy punkt na krawedzi/scianie do pos
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Tuple

import numpy as np

from prism_sdk import constants as C


# ==================================================================
# Surface enumeration
# ==================================================================

class Surface(IntEnum):
    NONE      = 0
    FLOOR     = 1
    CEILING   = 2
    SIDE_WALL_POS_X = 3   # x = +4096
    SIDE_WALL_NEG_X = 4   # x = -4096
    BACK_WALL_POS_Y = 5   # y = +5120 (orange side)
    BACK_WALL_NEG_Y = 6   # y = -5120 (blue side)
    CORNER    = 7         # 45° corner ramp (jeden z 4)


# ==================================================================
# Surface detection
# ==================================================================

def surface_at(position, tol: float = C.WALL_TOUCH_TOL) -> Surface:
    """Jaka powierzchnia jest 'pod' danym punktem w promieniu `tol`.

    Nie sprawdza wszystkich naraz - najbardziej pasujaca (najmniejsza odleglosc).
    Jesli pozycja jest wewnatrz areny (>tol od wszystkich powierzchni) -> NONE.
    """
    x, y, z = float(position[0]), float(position[1]), float(position[2])

    d_floor   = z
    d_ceiling = C.CEILING_Z - z
    d_side_pos = C.SIDE_WALL_X - x
    d_side_neg = x + C.SIDE_WALL_X   # dystans od x = -4096
    d_back_pos = C.BACK_WALL_Y - y
    d_back_neg = y + C.BACK_WALL_Y

    # Corner: |x| + |y| - CORNER_WALL_SUM zblizone do 0 (na diagonalu)
    d_corner   = C.CORNER_WALL_SUM - (abs(x) + abs(y))
    # scale corner distance: linia diagonalna, dystans "perpendicular" to  d/sqrt(2)
    d_corner_perp = d_corner / (2 ** 0.5)

    candidates = [
        (d_floor,      Surface.FLOOR),
        (d_ceiling,    Surface.CEILING),
        (d_side_pos,   Surface.SIDE_WALL_POS_X),
        (d_side_neg,   Surface.SIDE_WALL_NEG_X),
        (d_back_pos,   Surface.BACK_WALL_POS_Y),
        (d_back_neg,   Surface.BACK_WALL_NEG_Y),
        (d_corner_perp, Surface.CORNER),
    ]

    dist, best = min(candidates, key=lambda p: p[0])
    if dist <= tol:
        return best
    return Surface.NONE


def is_on_ground(position, tol: float = C.GROUND_TOUCH_Z_MAX) -> bool:
    return float(position[2]) <= tol


def is_on_ceiling(position, tol: float = C.WALL_TOUCH_TOL) -> bool:
    return float(position[2]) >= C.CEILING_Z - tol


def is_on_wall(position, tol: float = C.WALL_TOUCH_TOL) -> bool:
    """True gdy auto jest na scianie bocznej, tylnej albo cornerze
    (podloga i sufit odrzucone)."""
    s = surface_at(position, tol)
    return s not in (Surface.NONE, Surface.FLOOR, Surface.CEILING)


def is_in_corner(position, tol: float = C.WALL_TOUCH_TOL) -> bool:
    return surface_at(position, tol) == Surface.CORNER


# ==================================================================
# Wall normals - do alignment auta na scianach
# ==================================================================

_UP    = np.array([0., 0., 1.], dtype=np.float64)
_DOWN  = np.array([0., 0., -1.], dtype=np.float64)
_EAST  = np.array([-1., 0., 0.], dtype=np.float64)   # normal side +X = kierunek do srodka
_WEST  = np.array([ 1., 0., 0.], dtype=np.float64)
_NORTH = np.array([0., -1., 0.], dtype=np.float64)   # normal back +Y
_SOUTH = np.array([0.,  1., 0.], dtype=np.float64)


def surface_normal(surface: Surface, at_pos: Optional[np.ndarray] = None) -> np.ndarray:
    """Wektor normalnej powierzchni (do wewnatrz areny). Dla CORNER wymaga at_pos
    zeby okreslic ktory z 4 corners."""
    if surface == Surface.FLOOR:            return _UP
    if surface == Surface.CEILING:          return _DOWN
    if surface == Surface.SIDE_WALL_POS_X:  return _EAST
    if surface == Surface.SIDE_WALL_NEG_X:  return _WEST
    if surface == Surface.BACK_WALL_POS_Y:  return _NORTH
    if surface == Surface.BACK_WALL_NEG_Y:  return _SOUTH
    if surface == Surface.CORNER:
        if at_pos is None:
            return _UP
        # Corner normal to unit(-sign(x), -sign(y), 0) / sqrt(2)
        sx = 1.0 if at_pos[0] > 0 else -1.0
        sy = 1.0 if at_pos[1] > 0 else -1.0
        return np.array([-sx, -sy, 0.0]) * (1.0 / (2 ** 0.5))
    return _UP


def nearest_wall_normal(position) -> np.ndarray:
    """Normalnej najblizszej powierzchni (podloga tez sie liczy jako 'sciana')."""
    return surface_normal(surface_at(position, tol=1e9), at_pos=position)


# ==================================================================
# Distances
# ==================================================================

def distance_to_wall(position) -> float:
    """Najmniejszy dystans do jakiejkolwiek powierzchni areny."""
    x, y, z = float(position[0]), float(position[1]), float(position[2])
    d_side   = C.SIDE_WALL_X - abs(x)
    d_back   = C.BACK_WALL_Y - abs(y)
    d_floor  = z
    d_ceil   = C.CEILING_Z - z
    d_corner = (C.CORNER_WALL_SUM - (abs(x) + abs(y))) / (2 ** 0.5)
    return max(0.0, min(d_side, d_back, d_floor, d_ceil, d_corner))


def distance_to_ground(position) -> float:
    return max(0.0, float(position[2]))


def distance_to_ceiling(position) -> float:
    return max(0.0, C.CEILING_Z - float(position[2]))


def distance_to_side_wall(position) -> float:
    """Do najblizszej sciany bocznej (x = ±4096)."""
    return max(0.0, C.SIDE_WALL_X - abs(float(position[0])))


def distance_to_back_wall(position) -> float:
    """Do najblizszej sciany tylnej (y = ±5120)."""
    return max(0.0, C.BACK_WALL_Y - abs(float(position[1])))


def distance_to_goal_line(position, team_num: int) -> float:
    """Dystans do goal-line druzyny team_num (bramka ktorej broni team).
    team_num=0 (blue) -> y = -5120"""
    goal_y = C.BLUE_GOAL_Y if team_num == 0 else C.ORANGE_GOAL_Y
    return abs(float(position[1]) - goal_y)


# ==================================================================
# Positioning
# ==================================================================

def is_in_own_half(position, team_num: int) -> bool:
    """True gdy pozycja jest w polowie druzyny team_num.
    Blue = y < 0, Orange = y > 0."""
    y = float(position[1])
    if team_num == 0:
        return y < 0
    else:
        return y > 0


def is_in_attacking_third(position, team_num: int) -> bool:
    """True gdy w tercji atakujacej (bliskiej bramce przeciwnika).
    Trzecia = 1/3 dlugosci pola od goal-line przeciwnika."""
    y = float(position[1])
    opp_y = C.ORANGE_GOAL_Y if team_num == 0 else C.BLUE_GOAL_Y
    third_boundary = opp_y - (C.FIELD_LENGTH / 3) * (1 if team_num == 0 else -1)
    if team_num == 0:
        return y > third_boundary
    return y < third_boundary


def side_of_field(y: float) -> int:
    """0 = blue half (y<0), 1 = orange half (y>=0)."""
    return 0 if y < 0 else 1


# ==================================================================
# Ball collision helpers - dla bardziej dokladnej symulacji
# ==================================================================

def clamp_ball_to_arena(position: np.ndarray, ball_radius: float = C.BALL_RADIUS
                        ) -> Tuple[np.ndarray, Optional[Surface]]:
    """Wciska pozycje pilki wewnątrz areny (uwzgl. corner ramps). Zwraca
    (clamped_position, surface_hit or None).

    Uzywane w ball_physics.step dla dokladnego bounce - zamiast prostych
    walls dolapuje corner ramps.
    """
    x, y, z = float(position[0]), float(position[1]), float(position[2])
    hit: Optional[Surface] = None

    # Floor
    if z < ball_radius:
        z = ball_radius
        hit = Surface.FLOOR
    # Ceiling
    if z > C.CEILING_Z - ball_radius:
        z = C.CEILING_Z - ball_radius
        hit = Surface.CEILING

    # Side walls (chyba ze jestesmy w obszarze bramki)
    in_goal_zone_x = abs(x) < C.GOAL_HALF_WIDTH
    in_goal_zone_z = z < C.GOAL_HEIGHT
    in_goal_zone_y = abs(y) > C.BACK_WALL_Y - 50 and in_goal_zone_x and in_goal_zone_z

    if abs(x) > C.SIDE_WALL_X - ball_radius:
        # Pomin gdy jesteśmy między goal-line a nettem (goal box)
        if not in_goal_zone_y:
            x = (1 if x > 0 else -1) * (C.SIDE_WALL_X - ball_radius)
            hit = Surface.SIDE_WALL_POS_X if x > 0 else Surface.SIDE_WALL_NEG_X

    # Back walls (chyba ze w bramce)
    if abs(y) > C.BACK_WALL_Y - ball_radius:
        if not (in_goal_zone_x and in_goal_zone_z):
            y = (1 if y > 0 else -1) * (C.BACK_WALL_Y - ball_radius)
            hit = Surface.BACK_WALL_POS_Y if y > 0 else Surface.BACK_WALL_NEG_Y

    # Corner ramps - plaszczyzna |x| + |y| = CORNER_WALL_SUM
    sum_xy = abs(x) + abs(y)
    diagonal_dist_from_corner = C.CORNER_WALL_SUM - sum_xy
    # perpendicular dystans do plaszczyzny corner = diag_dist / sqrt(2)
    perp_dist = diagonal_dist_from_corner / (2 ** 0.5)
    if perp_dist < ball_radius:
        # Wcisnij wzdluz normalnej (-sign(x), -sign(y)) / sqrt(2)
        push = (ball_radius - perp_dist) * (2 ** 0.5)
        sx = 1 if x > 0 else -1
        sy = 1 if y > 0 else -1
        x -= sx * push * 0.5
        y -= sy * push * 0.5
        hit = Surface.CORNER

    return np.array([x, y, z], dtype=np.float64), hit
