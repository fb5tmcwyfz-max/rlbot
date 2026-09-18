"""
RLBotTimingMixin - odtwarza taktowanie i kickoff z RLBotowego `bot.py`.

Po co: modele z rodziny RLGym-PPO (Nexto, Necto, Kumori) graja pod RLBotem
WYRAZNIE lepiej niz pod naszym Managerem, mimo ze siec i wybor akcji sa
identyczne. Roznica siedzi w dwoch rzeczach, ktore robi ich `bot.py`:

1. KICKOFF NIE JEST GRANY SIECIA. `hardcoded_kickoffs=True` jest u nich
   domyslne - stala sekwencja 168 tikow przejmuje sterowanie na czas
   kickoffu. Kickoff zdarza sie po kazdym golu, wiec to najbardziej widoczny
   fragment meczu.

2. AKCJA WCHODZI Z OPOZNIENIEM FAZOWYM. Ich petla:

       ticks_elapsed = round(delta * 120)
       self.ticks += ticks_elapsed
       if self.ticks >= self.tick_skip - 1:   # 7 z 8
           self.update_controls(self.action)
       if self.ticks >= self.tick_skip:
           self.ticks = 0; self.update_action = True

   Decyzja policzona na poczatku okna osmiu tikow jest wysylana dopiero tik
   przed jego koncem. To wyrownuje opoznienie do warunkow z treningu. Nasz
   Manager wysylal akcje natychmiast po policzeniu - inna faza, a te modele
   sa wrazliwe wlasnie na timing flipow i kontaktu w powietrzu.

Mixin jest MODELO-AGNOSTYCZNY: `act()` wola `super().act()`, wiec dziala z
dowolnym Agentem z tej rodziny. Kolejnosc bazowych klas ma znaczenie:

    class KumoriRLBotAgent(RLBotTimingMixin, KumoriAgent): ...

MRO: mixin.act() -> KumoriAgent.act() (policzenie akcji siecia) -> Agent.

Uwaga: `bots/nexto_rlbot.py` ma wlasna, starsza kopie tej logiki i celowo
jej nie ruszam (dziala). Docelowo mozna go przepiac na ten mixin.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional, Tuple

import numpy as np

from prism_sdk.control import ControllerState
from prism_sdk.adapters.kickoff_speedflip import SpeedFlipKickoff
from prism_sdk.low import offsets as O


#: Sekwencja kickoffu z RLBotPack (Necto/Nexto/Kumori maja ja IDENTYCZNA -
#: porownane wiersz po wierszu z KICKOFF_CONTROLS w bot.py Kumori).
#: Kazdy wiersz to JEDEN tik gry (120 na sekunde), stad mnozniki *4.
_KICKOFF_STAGES = (
    (11, dict(throttle=1, boost=1)),
    (4,  dict(throttle=1, boost=1, steer=-1)),
    (2,  dict(throttle=1, boost=1, jump=1)),
    (1,  dict(throttle=1, boost=1)),
    (1,  dict(throttle=1, boost=1, jump=1, yaw=0.8, pitch=-0.7)),
    (13, dict(throttle=1, boost=1, pitch=1)),
    (10, dict(throttle=1, pitch=0.5, roll=1)),
)
_ORDER = ("throttle", "steer", "pitch", "yaw", "roll", "jump", "boost", "handbrake")


def _build_kickoff_table() -> np.ndarray:
    rows = []
    for repeat, fields in _KICKOFF_STAGES:
        row = [float(fields.get(k, 0)) for k in _ORDER]
        rows.extend([row] * (repeat * 4))
    return np.array(rows, dtype=np.float32)


KICKOFF_NUMPY = _build_kickoff_table()


class RLBotTimingMixin:
    """Faza tikow + skryptowany kickoff. Wstaw PRZED klasa modelu."""

    #: None = act() wolane co tik Managera. Okno osmiu tikow odmierzamy sami,
    #: bo inaczej nie da sie odtworzyc fazy "akcja wchodzi na 7. tiku".
    decision_hz = None

    TICK_SKIP = 8

    def __init__(self, *args, hardcoded_kickoffs=None, **kwargs):
        super().__init__(*args, **kwargs)
        # None = decyduje `kickoff_mode`. Domyslnie skryptu NIE MA:
        # kickoff rozgrywa siec swoimi wagami, tak jak reszte gry.
        self.hardcoded_kickoffs = (self.kickoff_mode != "off"
                                   if hardcoded_kickoffs is None
                                   else bool(hardcoded_kickoffs))
        self._ticks     = float(self.TICK_SKIP)   # pierwsza akcja liczona od razu
        self._prev_time: Optional[float] = None
        self._pending   = None                    # policzona akcja, czeka na wyslanie
        self._sent      = ControllerState.neutral()
        #: -1 = przed kickoffem, -2 = nie ja biore kickoff, >=0 = indeks
        self._kickoff_index = -1.0
        #: Ile tikow z rzedu odlozylismy decyzje o uzbrojeniu (patrz
        #: `_maybe_kickoff`). Zerowane po podjeciu decyzji.
        self._arm_deferred = 0
        #: Czy poprzedni tik nalezal do skryptu kickoffu (patrz `act`).
        self._in_kickoff = False
        #: Manewr speed flipa (tryb 'speedflip'). None = tabela albo brak.
        self._flip = None
        #: Slot startowy z chwili uzbrojenia (0=rog, 1=tyl, 2=gleboki tyl).
        self._my_slot = 0
        #: Diagnostyka pokrycia tabeli - ktore wiersze realnie wyszly.
        #: Wlaczane przez PRISM_KICKOFF_LOG=1.
        self._rows_sent: List[int] = []

    # ------------------------------------------------------------------
    # Zegar
    # ------------------------------------------------------------------
    def _elapsed_ticks(self) -> float:
        """Ile tikow gry (120 Hz) minelo od ostatniego wywolania - ULAMKOWO.

        NIE zaokraglamy do inta. Manager wola act() do 240 razy na sekunde,
        wiec delta * 120 ~ 0.5, a round(0.5) == 0 (zaokraglanie bankierskie).
        Przy incie licznik tikow nigdy by nie ruszyl z miejsca: bot policzylby
        JEDNA akcje i trzymal ja w nieskonczonosc. RLBot tego nie ma, bo
        dostaje delte z pakietu przy 120 Hz.
        """
        now = time.perf_counter()
        if self._prev_time is None:
            self._prev_time = now
            return 0.0
        delta = now - self._prev_time
        self._prev_time = now
        # Zabezpieczenie przed przerwa (breakpoint, zamrozenie gry) - bez tego
        # jeden dlugi skok przewinalby sekwencje kickoffu do konca.
        return min(delta * 120.0, float(self.TICK_SKIP))

    # ------------------------------------------------------------------
    # Detekcja kickoffu
    # ------------------------------------------------------------------
    #: Pilka na kickoffie stoi na srodku na wysokosci ~93 uu. Odczyt [0,0,0]
    #: to NIE kickoff, tylko nieudany odczyt pamieci.
    KICKOFF_BALL_XY_TOL = 5.0
    KICKOFF_BALL_Z_MIN  = 50.0
    KICKOFF_SPAWN_TOL   = 400.0

    #: Piec pozycji startowych kickoffu, |x| i |y| (lustrzane dla obu druzyn).
    #: Kolejnosc = rosnaca odleglosc od pilki, ktora na kickoffie ZAWSZE stoi
    #: w (0,0): rog 3278 uu, tyl 3848 uu, gleboki tyl 4608 uu. Uzywane sa
    #: 1 z 5 przy 1v1, 2 przy 2v2, 3 przy 3v3.
    _SPAWN_XY = ((2048.0, 2560.0), (256.0, 3840.0), (0.0, 4608.0))

    #: Ile tikow wolno czekac na spojny odczyt pozycji, zanim podejmiemy
    #: decyzje na tym, co jest. Patrz `_maybe_kickoff`.
    ARM_DEFER_TICKS = 6

    #: "speedflip" = manewr sterowany CZASEM i PREDKOSCIA (patrz
    #: kickoff_speedflip.py), odpalany tylko z naroznika. "table" = stara
    #: tabela 168 tikow z RLBotPacka, na kazdym spawnie.
    #:     PRISM_KICKOFF=table python run_sdk.py --run
    #: "off" (DOMYSLNIE) = zadnego skryptu, kickoff gra siec swoimi wagami.
    #: "speedflip" = manewr z kickoff_speedflip.py, tylko z naroznika.
    #: "table"     = tabela 168 tikow z RLBotPacka, na kazdym spawnie.
    #:     $env:PRISM_KICKOFF = "speedflip"   # PowerShell
    kickoff_mode = os.environ.get("PRISM_KICKOFF", "off").strip().lower()

    def _is_kickoff_pause(self) -> bool:
        """Odpowiednik packet.game_info.is_kickoff_pause.

        Runda aktywna, a pilka jeszcze nie zostala uderzona. Oba bity siedza
        w tym samym uint32 pod GameEvent+0x828.
        """
        world = self._ctx.world
        ge = getattr(world, "_game_event_addr", 0)
        if not ge:
            return False
        try:
            flags = world.driver.read_u32(ge + O.GameEventSoccar.STATE_FLAGS)
        except Exception:
            return False
        return (bool(flags & O.GameEventSoccar.FLAG_ROUND_ACTIVE)
                and not bool(flags & O.GameEventSoccar.FLAG_BALL_HAS_BEEN_HIT))

    def _my_car(self, world):
        """Nasze auto. self_car_index indeksuje world.cars (patrz AgentContext)."""
        try:
            return world.cars[self._ctx.self_car_index]
        except Exception:
            return None

    def _spawn_slot(self, pos_xy) -> Optional[int]:
        """Ktory z pieciu punktow startowych zajmuje auto (0=rog, 2=gleboki tyl).

        None = auto nie stoi na zadnym z nich, czyli albo nie jest to kickoff,
        albo odczyt pozycji jest nieaktualny. Rozroznienie "nie wiem" od
        "daleko" jest tu wazne - patrz `_maybe_kickoff`.
        """
        try:
            x = abs(float(pos_xy[0]))
            y = abs(float(pos_xy[1]))
        except Exception:
            return None
        for i, (sx, sy) in enumerate(self._SPAWN_XY):
            if (abs(x - sx) < self.KICKOFF_SPAWN_TOL
                    and abs(y - sy) < self.KICKOFF_SPAWN_TOL):
                return i
        return None

    def _at_kickoff_spawn(self, world) -> bool:
        car = self._my_car(world)
        if car is None:
            return False
        return self._spawn_slot(car.physics.position) is not None

    def _ball_on_kickoff_spot(self, world) -> bool:
        """Pilka realnie stoi na srodku - z sanity checkiem WYSOKOSCI.

        Sam warunek `abs(ball_y) < 1e-3` (jak w oryginale) jest za slaby:
        nieudany odczyt pamieci daje [0,0,0], ktore go spelnia, i skrypt
        kickoffu odpalal sie w srodku normalnej gry.
        """
        b = getattr(world, "ball", None)
        if b is None:
            return False
        try:
            p = b.position
            return (abs(float(p[0])) < self.KICKOFF_BALL_XY_TOL
                    and abs(float(p[1])) < self.KICKOFF_BALL_XY_TOL
                    and float(p[2]) > self.KICKOFF_BALL_Z_MIN)
        except Exception:
            return False

    def _team_slots(self, world):
        """[(slot, x, is_me)] dla aut MOJEJ druzyny stojacych na spawnie.

        Zwraca (lista, komplet), gdzie `komplet` mowi, czy KAZDE auto mojej
        druzyny udalo sie przypisac do punktu startowego. Niekompletny odczyt
        to sygnal, ze pozycje sa jeszcze sprzed resetu po golu - i lepiej
        poczekac tik, niz podjac na nich decyzje.
        """
        me_car = self._my_car(world)
        if me_car is None:
            return [], False
        my_team = None
        for p in world.players:
            car = getattr(p, "car", None)
            if car is not None and car.address == me_car.address:
                my_team = p.team_num
                break
        if my_team is None:
            return [], False

        out, complete = [], True
        for p in world.players:
            car = getattr(p, "car", None)
            if car is None or p.team_num != my_team:
                continue
            slot = self._spawn_slot(car.physics.position)
            if slot is None:
                complete = False
                continue
            out.append((slot, float(car.physics.position[0]),
                        car.address == me_car.address))
        return out, complete

    def _is_kickoff_taker(self, world) -> bool:
        """Kto jedzie na pilke - decyzja po PUNKCIE STARTOWYM, nie po dystansie.

        Wynik jest ten sam co u RLBota, bo piatka punktow startowych ma trzy
        rozne odleglosci od pilki (rog 3278, tyl 3848, gleboki tyl 4608), a
        pilka na kickoffie zawsze stoi w (0,0). "Najblizszy do pilki" to wiec
        po prostu "najnizszy numer slotu", tyle ze bez liczenia norm na
        pozycjach, ktore w pierwszym tiku po gwizdku bywaja jeszcze sprzed
        resetu po golu. Tolerancja 10 uu z oryginalu zamienia sie w rownosc
        slotow - dokladnie to, co ona miala oznaczac.

        Remis (dwoch kolegow na tej samej parze rogow/tylow) rozstrzyga ta
        sama regula co u nich: dla blue jedzie ten z wiekszym x, dla orange
        z mniejszym - strony sa lustrzane.
        """
        try:
            slots, _ = self._team_slots(world)
            me = next((e for e in slots if e[2]), None)
            if me is None or len(slots) < 2:
                return True          # sam na polowie - zawsze ja

            best = min(e[0] for e in slots)
            if me[0] != best:
                return False         # kolega startuje blizej pilki

            my_team = 0
            for p in world.players:
                car = getattr(p, "car", None)
                if car is not None and self._my_car(world) is not None                         and car.address == self._my_car(world).address:
                    my_team = p.team_num
                    break

            for slot, x, is_me in slots:
                if is_me or slot != me[0]:
                    continue
                other_is_left = (x < me[1]) if my_team == 0 else (x > me[1])
                if not other_is_left:
                    return False
            return True
        except Exception:
            return True      # w razie watpliwosci jedz - lepsze niz stanie

    def _maybe_kickoff(self, ticks_elapsed: float) -> bool:
        """Skryptowany kickoff. True = kickoff przejal sterowanie.

        Uzbrajany WYLACZNIE na zboczu wejscia w kickoff, konczony gdy
        sekwencja sie wyczerpie albo pilka ruszy.
        """
        world = self._ctx.world
        in_kickoff = (self._is_kickoff_pause()
                      and self._ball_on_kickoff_spot(world))
        if not in_kickoff:
            self._report_rows()
            self._kickoff_index = -1.0     # rozbroj, oddaj sterowanie sieci
            self._arm_deferred = 0
            self._flip = None
            return False

        if self._kickoff_index >= 0:
            self._kickoff_index += ticks_elapsed
            if self._flip is not None:
                return self._step_flip(ticks_elapsed)
        elif self._kickoff_index == -1:
            # Decyzja zapada RAZ i zatrzaskuje sie do konca okna: -2 nigdy nie
            # wraca do sekwencji. Dlatego nie wolno jej podjac na niespojnym
            # odczycie - flagi kickoffu czytamy na zywo z pamieci, a pozycje
            # aut pochodza z ostatniego `world.refresh()` i w pierwszym tiku po
            # gwizdku potrafia byc jeszcze sprzed resetu po golu. Objawem byl
            # kickoff oddany sieci mimo startu z rogu, czyli kickoff bez
            # speed flipa.
            #
            # Jesli nie wszystkie auta druzyny siedza na rozpoznanych punktach
            # startowych, odkladamy decyzje o tik. Krotko - `ARM_DEFER_TICKS`
            # tikow - bo spozniony start sekwencji jest gorszy niz jej brak:
            # tabela odpalilaby dodge w zlej odleglosci od pilki.
            slots, complete = self._team_slots(world)
            if not complete and self._arm_deferred < self.ARM_DEFER_TICKS:
                self._arm_deferred += 1
                return False
            self._arm_deferred = 0
            if not self._at_kickoff_spawn(world) or not self._is_kickoff_taker(world):
                self._kickoff_index = -2.0
            else:
                me = next((e for e in slots if e[2]), None)
                self._my_slot = me[0] if me else 0
                # Ograniczenie do naroznika ZDJETE: pod RLBotem speed flip
                # wychodzi takze ze spawnu srodkowego (potwierdzone
                # obserwacyjnie). Manewr jest zorientowany wzgledem nosa
                # auta i pilki, nie wzgledem pozycji na boisku.
                if False:
                    # Speed flip robi sie WYLACZNIE z naroznika. Ze spawnu
                    # tylnego i srodkowego tabela robila bezsensowny podskok
                    # w miejscu - Botimus rozwiazuje to tak samo, jego wariant
                    # ma w docstringu 'Works only on corner kickoffs'.
                    # Oddajemy kickoff sieci: pojedzie chociaz sensownie.
                    self._kickoff_index = -2.0
                else:
                    self._kickoff_index = 0.0
                    if self.kickoff_mode == 'speedflip':
                        self._flip = SpeedFlipKickoff()
                        return self._step_flip(ticks_elapsed)

        if 0 <= self._kickoff_index < len(KICKOFF_NUMPY):
            row = int(self._kickoff_index)
            self._rows_sent.append(row)
            self._sent = ControllerState.from_array(KICKOFF_NUMPY[row])
            return True
        self._report_rows()
        return False        # sekwencja wyczerpana - reszte dogrywa siec

    def _step_flip(self, ticks_elapsed: float) -> bool:
        """Jeden tik manewru speed flipa. False = manewr zakonczony."""
        world = self._ctx.world
        car = self._my_car(world)
        if car is None or self._flip is None:
            return False
        try:
            vel = np.asarray(car.physics.linear_velocity, dtype=np.float64)
            speed = float(np.linalg.norm(vel))
            by = SpeedFlipKickoff.ball_local_y(
                car.physics.position, car.physics.rotation_matrix,
                world.ball.position)
            on_ground = bool(getattr(car, "is_on_ground", True))
        except Exception:
            return False

        # dt w SEKUNDACH. Manewr jest opisany czasem, a nie numerem tiku,
        # wiec zaciecie petli go opoznia, ale nie przeskakuje mu fazy -
        # tabela przy tym samym zacieciu gubila cale okno 4-tikowe.
        self._sent = self._flip.step(ticks_elapsed / 120.0, speed, by, on_ground)
        if self._flip.finished:
            self._flip = None
            self._kickoff_index = -2.0   # reszte kickoffu dogrywa siec
            return False
        return True

    #: Etapy, ktorych uciecie psuje speed flipa - patrz `act`.
    _CRITICAL = ((68, 72, "puszczenie skoku"), (72, 76, "dodge"),
                 (76, 84, "poczatek cancela"))

    def _report_rows(self) -> None:
        """Podsumuj, ile z tabeli realnie wyszlo do gry (PRISM_KICKOFF_LOG=1).

        Sama liczba wierszy nie wystarcza - liczy sie NAJWIEKSZA DZIURA. Etapy
        krytyczne maja po cztery tiki, wiec przeskok o 3 potrafi ktorys z nich
        pominac w calosci, a suma i tak bedzie wygladac zdrowo.
        """
        rows, self._rows_sent = self._rows_sent, []
        if not rows or not os.environ.get("PRISM_KICKOFF_LOG"):
            return
        gap = max((b - a for a, b in zip(rows, rows[1:])), default=0)
        parts = []
        for lo, hi, name in self._CRITICAL:
            n = sum(1 for r in rows if lo <= r < hi)
            parts.append(f"{name}={n}/{hi - lo}")
        print(f"[kickoff] wierszy {len(rows)} ({rows[0]}..{rows[-1]}), "
              f"najwieksza dziura {gap}, " + ", ".join(parts))

    # ------------------------------------------------------------------
    def act(self) -> ControllerState:
        ticks = self._elapsed_ticks()

        # 1) KICKOFF PRZED SIECIA, z wyjsciem od razu.
        #
        # Wczesniej bylo odwrotnie: `super().act()` (forward pass sieci)
        # liczyl sie zawsze, a kickoff nadpisywal wynik linijke nizej. Wynik
        # sieci szedl wiec do kosza, ale jej KOSZT zostawal - i to on psul
        # speed flipa.
        #
        # `_elapsed_ticks()` mierzy zegar scienny. Inferencja torcha trwa
        # kilkanascie milisekund, wiec gdy wypadnie w srodku kickoffu, petla
        # sie zacina i nastepny odczyt zwraca 2-3 tiki naraz. `_kickoff_index`
        # przeskakuje wtedy wiersze tabeli. Etap puszczenia skoku (68..71) i
        # etap dodge'a (72..75) maja po CZTERY tiki, czyli 33 ms - jeden stall
        # potrafi je uciac. Przy przeskoczonym puszczeniu gra nie zobaczy
        # drugiego wcisniecia i nie ma dodge'a; przy przycietym poczatku
        # etapu `pitch=1` flip nie zostaje anulowany i auto obraca sie dalej.
        #
        # RLBot tego nie ma, bo dostaje dokladnie jedno wywolanie na tik gry.
        # My mamy zegar, wiec jedyne wyjscie to nie stac w miejscu: w trakcie
        # kickoffu siec jest niepotrzebna, wiec jej nie wolamy.
        if self.hardcoded_kickoffs and self._maybe_kickoff(ticks):
            self._in_kickoff = True
            return self._sent

        if self._in_kickoff:
            # Wyjscie z kickoffu. Okno tikow stalo przez cala sekwencje, wiec
            # zaczynamy je od zera - i to od razu z decyzja, zeby siec przejela
            # sterowanie w tym samym ticku, a nie po kolejnych osmiu.
            self._in_kickoff = False
            self._ticks = float(self.TICK_SKIP)
            self._pending = None

        self._ticks += ticks

        # 2) Nowa decyzja na poczatku okna - liczymy, ale JESZCZE nie wysylamy.
        if self._pending is None:
            self._pending = super().act()

        # 3) Akcja wchodzi tik przed koncem okna (u nich: ticks >= tick_skip-1).
        if self._ticks >= self.TICK_SKIP - 1 and self._pending is not None:
            self._sent = self._pending

        # 4) Zamkniecie okna - nastepny act() policzy nowa akcje.
        if self._ticks >= self.TICK_SKIP:
            self._ticks = 0
            self._pending = None

        return self._sent
