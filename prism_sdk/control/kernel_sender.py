"""
KernelSender - pisze input auta przez PrismKernel kernel-side auto-writer.

Zero DLL, zero APC, zero user-mode hot loop.

Architektura:
    Python (Manager tick, 60Hz):
        __call__(car_index, cs) -> IOCTL_KWRITE_UPDATE(slot, new_bytes)
    Driver (system thread na PASSIVE_LEVEL):
        loop { for slot in active_slots: MmCopyVirtualMemory(target=game) }
        KeDelayExecutionThread(-1ms)  -> ~1000Hz overwrite rate

Efekt: Python uzywa ~0% CPU (jeden IOCTL na tick 60Hz = ~60us pracy CPU/s).
Kernel thread na 1000Hz to caly overhead - w praktyce niemierzalny (<0.1% CPU).

Per auto alokujemy 2 sloty:
    slot A = Vehicle_TA + 0x7E4 (INPUT FVehicleInputs 32B: 5xfloat + flags)
    slot B = Vehicle_TA + 0x804 (ReplicatedThrottle/Steer 2B u8)
"""

from __future__ import annotations

import time
from typing import Optional

from prism_sdk.control.controller_state import ControllerState
from prism_sdk.control.player_controller import PlayerControllerChannel, MODE_OVERRIDE
from prism_sdk.control.vehicle_inputs   import (pack_input as _pack_input,
                                                pack_replicated as _pack_replicated)
from prism_sdk.low.kwrite import KwriteHandle, KwriteError


# Offsety Vehicle_TA (patrz prism_sdk/low/offsets.py oraz SDK dump TAGame.txt)
OFF_INPUT_STRUCT   = 0x7E4    # FVehicleInputs (32B)
OFF_THROTTLE       = 0x00
OFF_STEER          = 0x04
OFF_PITCH          = 0x08
OFF_YAW            = 0x0C
OFF_ROLL           = 0x10
OFF_DODGE_FORWARD  = 0x14     # float [-1,1] - kierunek dodge (przod-tyl)
OFF_DODGE_RIGHT    = 0x18     # float [-1,1] - kierunek dodge (bok)
OFF_FLAGS          = 0x1C
FLAG_HANDBRAKE      = 0x01
FLAG_JUMP           = 0x02
FLAG_ACTIVATE_BOOST = 0x04
FLAG_HOLDING_BOOST  = 0x08
FLAG_JUMPED         = 0x10
FLAG_GRAB           = 0x20
FLAG_BUTTON_MASH    = 0x40
FLAG_AIR_ROLL       = 0x80

OFF_REPLICATED_THROTTLE = 0x804
OFF_REPLICATED_STEER    = 0x805

OFF_PLAYER_CONTROLLER = 0x810  # UPlayerController_TA* - jesli != NULL, PC kontroluje input

INPUT_BLOCK_SIZE = 0x20


class KernelSender:
    """Sender uzywajacy kernel-side auto-writer. Prawie zero CPU.

    pc_channel=True dokłada rownolegly zapis po stronie PlayerControllera
    (OverrideInput) - patrz prism_sdk/control/player_controller.py. Bez tego
    kanalu w multiplayer serwer dostaje input czlowieka, nie bota.
    """

    def __init__(self, world, verbose: bool = False,
                 pc_channel: bool = True, pc_mode: str = MODE_OVERRIDE):
        self.world = world
        self.verbose = verbose

        self._kw = KwriteHandle().open()
        self.pc = (PlayerControllerChannel(world, mode=pc_mode, verbose=verbose)
                   if pc_channel else None)

        # {car_index: (car_addr, input_slot_id, replicated_slot_id)}
        self._slots: dict[int, tuple[int, int, int]] = {}

        # Diagnostyka respawnu/menu
        self._no_cars_last_warn = 0.0
        self._no_cars_count = 0

    def _ensure_slots(self, car_index: int, car_addr: int, cs: ControllerState) -> Optional[tuple[int, int]]:
        """Zwroc (input_slot, rep_slot) - alokuj jesli brak, lub reallocuj gdy adres zmienil."""
        existing = self._slots.get(car_index)
        if existing is not None:
            existing_addr, i_slot, r_slot = existing
            if existing_addr == car_addr:
                return i_slot, r_slot
            # Adres zmienil - respawn/mode switch. Zwolnij stare, alokuj nowe.
            try:
                self._kw.stop(i_slot)
                self._kw.stop(r_slot)
            except KwriteError:
                pass
            del self._slots[car_index]

        try:
            pid = self.world.driver.pid
            i_slot = self._kw.start(pid, car_addr + OFF_INPUT_STRUCT,      _pack_input(cs))
            r_slot = self._kw.start(pid, car_addr + OFF_REPLICATED_THROTTLE, _pack_replicated(cs))
        except KwriteError as e:
            if self.verbose:
                print(f"[KernelSender] kwrite start failed: {e}")
            return None

        self._slots[car_index] = (car_addr, i_slot, r_slot)
        if self.verbose:
            print(f"[KernelSender] car[{car_index}] slots: input={i_slot} rep={r_slot} @ 0x{car_addr:X}")
        return i_slot, r_slot

    # -- Sender API --

    def __call__(self, car_index: int, cs: ControllerState) -> None:
        # Kanal PlayerControllera nie zalezy od world.cars - leci nawet gdy
        # skan aut jest chwilowo pusty (respawn), bo PC resolwuje sie sam.
        if self.pc is not None:
            self.pc.apply(cs)

        cars = getattr(self.world, "cars", None) or []
        if car_index >= len(cars):
            self._no_cars_count += 1
            if self.verbose and (time.perf_counter() - self._no_cars_last_warn) > 2.0:
                print(f"[KernelSender] world.cars pusty ({self._no_cars_count}) - respawn/menu")
                self._no_cars_last_warn = time.perf_counter()
                self._no_cars_count = 0
            return

        if self._no_cars_count > 0 and self.verbose:
            print(f"[KernelSender] cars powrocili ({len(cars)}), resume")
            self._no_cars_count = 0

        car_addr = cars[car_index].address
        if car_addr == 0:
            return

        slots = self._ensure_slots(car_index, car_addr, cs)
        if slots is None:
            return
        i_slot, r_slot = slots

        try:
            self._kw.update(i_slot, _pack_input(cs))
            self._kw.update(r_slot, _pack_replicated(cs))
        except KwriteError as e:
            if self.verbose:
                print(f"[KernelSender] update failed: {e}")

    # -- lifecycle --

    def close(self) -> None:
        """Wpisz neutral raz na kazde auto, potem stop wszystkich slotow."""
        if self.pc is not None:
            try:
                self.pc.release()
            except Exception:
                pass
        neutral_input = _pack_input(ControllerState.neutral())
        neutral_rep   = _pack_replicated(ControllerState.neutral())
        for car_index, (car_addr, i_slot, r_slot) in list(self._slots.items()):
            try:
                self._kw.update(i_slot, neutral_input)
                self._kw.update(r_slot, neutral_rep)
            except KwriteError:
                pass
        # Daj kernel thread'owi czas zapisac neutral przed stopem
        time.sleep(0.02)
        for car_index, (car_addr, i_slot, r_slot) in list(self._slots.items()):
            try:
                self._kw.stop(i_slot)
                self._kw.stop(r_slot)
            except KwriteError:
                pass
        self._slots.clear()
        self._kw.close()


def is_driver_alive(world) -> bool:
    try:
        return world.driver.attached and world.driver.module_base != 0
    except Exception:
        return False
