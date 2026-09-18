"""
FVehicleInputs (0x20 B) - pakowanie / rozpakowanie.

Jedno miejsce dla WSZYSTKICH kanalow sterowania (kernel patch SetVehicleInput,
kwrite auto-writer, PlayerController). Wczesniej kazdy sender mial wlasna kopie
_pack_input() - przy trzech kanalach dzialajacych rownolegle rozjechanie sie
tych kopii dawaloby auto z dwoma roznymi inputami naraz.

Layout (sdk_dump/TAGame.txt -> ScriptStruct TAGame._Types_TA.VehicleInputs):
    0x00 float Throttle      0x14 float DodgeForward
    0x04 float Steer         0x18 float DodgeRight
    0x08 float Pitch         0x1C uint8 bity (Handbrake/Jump/Boost/...)
    0x0C float Yaw
    0x10 float Roll
"""

from __future__ import annotations

import os
import struct

from prism_sdk.control.controller_state import ControllerState
from prism_sdk.low import offsets as O


SIZE = O.VehicleInputs.SIZE   # 0x20

# Pamiec ostatniej klatki na potrzeby generowania krawedzi bitow.
# `pack_input` jest stateless; edge dla JUMPED (0x10 - "just pressed jump")
# wymaga porownania z tikiem poprzednim. Trzymamy tylko booleana skoku, bo
# tylko on generuje edge sygnal w BUTTONS. Reset - patrz `reset_edge_state`.
_prev_jump = False

#: Czy dopisywac BIT_HOLDING_BOOST (0x08) do BUTTONS.
#:   PRISM_BOOST_HOLD=1  -> tak (stare zachowanie, zwykle lepsze ONLINE)
#:   PRISM_BOOST_HOLD=0  -> nie (zgodne z RLBotem, ktory 0x08 nie uzywa)
#: Pomiar z dzialajacego RLBota: on ustawia wylacznie 0x01/0x02/0x04.
#: DOMYSLNIE WLACZONE. Pomiar z RLBota pokazal, ze on 0x08 nie uzywa, ale
#: RLBot pisze input WCZESNIEJ w potoku - gra zdazy ustawic sobie ten bit
#: sama. Nasz patch nadpisuje cale 32 bajty przy wejsciu do SetVehicleInput,
#: wiec kasuje go zaraz po tym, jak gra go ustawila. Objaw przy 0x08=off:
#: boost sie aktywuje, ale auto nie przyspiesza (nie "trzyma" boosta).
_BOOST_HOLD = os.environ.get("PRISM_BOOST_HOLD", "1").strip() not in ("0", "", "false")


def reset_edge_state() -> None:
    """Zresetuj pamiec edge (np. po oddaniu sterowania czlowiekowi)."""
    global _prev_jump
    _prev_jump = False


def f_to_u8(v: float) -> int:
    """[-1.0, 1.0] -> [0, 255], 128 = neutral (format ReplicatedThrottle/Steer)."""
    if v > 1.0: v = 1.0
    if v < -1.0: v = -1.0
    return max(0, min(255, int(round(v * 127.0)) + 128))


def pack_input(cs: ControllerState) -> bytes:
    """ControllerState -> 32B FVehicleInputs."""
    V = O.VehicleInputs
    buf = bytearray(SIZE)
    struct.pack_into("<f", buf, V.THROTTLE, cs.throttle)
    struct.pack_into("<f", buf, V.STEER,    cs.steer)
    struct.pack_into("<f", buf, V.PITCH,    cs.pitch)
    struct.pack_into("<f", buf, V.YAW,      cs.yaw)
    struct.pack_into("<f", buf, V.ROLL,     cs.roll)
    # DodgeForward/Right - kierunek flipa, czytany przez gre TYLKO w momencie
    # dodge'a (drugi skok). Dlatego wypelniamy je wylacznie gdy trzymamy jump.
    #
    # Wczesniej szly tam `-pitch` i `yaw` w KAZDEJ klatce. Objaw byl taki, ze
    # auto przemieszczalo sie w okregu z NIERUCHOMYM kadlubem - czyli stall:
    # w Rocket League przeciwstawny yaw i roll w powietrzu zatrzymuja obrot
    # i auto szybuje lukiem. Nexto regularnie wybiera kombinacje typu
    # yaw=+1 / roll=-1, wiec staly zapis kierunku dodge'a trzymal auto w tym
    # stanie bez przerwy i zaden aerial nie mogl sie udac.
    #
    # `+ 0.0` normalizuje -0.0 -> 0.0: przy pitch=0 negacja dawala 0x80000000,
    # przez co "neutral" nie byl bajtowo rowny zerom (mylace przy porownywaniu
    # zrzutow pamieci i przy restore stanu po release()).
    # Test 2026-08-22: wyzerowanie DodgeForward/DodgeRight DAWALO GORSZY
    # kickoff niz wpisywanie ich z pitch/yaw. Wniosek: gra nie liczy sobie
    # dodge kierunku samodzielnie z aktualnego pitch/yaw - te dwa pola sa
    # ODCZYTYWANE bezposrednio z FVehicleInputs w momencie drugiego skoku
    # i musza byc wypelnione, inaczej dodge leci bez kierunku.
    #
    # Wracamy do `-pitch / yaw`. Nadal to nie jest to co robi RLBot przez
    # SDK Psyonixa, ale jest zauwazalnie blizej.
    if cs.jump:
        struct.pack_into("<f", buf, V.DODGE_FORWARD, -cs.pitch + 0.0)
        struct.pack_into("<f", buf, V.DODGE_RIGHT,    cs.yaw + 0.0)
    else:
        struct.pack_into("<f", buf, V.DODGE_FORWARD, 0.0)
        struct.pack_into("<f", buf, V.DODGE_RIGHT,   0.0)
    # BUTTONS - wracamy do bazowych czterech bitow.
    #
    # Test 2026-08-22: dodanie JUMPED (0x10), BUTTON_MASH (0x40) i AIR_ROLL
    # (0x80) po kolei ani razem NIC nie zmienilo na kickoffie. Trzy bity
    # obciazaja input bez efektu, wiec je zdejmujemy. Kickoff Nexto z
    # `PRISM_KICKOFF=table` na patch synchronicznym w rezultacie zachowuje
    # sie identycznie jak z tymi bitami - roznica z RLBotem musi siedziec
    # gdzie indziej, poza mapowaniem `ControllerState -> FVehicleInputs`.
    global _prev_jump
    _prev_jump = cs.jump         # nadal trackujemy, gdyby cos chcialo edge

    flags = 0
    if cs.handbrake: flags |= V.BIT_HANDBRAKE
    if cs.jump:      flags |= V.BIT_JUMP
    # TYLKO ACTIVATE_BOOST - bez HOLDING_BOOST (0x08).
    #
    # Zmierzone 2026-08-22 na dzialajacym RLBocie (rlbot_ref.csv, 7042 tiki,
    # 8 dodge'ow): RLBot wpisuje do BUTTONS wylacznie wartosci
    #     [0, 1, 3, 4, 5, 6, 7]
    # czyli uzywa DOKLADNIE trzech bitow: 0x01 handbrake, 0x02 jump,
    # 0x04 boost. Bit 0x08 NIE POJAWIA SIE ANI RAZU, mimo ze bot boostuje
    # przez wiekszosc kickoffu.
    #
    # My pisalismy 0x04|0x08 na kazdy boost, wiec nasz BUTTONS mial 0x0F
    # tam, gdzie RLBot ma 0x07. Bit 0x08 jest najwyrazniej zarzadzany
    # wewnetrznie przez silnik (stad nazwa "HOLDING" - stan, nie zadanie)
    # i wymuszanie go z zewnatrz kolidowalo ze stanem skoku/dodge'a.
    # ALE: nasz patch nadpisuje CALE 32 bajty przy wejsciu do
    # SetVehicleInput, wiec kasujemy tez bity, ktore gra ustawila sobie sama.
    # RLBot pisze input wczesniej w potoku i tego problemu nie ma. Objaw:
    # online boost bywa wcisniety, a auto nie przyspiesza.
    #
    # PRISM_BOOST_HOLD=1 dopisuje 0x08 z powrotem (stare zachowanie).
    if cs.boost:
        flags |= V.BIT_ACTIVATE_BOOST
        if _BOOST_HOLD:
            flags |= V.BIT_HOLDING_BOOST
    buf[V.BUTTONS] = flags
    return bytes(buf)


def pack_replicated(cs: ControllerState) -> bytes:
    """2B (ReplicatedThrottle, ReplicatedSteer) pod Vehicle_TA + 0x804."""
    return bytes([f_to_u8(cs.throttle), f_to_u8(cs.steer)])


def unpack_input(data: bytes) -> ControllerState:
    """32B FVehicleInputs -> ControllerState (do diagnostyki / odczytu)."""
    V = O.VehicleInputs
    if len(data) < SIZE:
        return ControllerState.neutral()
    thr, ste, pit, yaw, rol = struct.unpack_from("<5f", data, V.THROTTLE)
    flags = data[V.BUTTONS]
    return ControllerState(
        throttle  = thr,
        steer     = ste,
        pitch     = pit,
        yaw       = yaw,
        roll      = rol,
        jump      = bool(flags & V.BIT_JUMP),
        boost     = bool(flags & (V.BIT_ACTIVATE_BOOST | V.BIT_HOLDING_BOOST)),
        handbrake = bool(flags & V.BIT_HANDBRAKE),
    )
