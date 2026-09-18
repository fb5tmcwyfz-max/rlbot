"""
Shim: prism_sdk.World -> rlgym.GameState (duck-type).

Oryginalny CoyoteObs.py (i pretrained_agents/nexto/nexto_v2_obs.py) oczekuja
obiektow rlgym: `GameState`, `PlayerData`, `PhysicsObject`, `boost_pads: np.ndarray[34]`.

Zamiast przepisywac obs builder na nasz World (73 KB kodu!), robimy cienki
adapter ktory eksponuje TE SAME atrybuty co rlgym oczekuje - obs builder nie
zauwazy roznicy.

Wymagane atrybuty rlgym API (skompilowane z uzycia w Coyote/Nexto obs builders):

GameState:
    .ball            PhysicsObject
    .inverted_ball   PhysicsObject (mirror through Y=0)
    .players         list[PlayerData]
    .boost_pads      np.ndarray shape (34,) dtype float32 (1.0=avail, 0.0=picked)
    .inverted_boost_pads (mirror)
    .blue_score, .orange_score  int

PlayerData:
    .car_id          int
    .team_num        int 0/1
    .car_data        PhysicsObject
    .inverted_car_data (mirror)
    .boost_amount    float 0..1
    .on_ground       bool
    .has_flip        bool
    .is_demoed       bool
    .has_jump        bool (opcjonalne)
    .ball_touched    bool (moze byc False - Coyote sam sprawdza)
    .match_goals/saves/shots/... (opcjonalne)

PhysicsObject:
    .position           np.ndarray (3,)
    .quaternion         np.ndarray (4,) - (x, y, z, w) format
    .linear_velocity    np.ndarray (3,)
    .angular_velocity   np.ndarray (3,)
    .rotation_mtx()     -> np.ndarray (3, 3)  method!
    .euler_angles()     -> np.ndarray (3,)    method! (opcjonalne)
    .forward()          -> np.ndarray (3,)    method!  (kolumna 0 rot_mtx)

Konwencja Inverted:
    Blue perspective - orange side is 'north', y+ = attacking direction.
    Orange perspective - musimy zwrocic mirror: x <- -x, y <- -y (obrocone 180°).
    Prawie wszystkie modele trenowane sa jako "always blue" - inverted_* zwraca
    orange stan jak gdyby to byl blue.
"""

from __future__ import annotations

from typing import List, Optional

import time

import numpy as np

from prism_sdk.world import World, Player, Ball
from prism_sdk.world.local import resolve_local_pawn as _resolve_local_pawn


# ==================================================================
# Identyfikacja LOKALNEGO auta (odporna na respawny)
# ==================================================================
#
# KRYTYCZNE: self_car_index z Managera to indeks w World.cars, ale obs
# buildery indeksuja World.players (inny skan GObjects, inna kolejnosc, moze
# krotsza po filtrze). W meczu te przestrzenie sie rozjezdzaja -> bot bierze
# CUDZE auto jako "ja". Dlatego selfa wybieramy po TOZSAMOSCI (adres pawna),
# resolwowanej co tick z LocalPlayers chain - tak samo jak robi PatchSender.

def resolve_local_pawn(world: World) -> int:
    """Zwraca adres Vehicle_TA* lokalnego auta (0 = respawn/menu/spectator).

    Re-export - implementacja siedzi w prism_sdk/world/local.py (ten sam chain
    resolwuje tez PlayerController dla kanalu sterowania PC).
    """
    return _resolve_local_pawn(world)


#: Ile kolejnych nieudanych rozpoznan tolerujemy, zanim naprawde oddamy
#: sterowanie. Przy 240 Hz to ~0.4 s - dosc, zeby przetrwac przerwe na gola
#: czy respawn, za malo, zeby sterowac obcym autem po zmianie skladu.
_SELF_GRACE_TICKS = 100

#: (indeks, adres pawna, licznik nieudanych prob) - patrz komentarz nizej.
_last_self: Optional[int] = None
_last_pawn: int = 0
_self_misses: int = 0
#: Ile razy lacznie zadzialalo podtrzymanie. Diagnostyka: jesli to rosnie
#: w trakcie meczu, lancuch wskaznikow jest niestabilny.
self_resolve_fallbacks: int = 0


def resolve_self_player_index(world: World) -> Optional[int]:
    """Indeks w World.players (== shim.car_id) odpowiadajacy lokalnemu autu.

    PODTRZYMANIE OSTATNIEJ ZNANEJ WARTOSCI - i to jest tu najwazniejsze.

    Lancuch LocalPlayers[0] -> PC+0x280 -> Pawn potrafi chwilowo nie wypalic:
    przy golu, respawnie, powtorce, albo gdy lista graczy odswiezy sie w innym
    momencie niz wskaznik. Wczesniej kazde takie potkniecie zwracalo None, a
    adapter odpowiadal na to `ControllerState.neutral()` - czyli bot puszczal
    wszystkie przyciski i auto toczylo sie bezwladnie. Z boku wygladalo to jak
    "bot glupieje i nie jedzie do pilki", mimo ze siec byla sprawna.

    RLBot tego problemu nie ma, bo indeks bota jest staly przez caly mecz.
    Robimy to samo: gdy rozpoznanie zawiedzie, przez ~0.4 s uzywamy ostatniego
    znanego auta. Dopiero potem oddajemy sterowanie - zeby po realnej zmianie
    skladu nie sterowac cudzym autem.
    """
    global _last_self, _last_pawn, _self_misses, self_resolve_fallbacks

    pawn = resolve_local_pawn(world)
    if pawn:
        for i, p in enumerate(world.players):
            if p.car_address == pawn:
                _last_self, _last_pawn, _self_misses = i, pawn, 0
                return i

    # Nie udalo sie. Jesli mamy swieza pamiec i auto nadal jest na liscie
    # pod tym samym adresem, uznajemy to za chwilowa dziure w odczycie.
    _self_misses += 1
    if _last_self is not None and _self_misses <= _SELF_GRACE_TICKS:
        players = world.players
        if _last_self < len(players) and players[_last_self].car_address == _last_pawn:
            self_resolve_fallbacks += 1
            return _last_self

    if _self_misses > _SELF_GRACE_TICKS:
        _last_self, _last_pawn = None, 0
    return None


# ==================================================================
# PhysicsObject shim
# ==================================================================

_MIRROR_XY = np.array([-1.0, -1.0, 1.0], dtype=np.float64)


class _PhysicsShim:
    """Duck-type of rlgym PhysicsObject."""

    __slots__ = ("position", "quaternion", "linear_velocity", "angular_velocity", "_rot_mtx")

    def __init__(self, position, quaternion, linear_velocity, angular_velocity,
                 rot_mtx: Optional[np.ndarray] = None):
        self.position         = np.asarray(position, dtype=np.float64)
        self.quaternion       = np.asarray(quaternion, dtype=np.float64)
        self.linear_velocity  = np.asarray(linear_velocity, dtype=np.float64)
        self.angular_velocity = np.asarray(angular_velocity, dtype=np.float64)
        self._rot_mtx         = rot_mtx    # opcjonalnie z prism_sdk (dokladniejsze niz z quat)

    def rotation_mtx(self) -> np.ndarray:
        if self._rot_mtx is not None:
            return self._rot_mtx
        # Fallback: policz z quaternion
        from prism_sdk.world._common import quat_to_matrix
        return quat_to_matrix(self.quaternion)

    def forward(self) -> np.ndarray:
        return self.rotation_mtx()[:, 0]

    def right(self) -> np.ndarray:
        return self.rotation_mtx()[:, 1]

    def up(self) -> np.ndarray:
        return self.rotation_mtx()[:, 2]

    def invert(self) -> "_PhysicsShim":
        """Mirror przez xy (obrocenie o 180° wokol Z)."""
        pos = self.position * _MIRROR_XY
        vel = self.linear_velocity * _MIRROR_XY
        av  = self.angular_velocity * _MIRROR_XY
        # Quaternion mirror: (x,y,z,w) -> (-x,-y,z,w) (obrocenie yaw o 180°)
        q = self.quaternion
        q_inv = np.array([-q[0], -q[1], q[2], q[3]])
        # Rot matrix mirror: R' = M R M   gdzie M = diag(-1,-1,1)
        rot = None
        if self._rot_mtx is not None:
            m = np.diag(_MIRROR_XY)
            rot = m @ self._rot_mtx @ m
        return _PhysicsShim(pos, q_inv, vel, av, rot_mtx=rot)


# ==================================================================
# PlayerData shim
# ==================================================================

class _PlayerShim:
    """Duck-type of rlgym PlayerData."""

    __slots__ = ("car_id", "team_num", "car_data", "inverted_car_data",
                 "boost_amount", "on_ground", "has_flip", "is_demoed",
                 "has_jump", "ball_touched",
                 "match_goals", "match_saves", "match_shots",
                 "match_assists", "match_demolishes")

    def __init__(self, car_id: int, team_num: int, car_data: _PhysicsShim,
                 boost_amount: float, on_ground: bool, has_flip: bool,
                 is_demoed: bool = False, has_jump: bool = True,
                 ball_touched: bool = False,
                 stats: Optional[dict] = None):
        self.car_id            = car_id
        self.team_num          = team_num
        self.car_data          = car_data
        self.inverted_car_data = car_data.invert()
        self.boost_amount      = float(boost_amount)
        self.on_ground         = bool(on_ground)
        self.has_flip          = bool(has_flip)
        self.is_demoed         = bool(is_demoed)
        self.has_jump          = bool(has_jump)
        self.ball_touched      = bool(ball_touched)
        s = stats or {}
        self.match_goals       = int(s.get("goals", 0))
        self.match_saves       = int(s.get("saves", 0))
        self.match_shots       = int(s.get("shots", 0))
        self.match_assists     = int(s.get("assists", 0))
        self.match_demolishes  = int(s.get("demolishes", 0))


# ==================================================================
# GameState shim
# ==================================================================

class _GameStateShim:
    """Duck-type of rlgym GameState.

    Uzywane: obs_builder.build_obs(player, state, previous_action).
    """

    __slots__ = ("ball", "inverted_ball", "players", "boost_pads",
                 "inverted_boost_pads", "blue_score", "orange_score", "game_type")

    def __init__(self):
        self.ball               = _PhysicsShim(np.zeros(3), np.array([0,0,0,1.]),
                                               np.zeros(3), np.zeros(3))
        self.inverted_ball      = self.ball.invert()
        self.players: List[_PlayerShim] = []
        self.boost_pads         = np.zeros(34, dtype=np.float32)
        self.inverted_boost_pads = np.zeros(34, dtype=np.float32)
        self.blue_score         = 0
        self.orange_score       = 0
        self.game_type          = 0    # 0=soccar; opcjonalny atrybut


# ==================================================================
# World -> rlgym-shim conversion
# ==================================================================

# 34 standardowe pozycje boost padow (soccar), kolejnosc RLGym.
# Uzywane do mapowania prism_sdk.boost_pads (arbitralna kolejnosc) na 34-element
# array w kolejnosci rlgym.
BOOST_LOCATIONS_RLGYM = np.array([
    ( 0.0,    -4240.0,  70.0),
    (-1792.0, -4184.0,  70.0),
    ( 1792.0, -4184.0,  70.0),
    (-3072.0, -4096.0,  73.0),
    ( 3072.0, -4096.0,  73.0),
    (-940.0,  -3308.0,  70.0),
    ( 940.0,  -3308.0,  70.0),
    ( 0.0,    -2816.0,  70.0),
    (-3584.0, -2484.0,  70.0),
    ( 3584.0, -2484.0,  70.0),
    (-1788.0, -2300.0,  70.0),
    ( 1788.0, -2300.0,  70.0),
    (-2048.0, -1036.0,  70.0),
    ( 0.0,    -1024.0,  70.0),
    ( 2048.0, -1036.0,  70.0),
    (-3584.0,     0.0,  73.0),
    (-1024.0,     0.0,  70.0),
    ( 1024.0,     0.0,  70.0),
    ( 3584.0,     0.0,  73.0),
    (-2048.0,  1036.0,  70.0),
    ( 0.0,     1024.0,  70.0),
    ( 2048.0,  1036.0,  70.0),
    (-1788.0,  2300.0,  70.0),
    ( 1788.0,  2300.0,  70.0),
    (-3584.0,  2484.0,  70.0),
    ( 3584.0,  2484.0,  70.0),
    ( 0.0,     2816.0,  70.0),
    (-940.0,   3310.0,  70.0),
    ( 940.0,   3308.0,  70.0),
    (-3072.0,  4096.0,  73.0),
    ( 3072.0,  4096.0,  73.0),
    (-1792.0,  4184.0,  70.0),
    ( 1792.0,  4184.0,  70.0),
    ( 0.0,     4240.0,  70.0),
], dtype=np.float64)


def _match_boost_pads_to_rlgym(world_pads) -> np.ndarray:
    """prism_sdk boost pady (arbitrary order) -> np.array[34] w kolejnosci rlgym.

    Dopasowanie po XY (min distance). Zwraca 0.0 gdy pad picked, 1.0 gdy avail.
    """
    out = np.ones(34, dtype=np.float32)   # default: avail
    for i, target in enumerate(BOOST_LOCATIONS_RLGYM):
        best = None
        best_d = float("inf")
        for pad in world_pads:
            dx = pad.position[0] - target[0]
            dy = pad.position[1] - target[1]
            d = dx*dx + dy*dy
            if d < best_d:
                best_d = d
                best = pad
        if best is not None and best_d < 200*200:
            out[i] = 1.0 if best.is_active else 0.0
    return out


# ==================================================================
# STAN MIEDZY KLATKAMI - tolerancja utraty kontaktu kol
# ==================================================================
#
# rlgym_compat/game_state.py:57 (czyli to, czego uzywa RLBotowy Nexto):
#
#     player_data.on_ground = (player_info.has_wheel_contact
#                              or self._on_ground_ticks[index] <= 6)
#
# NIE jest to surowa flaga kontaktu kol, tylko flaga z 6-tikowa (50 ms)
# tolerancja. Powod jest praktyczny: przy jezdzie po nierownosciach, krawezniku
# czy po lekkim odbiciu kola traca kontakt na pojedyncze tiki. Surowa flaga
# migocze wtedy kilka razy na sekunde.
#
# Dla Nexto to nie jest kosmetyka: `on_ground` to jedna z 24 cech kazdego auta
# w obserwacji i przelacza polityke miedzy jazda a lotem. Podawana surowo
# powodowala, ze siec oscylowala miedzy sterowaniem jezdnym a lotniczym co
# kilka tikow - stad `pitch`/`roll`/`yaw` wysylane do auta stojacego na
# ziemi (z=17) i zmierzone "85-95% czasu w powietrzu" tuz przy pilce.
#
#: Ile tikow po utracie kontaktu nadal uznajemy auto za "na ziemi".
_ON_GROUND_GRACE_TICKS = 6
#: adres auta -> tiki od ostatniego kontaktu kol
_on_ground_ticks: dict = {}
_last_state_time: Optional[float] = None


def _tick_on_ground(car_addr: int, wheel_contact: bool, ticks: float) -> bool:
    """Odwzorowanie logiki z rlgym_compat. Zwraca on_ground z tolerancja."""
    if wheel_contact:
        _on_ground_ticks[car_addr] = 0.0
        return True
    _on_ground_ticks[car_addr] = _on_ground_ticks.get(car_addr, 1e9) + ticks
    return _on_ground_ticks[car_addr] <= _ON_GROUND_GRACE_TICKS


def _elapsed_game_ticks() -> float:
    """Tiki gry (120 Hz) od poprzedniego zbudowania stanu."""
    global _last_state_time
    now = time.perf_counter()
    if _last_state_time is None:
        _last_state_time = now
        return 1.0
    dt = now - _last_state_time
    _last_state_time = now
    # Ogranicz skok po pauzie/menu - inaczej jedna dziura wyzeruje tolerancje.
    return min(max(dt * 120.0, 0.0), 30.0)


def _player_to_shim(player: Player, car_id: int, ticks: float = 1.0) -> Optional[_PlayerShim]:
    """prism_sdk.Player -> _PlayerShim albo None gdy brak auta."""
    car = player.car
    if car is None:
        return None
    phys = _PhysicsShim(
        position         = car.physics.position,
        quaternion       = car.physics.quaternion,
        linear_velocity  = car.physics.linear_velocity,
        angular_velocity = car.physics.angular_velocity,
        rot_mtx          = car.physics.rotation_matrix,
    )
    return _PlayerShim(
        car_id       = car_id,
        team_num     = 0 if player.team_num < 0 else player.team_num,
        car_data     = phys,
        boost_amount = car.boost,
        # Z tolerancja 6 tikow - patrz komentarz przy _tick_on_ground.
        on_ground    = _tick_on_ground(car.address, car.is_on_ground, ticks),
        # DOKLADNIE jak rlgym_compat/game_state.py:59 - `not double_jumped`,
        # nic wiecej.
        #
        # Mielismy tu `car.has_flip`, ktore jest wlasna, BARDZIEJ precyzyjna
        # wersja: uwzglednia dodge component i okno 1.5 s od skoku (car.py:136).
        # Brzmi lepiej, ale jest bledem - Nexto nie byl na tym trenowany.
        # Sam autor rlgym_compat zostawil w tej linii komentarz, ze swiadomie
        # NIE sledzi timera flipa. Siec nauczyla sie rozkladu tej uproszczonej
        # cechy, wiec podawanie jej "dokladniejszej" przesuwa wejscie poza to,
        # co zna - a `has_flip` decyduje, czy planuje aerial czy jazde.
        has_flip     = not car.has_double_jumped,
        # rlgym_compat bierze to z pakietu; u nas timer respawnu > 0 = zdemolowany.
        is_demoed    = bool(getattr(car, "demo_respawn_timer", 0.0) > 0.0),
        has_jump     = not car.has_jumped,
        stats={
            "goals": player.match_goals, "saves": player.match_saves,
            "shots": player.match_shots, "assists": player.match_assists,
            "demolishes": player.match_demolishes,
        },
    )


def world_to_rlgym_state(world: World) -> _GameStateShim:
    """Konwertuje prism_sdk.World na rlgym-styled state (duck-type).

    Wystarczy dla oryginalnego CoyoteObs.build_obs() i nexto_v2_obs.
    """
    st = _GameStateShim()

    # Ball
    b = world.ball
    st.ball = _PhysicsShim(
        b.position, b.quaternion, b.linear_velocity, b.angular_velocity,
    )
    st.inverted_ball = st.ball.invert()

    # Players
    st.players = []
    ticks = _elapsed_game_ticks()
    for i, p in enumerate(world.players):
        shim = _player_to_shim(p, car_id=i, ticks=ticks)
        if shim is not None:
            st.players.append(shim)

    # Boost pads (34)
    st.boost_pads = _match_boost_pads_to_rlgym(world.boost_pads)
    st.inverted_boost_pads = st.boost_pads[::-1].copy()   # mirror = reverse indices

    # Scores
    if world.teams.get(0):
        st.blue_score   = world.teams[0].score
    if world.teams.get(1):
        st.orange_score = world.teams[1].score

    return st
