"""
KumoriAgent - wytrenowany Kumori (RLGym-PPO / GigaLearn) jako prism_sdk.agent.Agent.

Kumori vs Nexto:
  * Nexto: JEDEN plik torch.jit (nexto-model.pt) z multi-head outputem.
  * Kumori: DWA pliki torch.jit - SHARED_HEAD.lt (backbone, obs 237 -> 1024)
    i POLICY.lt (1024 -> 90 dyskretnych akcji). Kopia .lt do .pt nie jest
    potrzebna, torch.jit.load laduje po zawartosci.
  * Obs: AdvancedObsPadder (237 floats, team_size=3). Pola ktorych uzywa
    (ball, boost pads, player.car_data.forward/up/vel/av, boost_amount,
    on_ground, has_flip, is_demoed, has_jump) pokrywaja sie 1:1 z tym co
    _rlgym_shim._PlayerShim/_PhysicsShim/_GameStateShim udostepnia.
  * Akcje: LookupAction (90 wpisow) - argmax po logitach jest niezaleznie
    softmaxu (softmax jest monotoniczny), wiec pominecie ostatniego softmax'a
    (POLICY.lt konczy sie na Linear, nie Softmax) nie zmienia decyzji.

WAZNE historyczne: oryginalny discrete_policy.py z paczki Kumori mial
nn.ReLU() zamiast nn.LeakyReLU(). Wagi trenowane sa z LeakyReLU
(potwierdzone porownaniem z TorchScriptem: ReLU->76.67% zgodnosci akcji,
LeakyReLU->100%). Kopia tutaj ma juz poprawka. Nie uzywamy tez rebuild-tu
przez DiscreteFF - bierzemy TorchScript wprost, bez ryzyka niespojnosci.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import numpy as np
import torch

_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Musi byc PRZED importem obs Kumori (podobnie jak w nexto adapterze).
from prism_sdk.adapters import coyote_obs  # noqa: F401

from pretrained_agents.kumori.obs       import AdvancedObsPadder
from pretrained_agents.kumori.your_act  import LookupAction

from prism_sdk.agent   import Agent
from prism_sdk.control import ControllerState
from prism_sdk.adapters._rlgym_shim import world_to_rlgym_state, resolve_self_player_index


_KUMORI_DIR = os.path.join(_HERE, "pretrained_agents", "kumori")
DEFAULT_SHARED_PATH = os.path.join(_KUMORI_DIR, "SHARED_HEAD.lt")
DEFAULT_POLICY_PATH = os.path.join(_KUMORI_DIR, "POLICY.lt")


class KumoriAgent(Agent):
    """Kumori (RLGym-PPO, 237-obs, 90-action lookup) jako prism_sdk Agent."""

    name = "Kumori"

    #: RLGym-PPO trenowany z tick_skip=8 przy 120 fps = 15 decyzji/s.
    #: Manager i tak wysyla ostatnia akcje do gry co tick - patrz
    #: prism_sdk/agent/base.py:Agent.decision_hz.
    decision_hz = 15.0

    def __init__(self, shared_path: str = DEFAULT_SHARED_PATH,
                 policy_path: str = DEFAULT_POLICY_PATH,
                 team_size: int = 3, device: str = "cpu"):
        super().__init__()
        self.device        = device
        self._shared       = torch.jit.load(shared_path, map_location=device)
        self._policy       = torch.jit.load(policy_path, map_location=device)
        self._shared.eval(); self._policy.eval()
        self._obs_builder  = AdvancedObsPadder(team_size=team_size)
        self._lookup       = LookupAction()._lookup_table.astype(np.float32)
        self._previous     = np.zeros(8, dtype=np.float32)
        torch.set_num_threads(1)

    def act(self) -> ControllerState:
        world = self._ctx.world
        state = world_to_rlgym_state(world)

        if not state.players:
            return ControllerState.neutral()

        # Znajdz "nas" po adresie pawna (tak jak NextoAgent). Kolejnosc graczy
        # w state.players zalezy od world.players, ktore nie musi zaczynac sie
        # od nas. Kumori ObsBuilder wybiera "self" po `player is other`, wiec
        # przekazanie zlego obiektu = obserwacje z perspektywy cudzego auta.
        my_pi = resolve_self_player_index(world)
        me = None
        if my_pi is not None:
            me = next((p for p in state.players if p.car_id == my_pi), None)
        if me is None:
            return ControllerState.neutral()

        obs = self._obs_builder.build_obs(me, state, self._previous)
        obs_t = torch.from_numpy(np.asarray(obs, dtype=np.float32)).to(self.device)

        with torch.no_grad():
            logits = self._policy(self._shared(obs_t))
        action_idx = int(torch.argmax(logits.view(-1)).item())

        parsed = self._lookup[action_idx].copy()
        self._previous = parsed
        return ControllerState.from_array(parsed)
