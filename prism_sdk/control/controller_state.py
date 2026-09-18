"""
ControllerState - 8-wymiarowe wejscie do RL, format zgodny z RLGym/Nexto.

Format zwracany przez botow w `Bot.act(world) -> ControllerState`. Sender
(kernel autowrite lub hook) mapuje na `FVehicleInputs` (offset 0x7E4 w Vehicle_TA).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Union


@dataclass
class ControllerState:
    """Stan kontrolera do wpisania w FVehicleInputs.

    Analog: throttle/steer/pitch/yaw/roll ∈ [-1, 1].
    Bity:   jump/boost/handbrake (bool).

    Znaki osi w powietrzu:
      pitch > 0 = nose UP
      yaw   > 0 = nose RIGHT
      roll  > 0 = right side DOWN
    (potwierdzone w bocie referencyjnym).
    """
    throttle:  float = 0.0
    steer:     float = 0.0
    pitch:     float = 0.0
    yaw:       float = 0.0
    roll:      float = 0.0
    jump:      bool  = False
    boost:     bool  = False
    handbrake: bool  = False

    # ---- konstruktory pomocnicze ----

    @classmethod
    def neutral(cls) -> "ControllerState":
        return cls()

    @classmethod
    def from_array(cls, arr: Iterable[Union[float, int, bool]]) -> "ControllerState":
        """Nexto zwraca [thr, str, pit, yaw, rol, jmp, bst, hb]."""
        v = list(arr)
        if len(v) != 8:
            raise ValueError(f"ControllerState wymaga 8 elementow, dostal {len(v)}")
        return cls(
            throttle  = float(v[0]),
            steer     = float(v[1]),
            pitch     = float(v[2]),
            yaw       = float(v[3]),
            roll      = float(v[4]),
            jump      = bool(v[5] > 0.5),
            boost     = bool(v[6] > 0.5),
            handbrake = bool(v[7] > 0.5),
        )

    def to_array(self) -> list:
        return [self.throttle, self.steer, self.pitch, self.yaw, self.roll,
                float(self.jump), float(self.boost), float(self.handbrake)]

    def clamp(self) -> "ControllerState":
        """Ogranicz osie do [-1,1] - defensywnie, na wypadek gdyby bot pokazal 2.5."""
        def _c(x): return max(-1.0, min(1.0, x))
        return ControllerState(
            throttle  = _c(self.throttle),
            steer     = _c(self.steer),
            pitch     = _c(self.pitch),
            yaw       = _c(self.yaw),
            roll      = _c(self.roll),
            jump      = self.jump,
            boost     = self.boost,
            handbrake = self.handbrake,
        )

    def __repr__(self):
        parts = []
        if abs(self.throttle) > 0.01: parts.append(f"T{self.throttle:+.2f}")
        if abs(self.steer)    > 0.01: parts.append(f"S{self.steer:+.2f}")
        if abs(self.pitch)    > 0.01: parts.append(f"P{self.pitch:+.2f}")
        if abs(self.yaw)      > 0.01: parts.append(f"Y{self.yaw:+.2f}")
        if abs(self.roll)     > 0.01: parts.append(f"R{self.roll:+.2f}")
        if self.jump:      parts.append("JMP")
        if self.boost:     parts.append("BST")
        if self.handbrake: parts.append("HB")
        return "CS[" + (" ".join(parts) if parts else "-") + "]"
