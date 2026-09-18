"""Warstwa kontroli - wysylanie inputow do gry.

Dwa MIEJSCA w pamieci gry, do ktorych mozna pisac input:

  1. Vehicle_TA + 0x7E4 (FVehicleInputs) - input zaaplikowany do fizyki auta.
     Uzywaja: HookSender (DLL), KernelSender (kwrite), KernelPatchHook (patch).
  2. PlayerController_TA (VehicleInput / OverrideInput) - input ktory gra
     wysyla w pakiecie ruchu do serwera. Uzywa: PlayerControllerChannel.
     W multiplayer bez (2) serwer widzi input czlowieka i koryguje auto.

Senders (Manager API: sender(car_index, ControllerState)):
- HookSender:   przez RLBotHook.dll wstrzykniete do RL (shared memory).
- KernelSender: DIRECT write do FVehicleInputs przez PrismKernel driver (+PC).
- PatchSender:  kernel patch SetVehicleInput (+PC) - najpewniejszy, zero race.
- PCSender:     sam PlayerController, bez patchowania kodu gry.
"""

from prism_sdk.control.controller_state import ControllerState
from prism_sdk.control.vehicle_inputs    import (pack_input, pack_replicated,
                                                 unpack_input, f_to_u8)
from prism_sdk.control.hook_sender      import HookSender, is_hook_alive
from prism_sdk.control.player_controller import (PlayerControllerChannel, PCSender,
                                                 MODE_OVERRIDE, MODE_DIRECT,
                                                 MODE_BOTH, MODES)
from prism_sdk.control.kernel_sender    import KernelSender, is_driver_alive
from prism_sdk.control.kernel_hook      import PatchSender, KernelPatchHook
