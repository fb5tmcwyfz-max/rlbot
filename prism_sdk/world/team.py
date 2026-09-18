"""
Team - druzyna (blue/orange). Score + lista czlonkow (PRI addrs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from prism_sdk.low.driver import Driver
from prism_sdk.low        import offsets as O
from prism_sdk.world._common import read_tarray_ptrs


@dataclass
class Team:
    address:      int = 0
    team_index:   int = -1        # 0=blue, 1=orange
    score:        int = 0
    member_pris:  Tuple[int, ...] = ()   # adresy PRI_TA graczy w druzynie

    @property
    def is_blue(self) -> bool:   return self.team_index == 0

    @property
    def is_orange(self) -> bool: return self.team_index == 1

    @classmethod
    def read(cls, drv: Driver, team_addr: int) -> "Team":
        if team_addr == 0:
            return cls()
        idx = drv.read_i32(team_addr + O.TeamInfo.TEAM_INDEX)
        score = drv.read_i32(team_addr + O.TeamInfo.SCORE)
        members = read_tarray_ptrs(drv, team_addr + O.Team.MEMBERS, max_count=8)
        return cls(
            address=team_addr,
            team_index=idx if idx in (0, 1) else -1,
            score=score,
            member_pris=members,
        )
