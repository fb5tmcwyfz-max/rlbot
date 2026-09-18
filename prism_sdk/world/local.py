"""
Identyfikacja LOKALNEGO gracza: GameEvent -> LocalPlayers[0] -> PC -> Pawn/PRI.

    GameEvent + 0x360 = TArray<UPlayerController_TA*> LocalPlayers
    LocalPlayers[0]   = UPlayerController_TA*   (bezposrednio, nie UPlayer!)
    PC + 0x280        = UPawn*   (== Vehicle_TA*)  <- NASZE auto
    PC + 0x288        = UPRI_TA*

LocalPlayers to gracze fizycznie na TEJ maszynie (splitscreen = 2, normalnie 1).
W meczu online tylko 1 z 6 PlayerControllerow jest lokalny - reszta jest
replikowana z serwera i nie ma Pawna po naszej stronie.

Adresy zmieniaja sie po KAZDYM respawnie (gol, reset, zmiana trybu), dlatego
resolwujemy je co tick zamiast cache'owac indeksy w world.cars.
"""

from __future__ import annotations

from typing import NamedTuple

from prism_sdk.low import offsets as O


class LocalChain(NamedTuple):
    """Wynik jednego resolve. Zera = respawn w toku / menu / spectator."""
    pc:   int    # UPlayerController_TA*
    pawn: int    # UVehicle_TA* (nasze auto)
    pri:  int    # UPRI_TA*

    @property
    def valid(self) -> bool:
        return self.pc != 0 and self.pawn != 0


_EMPTY = LocalChain(0, 0, 0)


def resolve_local_chain(world) -> LocalChain:
    """PC + Pawn + PRI lokalnego gracza w 3 odczytach IOCTL."""
    drv = getattr(world, "driver", None)
    ge  = getattr(world, "_game_event_addr", 0)
    if drv is None or not ge:
        return _EMPTY
    try:
        lp_data, lp_count, _ = drv.read_tarray(ge + O.GameEvent.LOCAL_PLAYERS)
        if lp_data == 0 or lp_count <= 0:
            return _EMPTY
        pc = drv.read_ptr(lp_data)
        if pc == 0:
            return _EMPTY
        # Pawn (0x280) i PRI (0x288) leza obok siebie - jeden odczyt 16B
        both = drv.read_bytes(pc + O.Controller.PAWN, 16)
        if len(both) != 16:
            return LocalChain(pc, 0, 0)
        pawn = int.from_bytes(both[0:8],  "little")
        pri  = int.from_bytes(both[8:16], "little")
        return LocalChain(pc, pawn, pri)
    except Exception:
        return _EMPTY


def resolve_local_pc(world) -> int:
    """UPlayerController_TA* lokalnego gracza (0 = menu / brak)."""
    return resolve_local_chain(world).pc


def resolve_local_pawn(world) -> int:
    """UVehicle_TA* lokalnego auta (0 = respawn / menu / spectator)."""
    return resolve_local_chain(world).pawn


def resolve_local_pri(world) -> int:
    """UPRI_TA* lokalnego gracza (0 = brak)."""
    return resolve_local_chain(world).pri
