"""
BoostPad - jeden pad boostu na mapie. 34 pady w standardowej arenie:
  * 6 wielkich (100 boost, ~10s respawn)
  * 28 malych (12 boost, ~4s respawn)

Adres kazdego pada spawnowany jest przy starcie meczu i stabilny do jego konca -
resolwujemy raz przez GObjects i cachujemy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np

from prism_sdk.low.driver import Driver
from prism_sdk.low        import offsets as O
from prism_sdk.world._common import Vec3


@dataclass
class BoostPad:
    address:  int   = 0
    position: Vec3  = field(default_factory=lambda: np.zeros(3))
    is_big:   bool  = False
    is_active: bool = True         # dostepny do zebrania

    @classmethod
    def read(cls, drv: Driver, pad_addr: int) -> "BoostPad":
        if pad_addr == 0:
            return cls()

        # Pozycja z Actor.Location - pady sa nieruchome
        x = drv.read_f32(pad_addr + O.Actor.LOCATION + 0)
        y = drv.read_f32(pad_addr + O.Actor.LOCATION + 4)
        z = drv.read_f32(pad_addr + O.Actor.LOCATION + 8)

        boost_type = drv.read_u8(pad_addr + O.BoostPickup.BOOST_TYPE)
        is_big = boost_type == 1

        # PreviousPickedUpValue: parzysty = dostepny, nieparzysty = picked
        pv = drv.read_u8(pad_addr + O.VehiclePickup.PREVIOUS_PICKED_UP_VALUE)
        is_active = (pv % 2) == 0

        return cls(
            address=pad_addr,
            position=np.array([x, y, z], dtype=np.float64),
            is_big=is_big,
            is_active=is_active,
        )

    @classmethod
    def refresh_status(cls, drv: Driver, pad: "BoostPad") -> "BoostPad":
        """Szybki refresh tylko statusu (bez re-czytania pozycji/typu).
        Uzywane per-tick zeby minimalizowac IOCTL."""
        if pad.address == 0:
            return pad
        pv = drv.read_u8(pad.address + O.VehiclePickup.PREVIOUS_PICKED_UP_VALUE)
        return BoostPad(
            address=pad.address,
            position=pad.position,
            is_big=pad.is_big,
            is_active=(pv % 2) == 0,
        )
