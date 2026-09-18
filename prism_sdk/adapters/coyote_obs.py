"""
Wrapper na oryginalny CoyoteObsBuilder.

Uzywa `_rlgym_shim` zeby oryginalny CoyoteObs.py (73 KB) dzialal bez modyfikacji
biorac prism_sdk.World jako input.

Deployed slicing (patrz bot_runner.py:99-114 i learner.py:88-95):
  * main tensor:  214 dim
  * cars tensor:  5 × 33 (drop `int(teammate)` i `demo_timer` z ostatnich 2 fields)
  * output:       91 logits

Wersje wagi Opti wymagaja tego dokladnego layoutu - inaczej model wypluje smieci.

Uzycie:
    from prism_sdk.adapters.coyote_obs import build_obs_from_world

    obs_main, obs_cars = build_obs_from_world(world, self_car_index=0)
    # obs_main.shape = (1, 214), obs_cars.shape = (1, 5, 33)
    with torch.no_grad():
        logits = opti_model((torch.tensor(obs_main), torch.tensor(obs_cars)))
    action_idx = int(logits.argmax(-1).item())
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional, Tuple

import numpy as np

# Wymus zeby Python bral CoyoteObs.py z NASZEGO Opti-main (nie zdublowanej sciezki)
_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Fake rlgym module - CoyoteObs.py importuje rlgym.utils.gamestates ale
# tylko dla type hints. Instalujemy pusty shim przed importem CoyoteObs.
import types
if "rlgym" not in sys.modules:
    _rlgym       = types.ModuleType("rlgym")
    _rlgym_utils = types.ModuleType("rlgym.utils")
    _rlgym_gs    = types.ModuleType("rlgym.utils.gamestates")
    _rlgym_gs.GameState  = object
    _rlgym_gs.PlayerData = object
    _rlgym_gs.PhysicsObject = object
    _rlgym_utils.gamestates = _rlgym_gs
    _rlgym.utils = _rlgym_utils
    sys.modules["rlgym"]                  = _rlgym
    sys.modules["rlgym.utils"]            = _rlgym_utils
    sys.modules["rlgym.utils.gamestates"] = _rlgym_gs
    # inne submoduly ktore CoyoteObs moze importowac
    _cv = types.ModuleType("rlgym.utils.common_values")
    # 34 boost pady RL - potrzebne dla CoyoteObs.inverted_boost_locations + add_boosts_to_obs
    _cv.BOOST_LOCATIONS = np.array([
        [    0.0, -4240.0, 70.0], [-1792.0, -4184.0, 70.0], [ 1792.0, -4184.0, 70.0],
        [-3072.0, -4096.0, 73.0], [ 3072.0, -4096.0, 73.0], [ -940.0, -3308.0, 70.0],
        [  940.0, -3308.0, 70.0], [    0.0, -2816.0, 70.0], [-3584.0, -2484.0, 70.0],
        [ 3584.0, -2484.0, 70.0], [-1788.0, -2300.0, 70.0], [ 1788.0, -2300.0, 70.0],
        [-2048.0, -1036.0, 70.0], [    0.0, -1024.0, 70.0], [ 2048.0, -1036.0, 70.0],
        [-3584.0,     0.0, 73.0], [-1024.0,     0.0, 70.0], [ 1024.0,     0.0, 70.0],
        [ 3584.0,     0.0, 73.0], [-2048.0,  1036.0, 70.0], [    0.0,  1024.0, 70.0],
        [ 2048.0,  1036.0, 70.0], [-1788.0,  2300.0, 70.0], [ 1788.0,  2300.0, 70.0],
        [-3584.0,  2484.0, 70.0], [ 3584.0,  2484.0, 70.0], [    0.0,  2816.0, 70.0],
        [ -940.0,  3308.0, 70.0], [  940.0,  3308.0, 70.0], [-3072.0,  4096.0, 73.0],
        [ 3072.0,  4096.0, 73.0], [-1792.0,  4184.0, 70.0], [ 1792.0,  4184.0, 70.0],
        [    0.0,  4240.0, 70.0],
    ], dtype=np.float32)
    _cv.BLUE_TEAM = 0
    _cv.ORANGE_TEAM = 1
    _cv.CEILING_Z = 2044
    _cv.SIDE_WALL_X = 4096
    _cv.BACK_WALL_Y = 5120
    _cv.BALL_RADIUS = 92.75
    sys.modules["rlgym.utils.common_values"] = _cv
    _obs_mod = types.ModuleType("rlgym.utils.obs_builders")
    class _ObsBuilder:
        def __init__(self, *a, **kw): pass
        def reset(self, initial_state): pass
        def pre_step(self, state): pass
        def build_obs(self, player, state, previous_action): return np.zeros(1)
    _obs_mod.ObsBuilder = _ObsBuilder
    sys.modules["rlgym.utils.obs_builders"] = _obs_mod
    # math helpers
    _math_mod = types.ModuleType("rlgym.utils.math")
    def _cosine_similarity(a, b):
        na = np.linalg.norm(a); nb = np.linalg.norm(b)
        if na == 0 or nb == 0: return 0.0
        return float(np.dot(a, b) / (na * nb))
    _math_mod.cosine_similarity = _cosine_similarity
    sys.modules["rlgym.utils.math"] = _math_mod

# numba shim - CoyoteObs ma @njit dla speed, ale nie jest wymagane semantycznie
if "numba" not in sys.modules:
    _numba = types.ModuleType("numba")
    def _njit(*args, **kwargs):
        # Uzycia: @njit i @njit(cache=True) - obsluz obie
        if args and callable(args[0]):
            return args[0]  # @njit z bezposrednim wywolaniem
        def wrap(fn): return fn
        return wrap
    _numba.njit = _njit
    _numba.jit  = _njit
    sys.modules["numba"] = _numba

# gym shim - CoyoteObs importuje gym.Space tylko dla type hint
if "gym" not in sys.modules:
    _gym = types.ModuleType("gym")
    class _Space:
        def __init__(self, *a, **kw): pass
    _gym.Space = _Space
    sys.modules["gym"] = _gym
    _gym_spaces = types.ModuleType("gym.spaces")
    _gym_spaces.Box = _Space
    _gym_spaces.Discrete = _Space
    _gym_spaces.Tuple = _Space
    _gym_spaces.Dict = _Space
    _gym_spaces.MultiBinary = _Space
    _gym_spaces.MultiDiscrete = _Space
    _gym.spaces = _gym_spaces
    sys.modules["gym.spaces"] = _gym_spaces

# Teraz mozna zaimportowac oryginalny CoyoteObs (jesli istnieje)
try:
    from CoyoteObs import CoyoteObsBuilder
    _HAS_COYOTE = True
except Exception as _e:
    _HAS_COYOTE = False
    _COYOTE_ERR = str(_e)
    CoyoteObsBuilder = None

from prism_sdk.world import World
from prism_sdk.adapters._rlgym_shim import world_to_rlgym_state, resolve_self_player_index


# ==================================================================
# Public API
# ==================================================================

_default_builder: Optional[Any] = None


def _get_builder(team_size: int = 3, tick_skip: int = 8,
                 expanding: bool = True, extra_boost_info: bool = True,
                 embed_players: bool = True) -> Any:
    """Lazy cache CoyoteObsBuilder z domyslnymi parametrami dla deployed Opti."""
    global _default_builder
    if not _HAS_COYOTE:
        raise RuntimeError(
            f"CoyoteObs.py nie ma sie zaimportowac ({_COYOTE_ERR}). "
            "Upewnij sie ze plik jest w root Opti-main."
        )
    if _default_builder is None:
        _default_builder = CoyoteObsBuilder(
            tick_skip=tick_skip, team_size=team_size,
            expanding=expanding, extra_boost_info=extra_boost_info,
            embed_players=embed_players,
        )
    return _default_builder


def build_obs_from_world(world: World, self_car_index: int,
                         previous_action: Optional[np.ndarray] = None,
                         team_size: int = 3) -> Tuple[np.ndarray, np.ndarray]:
    """Zbuduj tensor obserwacji dla wytrenowanego modelu Opti / Coyote-based.

    Args:
        world:            aktualny snapshot z prism_sdk
        self_car_index:   ktore auto (indeks w World.cars) obserwujemy
        previous_action:  poprzednia akcja 8-tuple (default: zera)
        team_size:        rozmiar druzyny (1/2/3 dla 1v1/2v2/3v3)

    Returns:
        (main, cars) - obs tensor dla Opti model.forward(inp).
        main.shape=(1, main_dim), cars.shape=(1, N_visible-1, 33 or 35)
    """
    builder = _get_builder(team_size=team_size)

    # Konwersja World -> rlgym shim
    state = world_to_rlgym_state(world)

    if not state.players:
        raise RuntimeError("brak graczy w world - jestes w menu?")

    # Self po TOZSAMOSCI (adres pawna) - self_car_index indeksuje World.cars,
    # a state.players idzie po World.players (rozne w meczu -> cudze auto = freeze).
    my_pi = resolve_self_player_index(world)
    self_player = None
    if my_pi is not None:
        self_player = next((p for p in state.players if p.car_id == my_pi), None)
    if self_player is None:
        # respawn/menu/spectator - nie da sie ustalic naszego auta.
        # OptiAgent.act() lapie ten wyjatek i wysyla neutral (nie steruje obcym).
        raise RuntimeError("brak lokalnego auta (respawn/menu) - suspend")

    if previous_action is None:
        previous_action = np.zeros(8, dtype=np.float32)

    # Reset gdy zmienila sie liczba graczy (CoyoteObs ma per-player buffery jak
    # demo_timers[N] ktore rosna z liczby graczy - zmiana wymaga reset).
    n_players = len(state.players)
    if getattr(builder, "_prism_n_players", None) != n_players:
        try:
            builder.reset(state)
        except Exception as _r:
            print(f"[coyote_obs] reset failed ({_r}) - kontynuuje")
        builder._prism_n_players = n_players
        builder.n = 0  # reset iteracji graczy

    # build_obs iteruje wewnetrznie po wszystkich playerach - musimy zresetowac n
    builder.n = 0

    obs = builder.build_obs(self_player, state, previous_action)
    return obs
