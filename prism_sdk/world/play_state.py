"""
Czy MY realnie gramy w tej chwili?

Bez tej bramki bot mieli caly czas: w menu, w replayu gola, na ekranie koncowym
i w odliczaniu kickoffu. To nie jest tylko marnowanie CPU - w odliczaniu gra
zamraza auto, wiec siec dostaje stan "stoje w miejscu", podejmuje na jego
podstawie decyzje i wchodzi w moment odmrozenia z akcja policzona dla martwego
stanu.

Sygnaly, ktorych uzywamy (od najpewniejszego):

  1. chain LocalPlayers -> PC -> Pawn      brak = menu / replay / spectator /
                                            miedzy meczami
  2. auto o tym adresie w world.cars       brak = snapshot niespojny, poczekaj
  3. car.is_driving                        False = odliczanie 3-2-1, podium,
                                            zamrozenie po golu
  4. match.is_match_ended                  koniec meczu

CELOWO nie bramkujemy po `match.is_round_active`. W Freeplay i czesci trybow
treningowych ta flaga bywa False mimo ze normalnie jezdzimy - gating po niej
zablokowalby bota na amen. Wystawiamy ja tylko informacyjnie.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from prism_sdk.world.local import resolve_local_chain


class PlayState(Enum):
    """Stan z punktu widzenia STEROWANIA, nie z punktu widzenia meczu."""

    PLAYING     = "playing"      # jedziemy, mozna sterowac
    COUNTDOWN   = "countdown"    # auto jest, ale zamrozone (3-2-1, po golu)
    NO_CAR      = "no_car"       # menu, replay, spectator, miedzy meczami
    MATCH_ENDED = "match_ended"  # ekran koncowy / podium

    @property
    def can_control(self) -> bool:
        return self is PlayState.PLAYING


@dataclass
class PlayStatus:
    state: PlayState
    pawn: int = 0
    pc: int = 0
    car: object = None
    round_active: bool = False

    @property
    def can_control(self) -> bool:
        return self.state.can_control

    def describe(self) -> str:
        opis = {
            PlayState.PLAYING:     "gramy",
            PlayState.COUNTDOWN:   "auto zamrozone (odliczanie / po golu)",
            PlayState.NO_CAR:      "brak auta (menu / replay / spectator)",
            PlayState.MATCH_ENDED: "mecz zakonczony",
        }[self.state]
        return opis


def detect_play_state(world) -> PlayStatus:
    """Jeden odczyt stanu. Tanie - korzysta z juz odswiezonego snapshotu World."""
    chain = resolve_local_chain(world)
    if not chain.valid:
        return PlayStatus(PlayState.NO_CAR, pc=chain.pc)

    match = getattr(world, "match", None)
    round_active = bool(getattr(match, "is_round_active", False))
    if match is not None and getattr(match, "is_match_ended", False):
        return PlayStatus(PlayState.MATCH_ENDED, pawn=chain.pawn, pc=chain.pc,
                          round_active=round_active)

    car = None
    for c in getattr(world, "cars", ()) or ():
        if c.address == chain.pawn:
            car = c
            break
    if car is None:
        # Pawn istnieje, ale skan aut jeszcze go nie zlapal - traktuj jak brak,
        # zaraz sie pojawi przy nastepnym refreshu.
        return PlayStatus(PlayState.NO_CAR, pawn=chain.pawn, pc=chain.pc,
                          round_active=round_active)

    if not car.is_driving:
        return PlayStatus(PlayState.COUNTDOWN, pawn=chain.pawn, pc=chain.pc,
                          car=car, round_active=round_active)

    return PlayStatus(PlayState.PLAYING, pawn=chain.pawn, pc=chain.pc,
                      car=car, round_active=round_active)
