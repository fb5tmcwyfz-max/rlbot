"""
Match - stan meczu (GameEvent_Soccar_TA).

Score obu druzyn dziedziczy z Team_TA - nie duplikujemy tu, w Match sa metadata
meczu (czas, aktywna runda, overtime, itd).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from prism_sdk.low.driver import Driver
from prism_sdk.low        import offsets as O


@dataclass
class Match:
    """Snapshot stanu meczu / rundy."""
    address: int = 0

    #: True gdy runda aktywna (nie w intro/goal/podium)
    is_round_active: bool = False

    #: True gdy overtime
    is_overtime: bool = False

    #: True gdy tryb bez limitu czasu (Freeplay, custom)
    is_unlimited_time: bool = False

    #: True gdy mecz sie skonczyl
    is_match_ended: bool = False

    #: Aktualny numer rundy
    round_num: int = 0

    #: Calkowity czas meczu w sekundach (limit)
    total_game_time: int = 0

    #: Pozostaly czas do konca rundy w sekundach (0 gdy unlimited)
    seconds_remaining: int = 0

    #: Dokladniejsza wersja pozostalego czasu (float)
    time_remaining: float = 0.0

    #: Maksymalny wynik konczacy mecz (jesli nie unlimited)
    max_score: int = 0

    #: Indeks druzyny wygranej (0/1/-1 gdy niezdecydowane)
    match_winner_team: int = -1

    @classmethod
    def read(cls, drv: Driver, gameevent_addr: int) -> "Match":
        if gameevent_addr == 0:
            return cls()

        flags = drv.read_u32(gameevent_addr + O.GameEventSoccar.STATE_FLAGS)

        winner_ptr = drv.read_ptr(gameevent_addr + O.GameEventSoccar.MATCH_WINNER)
        winner_team = -1
        if winner_ptr != 0:
            winner_team = drv.read_i32(winner_ptr + O.TeamInfo.TEAM_INDEX)
            if winner_team not in (0, 1):
                winner_team = -1

        return cls(
            address=gameevent_addr,
            is_round_active   = bool(flags & O.GameEventSoccar.FLAG_ROUND_ACTIVE),
            is_overtime       = bool(flags & O.GameEventSoccar.FLAG_OVERTIME),
            is_unlimited_time = bool(flags & O.GameEventSoccar.FLAG_UNLIMITED_TIME),
            is_match_ended    = bool(flags & O.GameEventSoccar.FLAG_MATCH_ENDED),
            round_num         = drv.read_i32(gameevent_addr + O.GameEventSoccar.ROUND_NUM),
            total_game_time   = drv.read_i32(gameevent_addr + O.GameEventSoccar.GAME_TIME),
            seconds_remaining = drv.read_i32(gameevent_addr + O.GameEventSoccar.SECONDS_REMAINING),
            time_remaining    = drv.read_f32(gameevent_addr + O.GameEventSoccar.GAME_TIME_REMAINING),
            max_score         = drv.read_i32(gameevent_addr + O.GameEventSoccar.MAX_SCORE),
            match_winner_team = winner_team,
        )
