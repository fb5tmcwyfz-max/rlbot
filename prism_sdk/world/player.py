"""
Player - gracz w RL. Wraps auto + PRI (nazwa, druzyna, gole, saves...).

Nie kazdy Car ma powiazany Player (auta w Freeplay czasem nie maja PRI).
Zawsze polaczenie idzie w kierunku PRI -> Car (bo PRI.Car @ 0x498 jest wskaznikiem).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from prism_sdk.low.driver import Driver
from prism_sdk.low        import offsets as O
from prism_sdk.world.car  import Car
from prism_sdk.world._common import read_fstring


@dataclass
class Player:
    """Gracz + jego auto."""

    #: Adres PRI_TA
    pri_address: int = 0

    #: Adres UCar_TA (moze byc 0 gdy gracz nie ma auta - obserwator, spawning)
    car_address: int = 0

    #: Auto tego gracza (None gdy car_address==0)
    car: Optional[Car] = None

    #: Nazwa gracza (moze byc pusta jesli nie udalo sie odczytac FString)
    name: str = ""

    #: 0=blue, 1=orange, -1=nieznane
    team_num: int = -1

    #: Adres UTeam_TA (do korelacji z World.teams[])
    team_address: int = 0

    #: True gdy to bot z gry (nie nasz agent, tylko wbudowany AI RL)
    is_ingame_bot: bool = False

    # Match stats
    match_score:     int = 0
    match_goals:     int = 0
    match_own_goals: int = 0
    match_assists:   int = 0
    match_saves:     int = 0
    match_shots:     int = 0
    match_demolishes: int = 0

    #: True gdy MVP meczu
    is_mvp: bool = False

    @classmethod
    def read(cls, drv: Driver, pri_addr: int) -> "Player":
        if pri_addr == 0:
            return cls()

        name  = read_fstring(drv, pri_addr + O.PRI.PLAYER_NAME, max_chars=32)

        team_ptr = drv.read_ptr(pri_addr + O.PRI.TEAM)
        team_num = -1
        if team_ptr != 0:
            team_num = drv.read_i32(team_ptr + O.TeamInfo.TEAM_INDEX)
            if team_num not in (0, 1):
                team_num = -1

        is_bot_flags = drv.read_u32(pri_addr + O.PRI.IS_BOT_FLAGS)
        match_flags  = drv.read_u32(pri_addr + O.PRI.MATCH_FLAGS)

        car_ptr = drv.read_ptr(pri_addr + O.PRI.CAR)
        car = Car.read(drv, car_ptr) if car_ptr != 0 else None

        return cls(
            pri_address=pri_addr,
            car_address=car_ptr,
            car=car,
            name=name,
            team_num=team_num,
            team_address=team_ptr,
            is_ingame_bot=bool(is_bot_flags & O.PRI.FLAG_IS_BOT),
            match_score      = drv.read_i32(pri_addr + O.PRI.MATCH_SCORE),
            match_goals      = drv.read_i32(pri_addr + O.PRI.MATCH_GOALS),
            match_own_goals  = drv.read_i32(pri_addr + O.PRI.MATCH_OWN_GOALS),
            match_assists    = drv.read_i32(pri_addr + O.PRI.MATCH_ASSISTS),
            match_saves      = drv.read_i32(pri_addr + O.PRI.MATCH_SAVES),
            match_shots      = drv.read_i32(pri_addr + O.PRI.MATCH_SHOTS),
            match_demolishes = drv.read_i32(pri_addr + O.PRI.MATCH_DEMOLISHES),
            is_mvp=bool(match_flags & O.PRI.FLAG_MVP),
        )

    def __repr__(self) -> str:
        who = self.name or f"pri:0x{self.pri_address:X}"
        team = {0: "blue", 1: "orange"}.get(self.team_num, "?")
        return f"<Player {who} team={team} goals={self.match_goals}>"
