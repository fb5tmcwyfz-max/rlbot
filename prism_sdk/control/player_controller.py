"""
Kanal sterowania przez PlayerController_TA (obok FVehicleInputs w aucie).

PO CO, skoro kernel patch SetVehicleInput juz dziala?
---------------------------------------------------
Patch SetVehicleInput podmienia input DOPIERO w momencie aplikowania go do
fizyki auta - lokalnie auto jedzie idealnie. Ale w multiplayer to nie jedyne
miejsce ktore liczy sie: PlayerController.PlayerMove() zbiera input z pada do
`PC.VehicleInput` i wysyla go przez ProcessMove_TA do SERWERA. Serwer dostaje
wiec input CZLOWIEKA (neutral) i po swojej symulacji koryguje pozycje auta ->
rubber-banding / auto "wraca" mimo ze lokalnie jechalo dobrze.

Ten modul pisze po stronie PlayerControllera, zeby pakiet ruchu do serwera
niosl input BOTA. Dwa warianty (mode):

  "override"  (domyslny, bez race)
        PC.OverrideInput (0xB18) = nasz input
        PC.bOverrideInput (bit 0x4 w bitfieldzie 0x9D0) = 1
        Gra sama wstawia OverrideInput w PlayerMove. To wartosc TRWALA -
        nie scigamy sie z game logic, wystarczy trzymac ja aktualna.

  "direct"    (race, ale nie wymaga zeby gra honorowala bOverrideInput)
        PC.VehicleInput (0x9B0) = nasz input, nadpisywane co klatke przez
        PlayerMove - wygrywamy tylko gdy trafimy w okno miedzy zebraniem
        inputu a ProcessMove_TA. Traktowac jako fallback / material do testow.

  "both"      oba naraz.

UWAGA: czy gra faktycznie honoruje bOverrideInput w danym buildzie sprawdza
`python diag_pc.py --test override` (pisze throttle=1 i patrzy czy auto ruszy).

Kanal jest komplementarny do patcha, nie zastepuje go:
    patch  -> lokalna fizyka (pewne, zero race)
    PC     -> to co leci do serwera (replikacja)
"""

from __future__ import annotations

import time
from typing import Optional

from prism_sdk.control.controller_state import ControllerState
from prism_sdk.control.vehicle_inputs   import pack_input, unpack_input, reset_edge_state
from prism_sdk.low import offsets as O
from prism_sdk.world.local import resolve_local_chain


PC = O.PlayerController

MODE_OVERRIDE = "override"
MODE_DIRECT   = "direct"
MODE_BOTH     = "both"
MODES = (MODE_OVERRIDE, MODE_DIRECT, MODE_BOTH)

# Bity ktorymi ZARZADZAMY w bajcie 0x9D0 (reszta - m.in. bHasPitchedOrRolled
# i bAirPitchSafetyEnabled - nalezy do gry i musi przetrwac nasz zapis).
_BIT_OVERRIDE  = PC.FLAG_OVERRIDE_INPUT
_BIT_JUMP      = PC.FLAG_JUMP_PRESSED
_BIT_BOOST     = PC.FLAG_BOOST_PRESSED
_BIT_HANDBRAKE = PC.FLAG_HANDBRAKE_PRESSED

_NEUTRAL_INPUT = pack_input(ControllerState.neutral())


class PlayerControllerChannel:
    """Pisze input bota do PlayerController_TA lokalnego gracza.

    Uzycie (samodzielnie albo obok PatchSendera):
        ch = PlayerControllerChannel(world)
        ch.apply(cs)          # co tick
        ch.release()          # oddaje sterowanie czlowiekowi
    """

    def __init__(self, world, mode: str = MODE_OVERRIDE,
                 press_bits: bool = True, verbose: bool = False):
        if mode not in MODES:
            raise ValueError(f"mode musi byc jednym z {MODES}, dostal '{mode}'")
        self.world      = world
        self.mode       = mode
        self.press_bits = press_bits
        self.verbose    = verbose
        self._pc        = 0        # ostatni znany PlayerController
        self._last_warn = 0.0
        self._writes    = 0
        self._flag_resets = 0      # ile razy gra sama skasowala bOverrideInput

    # -- pomocnicze --

    def _managed_mask(self) -> int:
        m = _BIT_OVERRIDE if self.mode in (MODE_OVERRIDE, MODE_BOTH) else 0
        if self.press_bits:
            m |= _BIT_JUMP | _BIT_BOOST | _BIT_HANDBRAKE
        return m

    def _write_flags(self, pc: int, want: int) -> None:
        """RMW jednego bajtu 0x9D0 - zachowuje bity gry, ustawia nasze."""
        mask = self._managed_mask()
        if mask == 0:
            return
        drv = self.world.driver
        cur = drv.read_bytes(pc + PC.INPUT_FLAGS, 1)
        if not cur:
            return
        cur_b = cur[0]
        new_b = (cur_b & ~mask) | (want & mask)
        if new_b != cur_b:
            # Gra skasowala nasz bOverrideInput miedzy tickami? (consume-once
            # semantyka w PlayerMove) - liczymy, bo to zmienia interpretacje
            # tego czy kanal dziala.
            if (self._writes > 0 and (want & _BIT_OVERRIDE)
                    and not (cur_b & _BIT_OVERRIDE)):
                self._flag_resets += 1
            drv.write_bytes(pc + PC.INPUT_FLAGS, bytes([new_b]))

    # -- API --

    def resolve(self) -> int:
        """Aktualny PlayerController lokalnego gracza (0 = menu / brak)."""
        return resolve_local_chain(self.world).pc

    def apply(self, cs: ControllerState, pc: int = 0) -> bool:
        """Wpisz input do PC. `pc` mozna podac gdy wolajacy juz go zresolwowal.

        Zwraca True gdy cokolwiek poszlo do gry.
        """
        if pc == 0:
            pc = self.resolve()
        if pc == 0:
            # menu / brak lokalnego gracza - nie zostawiaj wiszacego override
            if self._pc:
                self._clear(self._pc)
                self._pc = 0
            if self.verbose and (time.perf_counter() - self._last_warn) > 2.0:
                print("[PCChannel] brak lokalnego PlayerControllera (menu?)")
                self._last_warn = time.perf_counter()
            return False

        if pc != self._pc:
            if self._pc:
                self._clear(self._pc)     # stary PC - zdejmij override
            if self.verbose:
                print(f"[PCChannel] PlayerController -> 0x{pc:X} (mode={self.mode})")
            self._pc = pc

        drv  = self.world.driver
        data = pack_input(cs)

        if self.mode in (MODE_OVERRIDE, MODE_BOTH):
            drv.write_bytes(pc + PC.OVERRIDE_INPUT, data)
        if self.mode in (MODE_DIRECT, MODE_BOTH):
            drv.write_bytes(pc + PC.VEHICLE_INPUT, data)

        want = _BIT_OVERRIDE
        if cs.jump:      want |= _BIT_JUMP
        if cs.boost:     want |= _BIT_BOOST
        if cs.handbrake: want |= _BIT_HANDBRAKE
        self._write_flags(pc, want)

        self._writes += 1
        return True

    def _clear(self, pc: int) -> None:
        """Zdejmij override z konkretnego PC i wyzeruj OverrideInput.

        Kasujemy tez bJump/bBoost/bHandbrakePressed. Te bity sa krawedziowe
        (gra ustawia je na key-down, kasuje na key-up), wiec zostawienie
        naszej "1" bez pasujacego key-up moglo by zablokowac np. boost na
        stale. Koszt: jesli czlowiek TRZYMAL boost w chwili oddania sterowania,
        musi puscic i wcisnac ponownie. Zaciecie w pozycji "puszczone" jest
        bezpieczniejsze niz w "wcisniete".

        Zerujemy OBA bufory, nie tylko OVERRIDE_INPUT. `apply()` w trybie
        `both` (a tak lata bot) pisze rowniez do VEHICLE_INPUT, i to z niego
        gra sklada pakiet ruchu do serwera. Zostawienie tam throttle=1.0
        sprawialo, ze po zatrzymaniu bota auto dalej jechalo do przodu.
        Zapis neutralnego inputu do bufora, ktorego akurat nie uzywamy, jest
        nieszkodliwy - gra i tak nadpisuje go z prawdziwej klawiatury co tick.

        WAZNE (BSOD guard): NIE piszemy do PC ktory nie jest juz w LocalPlayers
        chain. Manager przy wejsciu w menu / replay wola `release()` z ostatnim
        znanym `self._pc`, a ten obiekt w tym momencie moze byc juz zwolniony
        w grze. PK_IOCTL_WRITE przechodzi bez whitelisty, wiec sterownik robi
        MmCopyVirtualMemory pod freed VA i gra pada. Analogiczna defensywa
        siedzi w PatchSender (patrz kernel_hook.py: "any(c.address == addr
        for c in self.world.cars)"). Tu weryfikujemy przez swiezy
        resolve_local_chain: jesli aktualny PC != argument, znaczy ze albo
        stary PC juz nie zyje, albo pisalibysmy do cudzego PC (multiplayer
        respawn) - w obu przypadkach lepiej odpuscic.
        """
        try:
            fresh = resolve_local_chain(self.world).pc
        except Exception:
            fresh = 0
        if fresh != pc:
            # PC zwolniony / zmieniony (menu, respawn, spectator) - nic nie
            # pisz. Gra sama nadpisze OverrideInput z prawdziwej klawiatury,
            # jak wrocimy do mecza to nowy `apply()` na swieżym PC ustawi
            # wszystko od zera.
            return
        try:
            self._write_flags(pc, 0)
            drv = self.world.driver
            drv.write_bytes(pc + PC.OVERRIDE_INPUT, _NEUTRAL_INPUT)
            drv.write_bytes(pc + PC.VEHICLE_INPUT, _NEUTRAL_INPUT)
        except Exception:
            pass

    def release(self) -> None:
        """Oddaj sterowanie czlowiekowi (bOverrideInput=0, OverrideInput=neutral)."""
        if self._pc:
            self._clear(self._pc)
            if self.verbose:
                print(f"[PCChannel] release (zapisow: {self._writes}, "
                      f"gra kasowala bOverrideInput {self._flag_resets} razy)")
        self._pc = 0
        # Wyzerowanie pamieci edge - nastepny apply() ma zaczac od czystej
        # kartki (JUMPED edge musi wypasc na PIERWSZYM naszym tiku po pauzie,
        # nie po drugim).
        reset_edge_state()

    def neutralize(self) -> bool:
        """Wyzeruj input w grze, nawet jesli TEN obiekt nic tam nie pisal.

        `release()` sprzata po sobie - czysci PlayerController, ktory sam
        wczesniej znalazl. Nie ma go komu wywolac, gdy proces bota zostal
        UBITY: jego `finally` nigdy nie polecialo, a w pamieci gry siedzi
        ostatni input, z gazem wlacznie, i auto jedzie dalej po zatrzymaniu.
        Ta metoda jest wejsciem dla osobnego, krotkiego przebiegu, ktory ma
        tylko posprzatac po takim procesie.

        Zwraca False, gdy nie ma czego czyscic (menu / brak lokalnego gracza).
        """
        pc = self.resolve()
        if pc == 0:
            return False
        self._clear(pc)
        self._pc = 0
        return True

    # -- diagnostyka --

    @property
    def writes(self) -> int:
        """Ile razy wpisalismy input do PC."""
        return self._writes

    @property
    def flag_resets(self) -> int:
        """Ile razy gra sama skasowala bOverrideInput miedzy naszymi zapisami.
        Duza wartosc = gra konsumuje flage co klatke (spodziewane, gdy kanal
        jest honorowany); zero przy braku ruchu = flaga jest ignorowana."""
        return self._flag_resets

    def snapshot(self, pc: int = 0) -> Optional[dict]:
        """Odczyt stanu inputu po stronie PC. None gdy brak PC."""
        if pc == 0:
            pc = self.resolve()
        if pc == 0:
            return None
        drv = self.world.driver
        flags = drv.read_u32(pc + PC.INPUT_FLAGS)
        return {
            "pc":              pc,
            "car":             drv.read_ptr(pc + PC.CAR),
            "vehicle_input":   unpack_input(drv.read_bytes(pc + PC.VEHICLE_INPUT, 0x20)),
            "override_input":  unpack_input(drv.read_bytes(pc + PC.OVERRIDE_INPUT, 0x20)),
            "last_inputs":     unpack_input(drv.read_bytes(pc + PC.LAST_INPUTS, 0x20)),
            "b_override":      bool(flags & _BIT_OVERRIDE),
            "b_jump":          bool(flags & _BIT_JUMP),
            "b_boost":         bool(flags & _BIT_BOOST),
            "b_handbrake":     bool(flags & _BIT_HANDBRAKE),
            "flags_raw":       flags,
        }


class PCSender:
    """Sender dla Managera uzywajacy WYLACZNIE PlayerControllera.

    Bez kernel patcha - nic nie modyfikuje kodu gry, tylko dane. Czy to
    wystarczy do sterowania autem zalezy od tego czy build honoruje
    bOverrideInput (sprawdz `python diag_pc.py --test override`).
    Do normalnej gry uzywaj PatchSendera (patch + ten kanal razem).
    """

    def __init__(self, world, mode: str = MODE_OVERRIDE, verbose: bool = False):
        self.channel = PlayerControllerChannel(world, mode=mode, verbose=verbose)

    def __call__(self, car_index: int, cs: ControllerState) -> None:
        # car_index ignorowany - PC jest z definicji nasz jeden lokalny gracz
        self.channel.apply(cs)

    def close(self) -> None:
        self.channel.release()
