"""
NextoAgent - wytrenowany Nexto v2 jako prism_sdk.agent.Agent.

Uzywa oryginalnego Nexto_V2_ObsBuilder (pretrained_agents/nexto/nexto_v2_obs.py)
przez _rlgym_shim - zero modyfikacji w kodzie obs buildera.

Uzycie:
    from prism_sdk.agent  import Manager
    from prism_sdk.world  import World
    from prism_sdk.adapters.nexto import NextoAgent

    world = World(); world.attach()
    mgr = Manager(world)
    mgr.bind(NextoAgent(), car_index=0)
    mgr.run(hz=15)   # Nexto trenowany na tick_skip=8 @ 120fps
"""

from __future__ import annotations

import copy
import os
import sys
from typing import Optional

import numpy as np
import torch
import torch.nn.functional as F

_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Ustaw rlgym shim przed importem nexto_v2_obs (patrz coyote_obs.py)
from prism_sdk.adapters import coyote_obs  # noqa: F401  side-effect: sys.modules['rlgym']

from pretrained_agents.nexto.nexto_v2_obs import Nexto_V2_ObsBuilder

from prism_sdk.agent   import Agent
from prism_sdk.control import ControllerState
from prism_sdk.adapters._rlgym_shim import world_to_rlgym_state, resolve_self_player_index


DEFAULT_MODEL_PATH = os.path.join(
    _HERE, "pretrained_agents", "nexto", "nexto-model.pt"
)


def _make_nexto_action_table() -> np.ndarray:
    """Skopiowane z pretrained_agents/nexto/nexto_v2.py - 8-action lookup."""
    actions = []
    for throttle in (-1, 0, 1):
        for steer in (-1, 0, 1):
            for boost in (0, 1):
                for handbrake in (0, 1):
                    if boost == 1 and throttle != 1:
                        continue
                    actions.append([throttle or boost, steer, 0, steer, 0, 0, boost, handbrake])
    for pitch in (-1, 0, 1):
        for yaw in (-1, 0, 1):
            for roll in (-1, 0, 1):
                for jump in (0, 1):
                    for boost in (0, 1):
                        if jump == 1 and yaw != 0:
                            continue
                        if pitch == roll == jump == 0:
                            continue
                        handbrake = int(jump == 1 and (pitch != 0 or yaw != 0 or roll != 0))
                        actions.append([boost, yaw, pitch, yaw, roll, jump, boost, handbrake])
    return np.array(actions, dtype=np.float32)


class NextoAgent(Agent):
    """Wytrenowany Nexto v2 (torch.jit) opakowany jako prism_sdk Agent."""

    name = "Nexto"

    #: Nexto trenowany z tick_skip=8 przy 120 fps = 15 decyzji/s. Czesciej
    #: != lepiej: akcja zaczyna migotac miedzy klatkami i psuje flipy.
    decision_hz = 15.0

    def __init__(self, model_path: str = DEFAULT_MODEL_PATH,
                 n_players: Optional[int] = None, device: str = "cpu"):
        super().__init__()
        self.device      = device
        self.model_path  = model_path
        self._actor      = torch.jit.load(model_path, map_location=device)
        self._actor.eval()
        self._obs_builder = Nexto_V2_ObsBuilder(n_players=n_players)
        self._lookup      = _make_nexto_action_table()
        self._previous    = np.zeros(8, dtype=np.float32)
        self._initialized = False
        #: Liczba graczy, dla ktorej obs_builder zostal ostatnio zresetowany.
        self._built_for_n = -1

    def _reset_if_needed(self, state):
        """Zresetuj obs_builder, gdy zmieni sie SKLAD meczu.

        Wczesniej byl tu zwykly `if not self._initialized` - reset odbywal sie
        RAZ i nigdy wiecej. To dzialalo tylko w meczu o stalym skladzie.
        W trybie "Soccar 3v3 +3bots" boty dolaczaja PO starcie: liczba graczy
        rosnie z 2 do 6 juz po tym, jak builder zostal zainicjowany na 2.
        Od tego momentu budowal obserwacje o zlym rozmiarze i siec dostawala
        belkot - bot gral sensownie przez pierwsze sekundy, a potem "jezdzil
        bez sensu".

        Sklad zmienia sie tez w druga strone: `world_to_rlgym_state` pomija
        graczy bez auta (respawn, spectator), wiec lista potrafi sie skrocic
        i wydluzyc w trakcie gry. Dlatego pilnujemy DLUGOSCI, a nie tylko
        pierwszego wywolania.
        """
        n = len(state.players)
        if self._initialized and n == self._built_for_n:
            return
        try:
            self._obs_builder.reset(state)
            if self._built_for_n != -1:
                print(f"[Prism] sklad zmienil sie {self._built_for_n} -> {n} "
                      f"graczy, przebudowuje obserwacje")
        except Exception as e:
            print(f"[Prism] reset obs_builder nie powiodl sie: {e}")
        self._built_for_n = n
        self._initialized = True

    def act(self) -> ControllerState:
        world = self._ctx.world
        state = world_to_rlgym_state(world)

        if not state.players:
            return ControllerState.neutral()

        self._reset_if_needed(state)

        # Nexto expects self player first, potem teammates, potem opps.
        # Selfa wybieramy po TOZSAMOSCI (adres pawna), nie po self_car_index -
        # ten ostatni indeksuje World.cars, a state.players idzie po World.players
        # (inna kolejnosc w meczu -> bralismy cudze auto = freeze).
        my_pi = resolve_self_player_index(world)
        me = None
        if my_pi is not None:
            me = next((p for p in state.players if p.car_id == my_pi), None)
        if me is None:
            return ControllerState.neutral()   # respawn/menu - trzymaj, nie steruj obcym
        team_mates = [p for p in state.players if p.team_num == me.team_num and p is not me]
        opps       = [p for p in state.players if p.team_num != me.team_num]

        # Ustaw kolejnosc graczy zgodnie z tym co Nexto oczekuje
        state.players = [me] + team_mates + opps

        obs = self._obs_builder.build_obs(me, state, self._previous)
        obs_t = tuple(torch.from_numpy(s).float().to(self.device) for s in obs)

        with torch.no_grad():
            out, _ = self._actor(obs_t)

        out = (out,)
        max_shape = max(o.shape[-1] for o in out)
        logits = torch.stack(
            [l if l.shape[-1] == max_shape
             else F.pad(l, pad=(0, max_shape - l.shape[-1]), value=float("-inf"))
             for l in out], dim=1,
        )
        action_idx = int(torch.argmax(logits, dim=-1).item())
        parsed = self._lookup[action_idx]
        self._previous = parsed.copy()

        return ControllerState.from_array(parsed)
