"""
GameModeDetector - runtime introspekcja aktualnego meczu.

Wykrywa:
  * kind:         Freeplay / Season / SoccarPrivate / SoccarOnline / KnockOut / Rumble / Hoops / Snowday / Dropshot / Training / Tutorial
  * team_size:    1 / 2 / 3 / 4 (rozmiar druzyny)
  * team_bots:    ile AI botow w meczu (0 = pure human)
  * is_active:    round_active flag (kickoff-safe)
  * ge_class:     nazwa klasy GameEvent (Soccar_TA / Season_TA / KnockOut_TA / etc.)
  * bot_side:     "blue" / "orange" - w ktorej druzynie jest bot

Kluczowe uzycie:
  * SupervisorAgent - moze skipnac skrypty w wrong trybie (np. FastKickoff tylko
    w normalnym Soccar, nie w Rumble/Hoops)
  * OptiAgent / NextoAgent - team_size do wlasciwej obs dimension
  * PatchSender - hint jesli inny gamemode wymaga innego hooka
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from prism_sdk.low import offsets as O


# Mapowanie GameEvent class name -> user-friendly nazwa trybu
_GAMEEVENT_KIND_MAP = {
    "GameEvent_Soccar_TA":       "Soccar",
    "GameEvent_Season_TA":       "Season",           # offline vs AI, season mode
    "GameEvent_KnockOut_TA":     "KnockOut",         # nowy tryb 1v1v1v1v1v1v1v1
    "GameEvent_Football_TA":     "Football",         # Rocket Rugby (soccer with rugby ball)
    "GameEvent_GodBall_TA":      "GodBall",          # Rumble mode
    "GameEvent_Breakout_TA":     "Breakout",         # brickbreak?
    "GameEvent_Territory_TA":    "Territory",        # heatseeker?
    "GameEvent_Tutorial_Basic_TA":    "Tutorial",
    "GameEvent_Tutorial_Advanced_TA": "Tutorial",
    "GameEvent_Tutorial_FreePlay_TA": "Freeplay",
    "GameEvent_Training_TA":     "Training",
    "GameEvent_Training_Aerial_TA":  "Training_Aerial",
    "GameEvent_Training_Goalie_TA":  "Training_Goalie",
    "GameEvent_Training_Striker_TA": "Training_Striker",
    "GameEvent_TrainingEditor_TA":   "Training_Custom",
    "GameEvent_GameEditor_TA":   "Training_Editor",
    "GameEvent_Lobby_TA":        "Lobby",
    "GameEvent_PostGameLobby_TA":"PostGame",
}


@dataclass
class GameModeInfo:
    ge_class:   str = ""
    ge_addr:    int = 0
    kind:       str = "Unknown"        # Soccar / Freeplay / Training / etc.
    team_size:  int = 0                # 1/2/3/4, 0 = nieznane
    num_bots:   int = 0
    is_active:  bool = False
    bot_side:   str = "?"              # "blue" / "orange" / "?"
    n_players:  int = 0

    def summary(self) -> str:
        parts = [f"{self.kind}"]
        if self.team_size > 0:
            parts.append(f"{self.team_size}v{self.team_size}")
        if self.num_bots > 0:
            parts.append(f"+{self.num_bots}bots")
        if self.n_players > 0:
            parts.append(f"({self.n_players} graczy)")
        if not self.is_active:
            parts.append("[nieaktywny]")
        parts.append(f"team={self.bot_side}")
        return " ".join(parts)


class GameModeDetector:
    """Odczyt biezacego trybu gry z live memory."""

    def __init__(self, world):
        self.world = world
        self.info = GameModeInfo()

    def detect(self) -> GameModeInfo:
        """Wolane na start (i moze co N tickow gdy zmieni sie tryb)."""
        w = self.world
        info = GameModeInfo()
        info.ge_addr = w._game_event_addr

        if info.ge_addr == 0:
            info.kind = "Menu"
            self.info = info
            return info

        # Read GameEvent class name (Object.Class -> class_obj.Name)
        try:
            class_ptr = w.driver.read_ptr(info.ge_addr + O.UOBJECT_NAME_IDX - 0x8)  # +0x50 Class
            # Actually: Object.Class at +0x50 -> UClass* -> Name at +0x48 (int32) -> GNames[idx]
            # Simpler: use SDK reflection cache jesli mamy nazwe
            # Fallback: sprobuj przez reflection.stats
            stats = w.refl.stats() if hasattr(w, "refl") else {}
            # Znajdz klase o najwiekszej liczbie instancji ktora jest GameEvent_*_TA
            candidates = [k for k in stats.keys() if k.startswith("GameEvent_") and k.endswith("_TA")]
            if candidates:
                # Bierz aktywny GameEvent - refl.highest zwraca instancje o max index
                # Jesli SDK dostarcza to najlepiej wg naszej istniejacej klasy game_event_addr
                # Prosto: klasa naszego game_event_addr
                info.ge_class = self._resolve_ge_class_name(info.ge_addr) or ""
        except Exception:
            pass

        if not info.ge_class:
            info.ge_class = "Unknown"
        info.kind = _GAMEEVENT_KIND_MAP.get(info.ge_class, info.ge_class)

        # TeamSize / NumBots (tylko dla GameEvent_Team_TA i podklas)
        try:
            info.team_size = w.driver.read_i32(info.ge_addr + O.GameEventTeam.MAX_TEAM_SIZE)
            info.num_bots  = w.driver.read_i32(info.ge_addr + O.GameEventTeam.NUM_BOTS)
            if info.team_size < 0 or info.team_size > 8:
                info.team_size = 0
        except Exception:
            pass

        # Round active flag - reuse existing Match read
        try:
            info.is_active = bool(w.match.is_round_active)
        except Exception:
            pass

        info.n_players = len(w.players) if hasattr(w, "players") else 0

        # bot_side: znajdz naszego local playera (przez LocalPlayers chain)
        try:
            bot_pri = self._find_local_pri()
            if bot_pri:
                for p in w.players:
                    if getattr(p, "pri_address", 0) == bot_pri:
                        info.bot_side = {0: "blue", 1: "orange"}.get(p.team_num, "?")
                        break
        except Exception:
            pass

        self.info = info
        return info

    def _resolve_ge_class_name(self, obj_addr: int) -> Optional[str]:
        """Odczytaj nazwe klasy UObject przez chain: obj -> Class -> Name idx -> GNames."""
        try:
            drv = w = self.world.driver
            # UObject.Class @ +0x50 (potwierdzone Core.txt)
            class_ptr = self.world.driver.read_ptr(obj_addr + 0x50)
            if class_ptr == 0: return None
            # UObject.Name @ +0x48 = FName (int32 idx + int32 number)
            name_idx = self.world.driver.read_i32(class_ptr + 0x48)
            # GNames[name_idx] -> FNameEntry -> Text @ +0x18
            gnames_va = self.world.driver.module_base + O.GNAMES_RVA
            gnames_data = self.world.driver.read_ptr(gnames_va)
            if gnames_data == 0: return None
            entry_ptr = self.world.driver.read_ptr(gnames_data + name_idx * 8)
            if entry_ptr == 0: return None
            return self.world.driver.read_ascii_from_wide(entry_ptr + 0x18, max_chars=64)
        except Exception:
            return None

    def _find_local_pri(self) -> int:
        """Wyciagnij nasze PRI addr przez LocalPlayers chain."""
        try:
            ge = self.world._game_event_addr
            lp_data  = self.world.driver.read_ptr(ge + 0x360)
            lp_count = self.world.driver.read_i32(ge + 0x360 + 8)
            if lp_data == 0 or lp_count <= 0: return 0
            pc = self.world.driver.read_ptr(lp_data)
            if pc == 0: return 0
            return self.world.driver.read_ptr(pc + 0x288)  # PC.PRI
        except Exception:
            return 0
