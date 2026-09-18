"""
HookSender - wysyla akcje do RL przez shared memory 'RLBotHookShared'.

RLBotHook.dll (wstrzykniety przez manual_map.py) hookuje SetVehicleInput.
Nasz sender pisze do 36-bajtowej struktury: magic + enabled + 5 osi (float)
+ 3 bity. Hook czyta to i wpisuje do FVehicleInputs w kazdej klatce fizyki.

Krytyczne: DLL musi byc juz wstrzykniety - HookSender sprawdza magic 'RBOT'
przed startem. Jesli brak, rzuca zeby user wiedzial ze zapomnial manual_map.py.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from prism_sdk.control.controller_state import ControllerState

# hook_dll/ zawiera shared_input.py z SharedInputWriter (skopiowany z Desktop\RLBOT\)
_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK_DIR = os.path.join(_HERE, "hook_dll")
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)


class HookSender:
    """Sender ktory pisze do RLBotHook shared memory.

    Wolane per tick przez Manager: `sender(car_index, controller_state)`.
    """

    def __init__(self, wait_for_dll_seconds: float = 3.0):
        from shared_input import SharedInputWriter, MAGIC
        self._MAGIC   = MAGIC
        self._writer  = SharedInputWriter()

        # Poll do 3s zeby DLL zdazyl utworzyc region i wpisac magic
        import time
        deadline = time.time() + wait_for_dll_seconds
        magic = 0
        while time.time() < deadline:
            magic = self._writer.read_raw()[0]
            if magic == MAGIC:
                break
            time.sleep(0.1)

        if magic != MAGIC:
            self._writer.close()
            raise RuntimeError(
                f"Region 'RLBotHookShared' ma magic=0x{magic:08X} zamiast 0x{MAGIC:08X}.\n"
                "DLL RLBotHook nie jest wstrzykniety. Odpal najpierw:\n"
                "  python manual_map.py"
            )

    def __call__(self, car_index: int, cs: ControllerState) -> None:
        """Manager sender API - (car_index, ControllerState) -> None."""
        # HookSender ignoruje car_index (RLBotHook hookuje wszystko dla PID gry).
        # Jesli chcesz wybrac konkretne auto, musisz mieć rożne shared memory
        # regions per car - obecnie hook obsluguje jedno auto na raz.
        try:
            self._writer.write(
                enabled=True,
                throttle=cs.throttle,
                steer=cs.steer,
                pitch=cs.pitch,
                yaw=cs.yaw,
                roll=cs.roll,
                jump=cs.jump,
                boost=cs.boost,
                handbrake=cs.handbrake,
            )
        except Exception:
            pass

    def release(self) -> None:
        """Zwolnij sterowanie (enabled=0) zeby hook przestal nadpisywac inputy."""
        try:
            self._writer.write(enabled=False)
        except Exception:
            pass

    def close(self) -> None:
        self.release()
        try:
            self._writer.close()
        except Exception:
            pass


def is_hook_alive() -> bool:
    """Szybki test: czy DLL RLBotHook jest wstrzykniety (magic w shared memory)?"""
    try:
        from shared_input import SharedInputWriter, MAGIC
        w = SharedInputWriter()
        magic = w.read_raw()[0]
        w.close()
        return magic == MAGIC
    except Exception:
        return False
