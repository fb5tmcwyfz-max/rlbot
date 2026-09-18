"""
World - top-level snapshot gry. Spina Ball + Cars + Players + Teams + Match + BoostPads.

Uzycie:
    world = World()
    if not world.attach():  raise RuntimeError("no game")
    while running:
        if world.refresh():
            for p in world.players: print(p)
            print(world.ball.position, world.match.time_remaining)

Optymalizacja:
    * `refresh()` re-scan GObjects tylko gdy adresy sa stale (auto detection).
    * Boost pady resolve jednorazowo (adresy stabilne w meczu), status
      odswiezany per tick szybkim read (1B / pad).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from prism_sdk.low.driver     import Driver, PROCESS_NAME
from prism_sdk.low.reflection import Reflection
from prism_sdk.low            import offsets as O

from prism_sdk.world.ball      import Ball
from prism_sdk.world.car       import Car
from prism_sdk.world.player    import Player
from prism_sdk.world.team      import Team
from prism_sdk.world.match     import Match
from prism_sdk.world.boost_pad import BoostPad


class World:
    """Glowne API SDK. Trzyma Driver + Reflection + snapshot.

    Snapshot jest budowany co refresh() - agenty widza spojne dane w danym ticku.
    """

    def __init__(self, process_name: str = PROCESS_NAME):
        self.driver    = Driver(process_name)
        self.refl      = Reflection(self.driver)

        # Cachowane adresy z GObjects (invalidowane gdy read sie posypie)
        self._ball_addr: int = 0
        self._game_event_addr: int = 0
        self._car_addrs: List[int] = []
        self._pri_addrs: List[int] = []
        self._team_addrs: List[int] = []
        self._pads:      List[BoostPad] = []      # z cachowanymi pozycjami

        # Aktualny snapshot
        self.ball:     Ball = Ball()
        self.match:    Match = Match()
        self.cars:     List[Car] = []
        self.players:  List[Player] = []
        self.teams:    Dict[int, Team] = {}       # team_index -> Team
        self.boost_pads: List[BoostPad] = []

        #: Ile klatek z rzedu swiat wyglada na martwy (brak lokalnego gracza).
        #: Po przekroczeniu limitu wymuszamy pelny rescan - patrz refresh().
        self._dead_world_frames: int = 0
        #: 60 klatek @120 Hz = 0.5 s. Dosc, zeby przetrwac dziure przy
        #: respawnie; za malo, zeby bot stal bezczynnie po zmianie trybu.
        self.DEAD_WORLD_LIMIT: int = 60

        self._last_full_scan: float = 0.0
        self._full_scan_interval: float = 5.0     # re-scan GObjects co 5s (bezpiecznik)

    # ---- lifecycle ----

    def attach(self) -> bool:
        if not self.driver.attach():
            return False
        return self._full_rescan()

    def detach(self):
        self.driver.detach()

    @property
    def attached(self) -> bool:
        return self.driver.attached

    # ---- refresh ----

    def refresh(self) -> bool:
        """Odswiez snapshot. Zwraca False gdy gra niedostepna (menu / nowy mecz)."""
        if not self.driver.attached:
            return False

        # okresowy pelny rescan zeby wychwycic respawny/nowe mecze
        now = time.time()
        # SKAN TYLKO GDY NAPRAWDE TRZEBA - nigdy "co N sekund".
        #
        # `_full_rescan()` przechodzi cala tablice GObjects i trwa ~5 SEKUND
        # (zmierzone: srednia 4963 ms). Odpalany co 5 s oznaczal, ze petla
        # sterowania zamierala na ~600 tikow fizyki, trzymajac ostatnia akcje -
        # auto jechalo prosto w sciane albo w boost, po czym bot "wracal".
        # Bot pracowal z 50% czasu sprawnosci. To jest ten objaw:
        # "przez chwile jest dobrze, nagle zawraca, przestaje kontrolowac,
        # znow sie polaczy".
        #
        # Sklad i auta czytamy teraz z ZYWYCH tablic GameEventu (PRIs 0x340,
        # Cars 0x350), wiec skan jest potrzebny wylacznie do znalezienia
        # samego GameEventu i pilki - czyli raz, przy starcie, i ponownie
        # dopiero gdy adresy realnie przestana byc wazne (zmiana mapy, menu).
        # Skan gdy adresy sa puste ALBO gdy swiat przestal odpowiadac.
        #
        # Sam warunek "adres == 0" NIE wystarcza. Po zmianie meczu/mapy gra
        # tworzy nowy GameEvent, a stary wskaznik nadal daje sie odczytac -
        # jest tylko martwy. Wtedy czytamy trupa: jego tablica PRIs sie
        # wysypuje (widziane: "sklad zmienil sie 6 -> 5 -> 4 -> 3"), a
        # LocalPlayers jest pusta, wiec PCChannel w kolko pisze "brak
        # lokalnego PlayerControllera" i bot juz sie nie podnosi.
        #
        # Dlatego liczymy klatki bez lokalnego gracza i po ~0.5 s wymuszamy
        # rescan. Zdrowa gra nigdy tego nie dotyka - licznik zeruje sie przy
        # pierwszym poprawnym odczycie.
        need_scan = (self._ball_addr == 0
                     or self._game_event_addr == 0
                     or self._dead_world_frames > self.DEAD_WORLD_LIMIT)
        if need_scan:
            self._dead_world_frames = 0

        if need_scan and not self._full_rescan():
            return False

        # Ball - ZAWSZE odswiez adres z GameEvent.GameBalls[0] (zywa pilka).
        # Bez tego po golu bot jedzie do "ducha" starej pilki (staly input,
        # krecenie w kolko) az do rescanu co 5s. To cheap: 2 read_ptr.
        live_ball = self._resolve_live_ball()
        if live_ball:
            self._ball_addr = live_ball
        self.ball = Ball.read(self.driver, self._ball_addr)
        # awaryjnie: pilka zerowa -> pelny rescan (menu/przejscie)
        if (self.ball.position == 0).all() and (self.ball.linear_velocity == 0).all():
            if self._full_rescan():
                live_ball = self._resolve_live_ball()
                if live_ball:
                    self._ball_addr = live_ball
                self.ball = Ball.read(self.driver, self._ball_addr)

        # Match
        self.match = Match.read(self.driver, self._game_event_addr)

        # Teams
        self.teams = {t.team_index: t for t in
                      (Team.read(self.driver, a) for a in self._team_addrs) if t.team_index >= 0}

        # Players - z ZYWEJ tablicy GameEvent.PRIs (0x340), nie ze skanu.
        # Dzieki temu dolaczajacy/wychodzacy gracze i boty sa widoczni
        # natychmiast, a nie po nastepnym skanie.
        live_pris = self._read_ptr_array(self._game_event_addr + O.GameEventSoccar.PRIS)
        if live_pris:
            self._pri_addrs = live_pris
        self.players = [Player.read(self.driver, a) for a in self._pri_addrs]

        if self._game_event_alive():
            self._dead_world_frames = 0
        else:
            self._dead_world_frames += 1

        # Cars - NA ZYWO, nie z 5-sekundowego skanu.
        #
        # `_car_addrs` pochodzi z pelnego skanu GObjects, ktory leci raz na
        # `_full_scan_interval` (5 s). Po golu, respawnie albo dolaczeniu bota
        # gra tworzy NOWE obiekty aut pod nowymi adresami - a my przez cale
        # 5 sekund czytalibysmy trupy. Objaw jest identyczny jak ten opisany
        # wyzej przy pilce: bot dziala wzgledem nieistniejacego juz auta,
        # czyli "po golu jezdzi w kolko w zupelnie innym miejscu".
        #
        # PRI kazdego gracza trzyma ZYWY wskaznik na jego auto i jest czytany
        # w kazdym refresh, wiec bierzemy adresy stamtad. Adresy ze skanu
        # dokladamy tylko dla aut BEZ gracza (np. bot we Freeplay), i tylko
        # jesli nie duplikuja tych zywych.
        # Zrodlem prawdy sa PRI graczy, NIE skan GObjects.
        #
        # Skan znajduje wszystkie obiekty klasy Car_TA jakie gra ma w tablicy -
        # zmierzone: 65 sztuk przy 6 graczach. Reszta to martwe obiekty po
        # poprzednich rundach i respawnach. Czytanie ich co tick to
        # jednoczesnie strata czasu (dominowaly ~7 ms na refresh) i czytanie
        # zwolnionej pamieci.
        live_addrs = [p.car_address for p in self.players if p.car_address]
        if not live_addrs:
            # Brak graczy z autami - jedyny przypadek, gdy skan jest potrzebny
            # (Freeplay z botem, ktory nie ma PRI).
            live_addrs = [a for a in self._car_addrs if a]
        self.cars = [Car.read(self.driver, a) for a in live_addrs]

        # Boost pads - tylko status refresh (pozycje sa stabilne, cache je w self._pads)
        self.boost_pads = [BoostPad.refresh_status(self.driver, p) for p in self._pads]

        return True

    def _game_event_alive(self) -> bool:
        """Czy GameEvent, ktory trzymamy, opisuje TRWAJACY mecz.

        Sygnal wg obserwacji: w menu nie ma licznika czasu meczu, a w nowym
        meczu pojawia sie natychmiast. Zmierzone na martwym GameEvencie
        (zostal po zmianie trybu) - wszystko czyta sie jako zera:

            GameTime=0  SecondsRemaining=0  flagi stanu=0x0

        Zywy mecz ma dlugosc meczu w GameTime (np. 300) i ustawione bity stanu.
        Freeplay/trening maja czas nielimitowany, wiec tam ratuje nas flaga
        bUnlimitedTime - dlatego warunek jest alternatywa, nie koniunkcja.

        To JEDEN odczyt uint32 zamiast przechodzenia dwoch tablic wskaznikow,
        wiec mozemy go robic w kazdej klatce bez kosztu.
        """
        ge = self._game_event_addr
        if not ge:
            return False
        try:
            flags = self.driver.read_u32(ge + O.GameEventSoccar.STATE_FLAGS)
            if flags == 0:
                return False            # zaden zywy GameEvent nie ma zerowych flag
            if flags & O.GameEventSoccar.FLAG_UNLIMITED_TIME:
                return True             # freeplay/trening - licznik nie obowiazuje
            return self.driver.read_i32(ge + O.GameEventSoccar.GAME_TIME) > 0
        except Exception:
            return False

    def _read_ptr_array(self, tarray_addr: int, max_count: int = 64) -> List[int]:
        """Odczyt UE3 TArray<T*> -> lista adresow. Pusta lista przy bledzie.

        TArray to {void* data; int32 count; int32 max} - dwa odczyty i gotowe.
        To jest ZYWE zrodlo: gra aktualizuje te tablice natychmiast, gdy ktos
        dolaczy, wyjdzie albo dostanie nowe auto po respawnie.
        """
        try:
            data = self.driver.read_ptr(tarray_addr)
            count = self.driver.read_i32(tarray_addr + 8)
        except Exception:
            return []
        if not data or count <= 0 or count > max_count:
            return []
        out = []
        for i in range(count):
            try:
                p = self.driver.read_ptr(data + i * 8)
            except Exception:
                continue
            if p:
                out.append(p)
        return out

    # ---- helpers ----

    def _resolve_live_ball(self) -> int:
        """Adres ZYWEJ pilki z GameEvent.GameBalls[0] (0 gdy brak).

        Autorytatywne zrodlo - nie zgaduje po najwyzszym indeksie GObjects,
        ktory po golu/resecie potrafi wskazac MARTWY obiekt pilki (bot jedzie
        wtedy do "ducha": staly input, krecenie w kolko przy speed~0)."""
        ge = self._game_event_addr
        if not ge:
            return 0
        try:
            gb = self.driver.read_ptr(ge + O.GameEventSoccar.GAME_BALLS)   # TArray.data
            if not gb:
                return 0
            return self.driver.read_ptr(gb) or 0                           # GameBalls[0]
        except Exception:
            return 0

    def _full_rescan(self) -> bool:
        """Ponowny scan GObjects. Uzywa nazw klas z offsets.CAR_CLASS_CANDIDATES."""
        if not self.refl.rebuild():
            return False

        # GameEvent - IS-A "GameEvent_Soccar_TA" (obsluguje wszystkie tryby:
        # Soccar/Casual/Private + Dropshot=Breakout + Gridiron=Football +
        # GodBall + KnockOut + Territory + Season + Training + Free Play +
        # Tutorial - wszystkie dziedzicza po Soccar_TA lub Tutorial_TA-> Soccar).
        # NAJPIERW - bo wskazuje ZYWA pilke przez GameBalls.
        self._game_event_addr = (
            self.refl.highest_by_ancestor(O.CLASS_GAMEEVENT_SOCCAR)
            or self.refl.highest(O.CLASS_GAMEEVENT_TA)
            or 0
        )

        # Ball - ZYWA pilka z GameEvent.GameBalls[0]; highest("Ball_TA") tylko
        # jako fallback (bywa STALE po golu/resecie).
        self._ball_addr = self._resolve_live_ball() or self.refl.highest(O.CLASS_BALL) or 0

        # Cars - IS-A "Vehicle_TA" (obsluguje wszystkie podklasy: Freeplay/Season/
        # KnockOut/etc.). Metaklasy juz odsiane przez find_by_ancestor
        # (FName.Number > 0 filter). Dodatkowo sanity: zywe auto ma PRI != 0
        # albo sensowna pozycje (nie 0,0,0).
        # PROBOWANE I ODRZUCONE: pomijanie tego skanu, gdy GameEvent.PRIs jest
        # niepuste. Zysk zmierzony: 0% - bo skan PRI ponizej kosztuje tyle samo
        # co skan aut, wiec wyciecie jednego nic nie dawalo. Do tego psulo
        # odczyt (graczy=0). Zostaje jak bylo.
        car_matches = self.refl.find_by_ancestor("Vehicle_TA")
        car_addrs: List[int] = []
        for idx, addr in car_matches:
            try:
                pri = self.driver.read_ptr(addr + O.Vehicle.PRI)
                pos_bytes = self.driver.read_bytes(
                    addr + O.RBActor.RB_STATE + O.RBState.LOCATION, 12)
                import struct as _s
                x, y, z = _s.unpack("<fff", pos_bytes) if pos_bytes else (0.0, 0.0, 0.0)
                if pri != 0 or abs(x) + abs(y) + abs(z) > 10.0:
                    car_addrs.append(addr)
            except Exception:
                car_addrs.append(addr)
        self._car_addrs = list(dict.fromkeys(car_addrs))

        self._scan_players_and_teams()
        self._last_full_scan = time.time()
        return self._ball_addr != 0 and self._game_event_addr != 0

    def _scan_players_and_teams(self) -> None:
        """Skan PRI, druzyn i padow. Wydzielone z _full_rescan, bo przy zywych
        PRI omijamy najdrozszy element (skan aut) i wolamy tylko to."""
        # Players (PRI)
        pri_matches = self.refl.find(O.CLASS_PRI)
        # Odsiej CDO (najnizszy indeks) - wez pozostale
        if len(pri_matches) >= 2:
            sorted_pri = sorted(pri_matches, key=lambda m: m[0])
            self._pri_addrs = [addr for _, addr in sorted_pri[1:]]
        else:
            self._pri_addrs = []

        # Teams - w Freeplay solo moze byc TYLKO 1 team (blue), w meczu 2.
        # CDO jest zwykle na najnizszym indeksie - odsiewamy przez validate TeamIndex.
        team_matches = self.refl.find(O.CLASS_TEAM_SOCCAR) or self.refl.find(O.CLASS_TEAM)
        team_addrs = []
        for _, addr in sorted(team_matches, key=lambda m: m[0]):
            idx = self.driver.read_i32(addr + O.TeamInfo.TEAM_INDEX)
            if idx in (0, 1):        # tylko real team objects (0=blue, 1=orange)
                team_addrs.append(addr)
        self._team_addrs = team_addrs

        # Boost pads (jednorazowo - pozycje stabilne)
        pad_matches = self.refl.find(O.CLASS_BOOST_PICKUP)
        pads = []
        for _, addr in pad_matches:
            p = BoostPad.read(self.driver, addr)
            # Filter: pomin CDO (position 0,0,0)
            if not (p.position == 0).all():
                pads.append(p)
        self._pads = pads


    # ---- filter helpers ----

    @property
    def blue_team(self) -> Optional[Team]:
        return self.teams.get(0)

    @property
    def orange_team(self) -> Optional[Team]:
        return self.teams.get(1)

    @property
    def local_player(self) -> Optional[Player]:
        """Player ktorego auto jest human-controlled (my). None jesli nie znaleziono."""
        for p in self.players:
            if p.car is not None and p.car.is_human_controlled:
                return p
        return None

    def players_by_team(self, team_num: int) -> List[Player]:
        return [p for p in self.players if p.team_num == team_num]

    def active_boost_pads(self) -> List[BoostPad]:
        return [p for p in self.boost_pads if p.is_active]

    # ---- diagnostyka ----

    def describe(self) -> str:
        lines = [f"[World] driver_pid={self.driver.pid} module=0x{self.driver.module_base:X}"]
        lines.append(f"  match: active={self.match.is_round_active} "
                     f"time={self.match.time_remaining:.1f} winner={self.match.match_winner_team}")
        lines.append(f"  ball: pos={self.ball.position} speed={self.ball.speed:.0f} "
                     f"predicted={len(self.ball.predicted_positions)}")
        lines.append(f"  cars: {len(self.cars)}, players: {len(self.players)}, "
                     f"teams: {len(self.teams)}, pads: {len(self.boost_pads)} "
                     f"({sum(1 for p in self.boost_pads if p.is_active)} active)")
        for p in self.players[:6]:
            lines.append(f"    {p}")
        return "\n".join(lines)
