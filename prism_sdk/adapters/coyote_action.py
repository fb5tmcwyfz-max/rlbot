"""
CoyoteAction lookup table + parser (przepisane samodzielnie z CoyoteParser.py).

91 dyskretnych akcji, kazda mapuje na 8-tuple:
  [throttle, steer, pitch, yaw, roll, jump, boost, handbrake]

Ta sama semantyka jak oryginalny CoyoteAction("Normal") - dzieki temu wagi
Opti-main mozna uzywac bez retrenowania.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

from prism_sdk.control import ControllerState


def _make_lookup_table_normal() -> np.ndarray:
    """Odtwarza CoyoteAction("Normal") z CoyoteParser.py:
      * Ground: 4 throttle × 5 steer × 2 boost × 2 handbrake, z pominieciem
        wariantow gdzie boost=1 przy throttle!=1 (bo boost implikuje thr=1)
      * Aerial: 7 pitch × 7 yaw × 3 roll × 2 jump × 2 boost, z odfiltrowaniem:
          - jump=1 gdy yaw != 0 (tylko roll dla sideflip)
          - pitch=roll=jump=0 (duplikat ground)
      * + stall: [0, 1, 0, 0, -1, 1, 0, 0]
    """
    actions = []

    # Ground
    for throttle in (-1, 0, 0.5, 1):
        for steer in (-1, -0.5, 0, 0.5, 1):
            for boost in (0, 1):
                for handbrake in (0, 1):
                    if boost == 1 and throttle != 1:
                        continue
                    actions.append(
                        [throttle or boost, steer, 0, steer, 0, 0, boost, handbrake])

    # Aerial
    for pitch in (-1, -0.75, -0.5, 0, 0.5, 0.75, 1):
        for yaw in (-1, -0.75, -0.5, 0, 0.5, 0.75, 1):
            for roll in (-1, 0, 1):
                for jump in (0, 1):
                    for boost in (0, 1):
                        if jump == 1 and yaw != 0:
                            continue
                        if pitch == roll == jump == 0:
                            continue
                        handbrake = int(jump == 1 and (
                                pitch != 0 or yaw != 0 or roll != 0))
                        actions.append(
                            [boost, yaw, pitch, yaw, roll, jump, boost, handbrake])

    # Stall
    actions.append([0, 1, 0, 0, -1, 1, 0, 0])
    return np.array(actions, dtype=np.float32)


LOOKUP_TABLE = _make_lookup_table_normal()
ACTION_SPACE_SIZE = len(LOOKUP_TABLE)   # 373 dla "Normal" mode Coyote


class CoyoteActionParser:
    """Discrete action idx -> 8-dim tuple / ControllerState.

    Wolane po inferencji modelu (argmax logits -> idx). Bezstanowe.
    """

    def __init__(self):
        self.lookup_table = LOOKUP_TABLE

    def index_to_action(self, action_idx: int) -> np.ndarray:
        """Zwraca 8-elementowa np.ndarray."""
        if action_idx < 0 or action_idx >= len(self.lookup_table):
            return np.zeros(8, dtype=np.float32)
        return self.lookup_table[action_idx].copy()

    def index_to_controller(self, action_idx: int) -> ControllerState:
        """Zwraca ControllerState gotowy do wyslania do gry."""
        a = self.index_to_action(action_idx)
        return ControllerState.from_array(a)
