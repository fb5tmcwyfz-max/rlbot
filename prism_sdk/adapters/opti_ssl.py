"""
OptiSSLAgent - Opti SSL bot trenowany na RLGym 2.0 i socket do prism_sdk.

Uzywa ModernObsBuilder (na RLGym 2.0 GameState) + OptiSelector policy + AdvancedLookupTableAction parser.
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

# Ustaw rlgym shim PRZED importem obs buildera
from prism_sdk.adapters import coyote_obs  # noqa: F401

from prism_sdk.agent import Agent
from prism_sdk.control import ControllerState
from prism_sdk.adapters._rlgym_shim import world_to_rlgym_state, resolve_self_player_index

# Importy SSL komponentów
from prism_sdk.adapters.opti_ssl.model import OptiSelector
from prism_sdk.adapters.opti_ssl.obs import ModernObsBuilder
from prism_sdk.adapters.opti_ssl.action import ModernActionParser

DEFAULT_MODEL_PATH = os.path.join(
    _HERE, "prism_sdk", "adapters", "opti_ssl", "weights", "actor.pt"
)


class OptiSSLAgent(Agent):
    """Opti SSL bot - OptiSelector policy + ModernObsBuilder + AdvancedLookupTable actions."""

    name = "OptiSSL"
    decision_hz = 15.0  # tick_skip=8 @ 120 fps

    def __init__(self, model_path: str = DEFAULT_MODEL_PATH,
                 n_players: Optional[int] = None, device: str = "cpu"):
        super().__init__()
        self.device = device
        self.model_path = model_path
        self._device = torch.device(device)

        # Załaduj trained checkpoint (raw PyTorch state_dict)
        checkpoint = torch.load(model_path, map_location=device)

        # Buduj OptiSelector z checkpointu
        # actor.pt zawiera state_dict, wyodrębnij embedder + net
        self._build_opti_from_checkpoint(checkpoint)

        # Obs builder i action parser
        self._obs_builder = ModernObsBuilder(n_players=n_players)
        self._action_parser = ModernActionParser()
        self._previous = np.zeros(8, dtype=np.float32)

        self._initialized = False
        self._built_for_n = -1

    def _build_opti_from_checkpoint(self, checkpoint):
        """Załaduj OptiSelector z raw state_dict."""
        # Checkpoint zawiera bezpośrednio state_dict modelu
        # OptiSelector ma embedder i net - musimy je zrekonstruować

        # Try to load as JIT first
        try:
            self._policy = torch.jit.load(self.model_path, map_location=self.device)
            self._policy.eval()
            return
        except Exception:
            pass

        # Fallback: load state_dict na CPU i skonstruuj instancję
        # TODO: jeśli checkpoint zawiera OptiSelector, potrzebujemy info o dimach
        # Na razie zakładamy że jest w formacie który można załadować jako raw module

        # Placeholder - załaduj jako surowy modul
        try:
            self._policy = torch.load(self.model_path, map_location=self.device)
            if isinstance(self._policy, torch.nn.Module):
                self._policy.eval()
            else:
                raise ValueError(f"Expected nn.Module, got {type(self._policy)}")
        except Exception as e:
            raise RuntimeError(f"Failed to load Opti model from {self.model_path}: {e}")

    def _reset_if_needed(self, state):
        """Zresetuj obs_builder jeśli zmienił się sład meczu."""
        n_players = len(state.players) if hasattr(state, 'players') else 0
        if self._built_for_n != n_players:
            self._obs_builder._reset(n_players)
            self._built_for_n = n_players
            self._initialized = True

    def act(self) -> ControllerState:
        """Działanie - obs -> forward -> action -> ControllerState."""
        if not self._ctx:
            return ControllerState.neutral()

        world = self._ctx.world

        try:
            # Konwertuj world na rlgym.GameState (RLGym 2.0 format)
            state = world_to_rlgym_state(world)
            self._reset_if_needed(state)

            # Rozpoznaj siebie
            self_player_idx = resolve_self_player_index(world)
            if self_player_idx < 0:
                return ControllerState.neutral()

            # Buduj obs - ModernObsBuilder oczekuje split (main, cars)
            # TODO: jeśli ModernObsBuilder.build_obs zwraca tuple (main, cars), to OK
            # Jeśli zwraca single array, będzie błąd na forward pass
            obs = self._obs_builder.build_obs(
                agent_id=self_player_idx,
                state=state,
                shared_info=None  # TODO: tracking shared state between frames
            )

            # Konwertuj obs na tensor
            if isinstance(obs, tuple):
                main, cars = obs
                obs_t = (
                    torch.from_numpy(main).float().to(self._device).unsqueeze(0),
                    torch.from_numpy(cars).float().to(self._device).unsqueeze(0)
                )
            else:
                obs_t = torch.from_numpy(obs).float().to(self._device).unsqueeze(0)

            # Forward pass przez OptiSelector
            with torch.no_grad():
                if isinstance(obs_t, tuple):
                    # OptiSelector.forward((main, cars)) zwraca logits (tuple jeśli split_shape)
                    logits = self._policy(obs_t)
                else:
                    logits = self._policy(obs_t)

            # Dekoduj akcję - jeśli logits to tuple per skill, weź argmax per skill
            if isinstance(logits, (tuple, list)):
                # Multi-head output - weź argmax per head
                action_indices = np.array([int(lg[0].argmax(-1).cpu().numpy()) for lg in logits])
            else:
                # Single output
                action_indices = int(logits[0].argmax(-1).cpu().numpy())

            # Konwertuj action index na ControllerState przez action parser
            # ModernActionParser.get_action(action) zwraca np.array(8) lub ControllerState
            control_vec = self._action_parser.parse_actions(
                action_indices if isinstance(action_indices, np.ndarray) else np.array([action_indices])
            )

            if isinstance(control_vec, np.ndarray):
                return ControllerState.from_array(control_vec[0] if control_vec.ndim > 1 else control_vec)
            else:
                return control_vec

        except Exception as e:
            print(f"[!] OptiSSLAgent.act() error: {e}")
            import traceback
            traceback.print_exc()
            return ControllerState.neutral()

    def reset(self):
        """Zresetuj state po pauzie."""
        self._obs_builder._reset(self._built_for_n)
        self._previous = np.zeros(8, dtype=np.float32)
