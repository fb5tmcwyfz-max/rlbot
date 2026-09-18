"""
Manager - orkiestruje wielu agentow naraz.

Model:
    * Manager trzyma pojedynczy World (jeden odczyt pamieci na tick, wspoldzielony
      dla wszystkich agentow)
    * Kazdy agent ma przypisany car_index (auto ktore steruje)
    * Petla: refresh world -> dla kazdego agenta observe+act -> wyslij do gry
    * Target Hz ustawiane per-run (140 dla SSL bot, 60 dla testow)

Sender jest injected - MVP moze uzyc dummy sendera (print), pozniej podłaczy
sie kernel autowrite/hook.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from prism_sdk.agent.base import Agent, AgentContext
from prism_sdk.control import ControllerState
from prism_sdk.world.play_state import detect_play_state


# Sender to funkcja: (car_index, ControllerState) -> None
Sender = Callable[[int, ControllerState], None]


def _print_sender(car_index: int, cs: ControllerState) -> None:
    """Dummy sender - loguje akcje zamiast wysylac. Do testow bez hooka."""
    print(f"    car[{car_index}] <- {cs}")


@dataclass
class _Binding:
    agent: Agent
    car_index: int
    player_index: int
    #: Ostatnia policzona akcja - wysylana co tick az do nastepnej decyzji
    cached: Optional[ControllerState] = None
    last_decision_t: float = 0.0
    decisions: int = 0
    sends: int = 0


class Manager:
    """Orkiestrator wielu agentow.

    Uzycie:
        world = World()
        world.attach()

        mgr = Manager(world, sender=hook_sender)
        mgr.bind(agent1, car_index=0)
        mgr.bind(agent2, car_index=1)
        mgr.run(hz=140)     # blokuje do Ctrl+C
    """

    def __init__(self, world, sender: Optional[Sender] = None,
                 decision_hz: Optional[float] = None,
                 gate_on_play_state: bool = True,
                 #: Co tick przepinaj binding na aktualne auto lokalnego
                 #: gracza. Wylacz tylko gdy celowo sterujesz cudzym autem.
                 follow_local: bool = True,
                 # Bylo 20 Hz "zeby nie mielic pamieci na sucho". Efekt uboczny:
                 # po replayu/golu powrot do sterowania byl odczuwalnie wolny,
                 # bo Manager zauwazal koniec pauzy dopiero przy nastepnym
                 # odczycie. Odpytanie pamieci jest tanie, a opoznienie startu
                 # kosztuje realna gre - wiec monitorujemy pelnym tempem.
                 idle_hz: float = 120.0,
                 #: Pauza z ZEWNATRZ (przycisk w launcherze). Predykat wolany
                 #: raz na tick; True = oddaj sterowanie czlowiekowi i nie
                 #: dotykaj gry, dopoki nie wroci False.
                 pause_source: Optional[Callable[[], bool]] = None):
        """decision_hz nadpisuje `agent.decision_hz` dla WSZYSTKICH agentow
        (None = kazdy agent decyduje wg swojego ustawienia).

        gate_on_play_state=True: gdy nie jestesmy realnie w grze (menu, replay,
        odliczanie, ekran koncowy) Manager NIE wola agenta i wysyla neutral.
        Zamiast mielic 240 Hz na sucho, przechodzi na `idle_hz` i czeka az
        wrocimy na boisko. Wylacz tylko do debugowania samej petli.
        """
        self.world = world
        self.sender: Sender = sender or _print_sender
        self.decision_hz_override = decision_hz
        self.gate_on_play_state = gate_on_play_state
        self.follow_local = follow_local
        self.idle_hz = idle_hz
        self.pause_source = pause_source
        self._bindings: List[_Binding] = []
        self._stop = False
        self._tick = 0

        # Stan bramki - do logowania przejsc zamiast spamu co tick
        self._last_state = None
        self._idle_since = 0.0
        self._last_idle_log = 0.0
        self.idling = False
        #: Czy poprzedni tick byl w pauzie zewnetrznej - zeby wejscie i
        #: wyjscie obsluzyc raz, na zboczu, a nie co tick.
        self._paused_ext = False

    def _decision_period(self, agent: Agent) -> float:
        """Sekundy miedzy decyzjami agenta. 0 = decyduj co tick."""
        hz = self.decision_hz_override
        if hz is None:
            hz = getattr(agent, "decision_hz", None)
        if hz is None or hz <= 0:
            return 0.0
        return 1.0 / hz

    # -- binding agentow --

    def _resync_local_binding(self) -> None:
        """Ustaw car_index/player_index na AKTUALNE auto lokalnego gracza.

        Wolane co tick. Tanie: to jeden odczyt wskaznika + porownanie adresow
        na juz wczytanej liscie graczy.
        """
        from prism_sdk.adapters._rlgym_shim import resolve_self_player_index

        try:
            pi = resolve_self_player_index(self.world)
            if pi is None or pi >= len(self.world.players):
                return
            addr = self.world.players[pi].car_address
            if not addr:
                return
            ci = next((k for k, c in enumerate(self.world.cars)
                       if c.address == addr), None)
            if ci is None:
                return
            for b in self._bindings:
                if b.car_index != ci or b.player_index != pi:
                    print(f"[Manager] przepinam na aktualne auto: "
                          f"car[{b.car_index}]->car[{ci}] player[{b.player_index}]->player[{pi}]")
                    b.car_index, b.player_index = ci, pi
                    # Poprzednia akcja dotyczyla innego auta - nie wysylaj jej.
                    b.cached = None
        except Exception:
            # Chwilowy brak danych nie moze wywalic petli sterowania.
            pass

    def bind(self, agent: Agent, car_index: int, player_index: Optional[int] = None) -> None:
        """Podepnij agenta do konkretnego auta. Jesli player_index=None,
        Manager sprobuje znalezc gracza dla tego auta w World.players[].
        """
        pi = player_index if player_index is not None else car_index
        self._bindings.append(_Binding(agent=agent, car_index=car_index, player_index=pi))

    def unbind_all(self) -> None:
        for b in self._bindings:
            try:
                b.agent.on_detach()
            except Exception:
                pass
        self._bindings.clear()

    # -- petla --

    def stop(self) -> None:
        self._stop = True

    def _release_control(self) -> None:
        """Oddaj sterowanie czlowiekowi, ale NIE rozbieraj sendera.

        Roznica wobec `sender.close()`: tam kanal jest zamykany na dobre (patch
        odinstalowany, sloty kernela zatrzymane), wiec powrot kosztuje tyle co
        start bota. Tu chodzi o pauze - kanal ma zostac gotowy, ma zniknac
        tylko NASZ input.

        Sam neutral NIE wystarcza. Przy kanale PlayerControllera gra bierze
        input z OverrideInput dopoki stoi bOverrideInput - z neutralem w srodku
        auto stoi, ale czlowiek tez nie jedzie, bo jego klawiatura jest
        nadpisywana zerami. Dlatego po neutralu zdejmujemy jeszcze flage.
        """
        for b in self._bindings:
            b.cached = None
            try:
                self.sender(b.car_index, ControllerState.neutral())
            except Exception:
                pass
        # Senderow jest kilka i flaga siedzi u kazdego gdzie indziej: HookSender
        # ma release() sam, PCSender trzyma kanal w .channel, Patch/KernelSender
        # w .pc. Dummy sender (print) nie ma zadnego - stad getattr.
        for owner in (self.sender, getattr(self.sender, "channel", None),
                      getattr(self.sender, "pc", None)):
            release = getattr(owner, "release", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    pass

    def _external_pause(self) -> bool:
        """Obsluz pauze z launchera. True = ten tick nalezy do czlowieka.

        Sprawdzane PRZED `world.refresh()`, bo odczyt swiata to ~7 ms - bot w
        pauzie ma nie kosztowac nic. Wejscie i wyjscie robimy na zboczu:
        w srodku pauzy nie dotykamy gry w ogole, zeby nie walczyc z inputem
        gracza co tick.
        """
        if self.pause_source is None:
            return False
        try:
            want = bool(self.pause_source())
        except Exception:
            want = False          # zerwana komunikacja nie moze zablokowac bota
        if want and not self._paused_ext:
            self._paused_ext = True
            self.idling = True
            print("[Manager] pauza z launchera - sterowanie oddane graczowi")
            self._release_control()
        elif not want and self._paused_ext:
            self._paused_ext = False
            self.idling = False
            print("[Manager] wznowienie z launchera")
            # Stan agenta jest sprzed pauzy - dokladnie ta sama sytuacja co po
            # przerwie na bramce stanu gry, wiec sprzatamy tak samo.
            for b in self._bindings:
                b.cached = None
                reset = getattr(b.agent, "reset", None)
                if callable(reset):
                    try:
                        reset()
                    except Exception:
                        pass
        return self._paused_ext

    def tick_once(self, dt: float) -> bool:
        """Jeden przebieg petli. Zwraca False gdy nie udalo sie odswiezyc swiata
        (menu, brak procesu itp.) - wolajacy moze sleep i sprobowac ponownie.

        Decyzja vs wysylka: agent z `decision_hz` mysli rzadziej (Nexto: 15 Hz
        = jego tick_skip=8), ale ostatnia akcja leci do gry KAZDY tick, zeby
        kernel buffer nigdy nie byl przeterminowany i zeby jump/dodge trwaly
        tyle klatek fizyki, ile trwaly w treningu.
        """
        if self._external_pause():
            self._tick += 1
            return True

        if not self.world.refresh():
            return False

        now = time.perf_counter()

        # -- BRAMKA: czy realnie jestesmy w grze --
        if self.gate_on_play_state:
            status = detect_play_state(self.world)
            if status.state is not self._last_state:
                first = self._last_state is None
                if status.can_control and first:
                    # Pierwszy odczyt po starcie to nie jest "wznowienie" -
                    # nie ma czego resetowac i nie ma o czym informowac.
                    pass
                elif status.can_control:
                    waited = (now - self._idle_since) if self._idle_since else 0.0
                    print(f"[Manager] wznawiam sterowanie"
                          + (f" (przerwa {waited:.1f}s)" if waited > 0.5 else ""))
                    # Po przerwie stan skryptow/rutyn jest nieaktualny
                    for b in self._bindings:
                        b.cached = None
                        reset = getattr(b.agent, "reset", None)
                        if callable(reset):
                            try:
                                reset()
                            except Exception:
                                pass
                else:
                    print(f"[Manager] pauza: {status.describe()} - czekam na powrot na boisko")
                    self._idle_since = now
                    self._last_idle_log = now
                self._last_state = status.state

            self.idling = not status.can_control
            if self.idling:
                # Neutral raz na wejsciu w pauze - zeby nic nie zostalo wcisniete.
                # Potem juz nic nie wysylamy: sender sam wykrywa brak pawna,
                # a przy odliczaniu gra i tak ignoruje input.
                for b in self._bindings:
                    if b.cached is not None:
                        b.cached = None
                        try:
                            self.sender(b.car_index, ControllerState.neutral())
                        except Exception:
                            pass
                # Heartbeat co 10 s, zeby bylo widac ze bot zyje i patrzy
                if now - self._last_idle_log > 10.0:
                    print(f"[Manager] nadal pauza ({status.describe()}, "
                          f"{now - self._idle_since:.0f}s)")
                    self._last_idle_log = now
                self._tick += 1
                return True

        # ------------------------------------------------------------------
        # PRZEPINANIE NA ZYWO - bez tego bot po pierwszym respawnie steruje
        # CUDZYM autem.
        # ------------------------------------------------------------------
        # bind() zapisuje car_index raz, przy starcie. Gra po golu/demolce
        # tworzy NOWY obiekt auta pod nowym adresem (widac to w logu jako
        # "[PatchSender] local pawn -> 0x..." ze zmieniona wartoscia), a lista
        # world.cars jest budowana od nowa - wiec indeks 4 zaczyna wskazywac
        # kogos innego. Objaw: bot gra poprawnie do pierwszego gola, a potem
        # "jezdzi w kolko w zupelnie innym miejscu", bo obserwuje i steruje
        # nie swoim autem.
        if self.follow_local:
            self._resync_local_binding()

        for b in self._bindings:
            period = self._decision_period(b.agent)
            need_decision = (b.cached is None or period <= 0.0
                             or (now - b.last_decision_t) >= period)
            if need_decision:
                # dt widziany przez agenta = czas od JEGO poprzedniej decyzji,
                # nie od poprzedniego ticku Managera.
                agent_dt = dt if b.cached is None else (now - b.last_decision_t)
                ctx = AgentContext(
                    world=self.world,
                    self_car_index=b.car_index,
                    self_player_index=b.player_index,
                    tick=self._tick,
                    dt=agent_dt,
                )
                try:
                    b.agent.observe(ctx)
                    cs = b.agent.act()
                    if isinstance(cs, ControllerState):
                        b.cached = cs.clamp()
                        b.decisions += 1
                    # agent zwrocil cos zlego -> trzymamy poprzednia akcje
                except Exception as e:
                    print(f"[Manager] agent {b.agent.name} tick {self._tick} error: {e}")
                b.last_decision_t = now

            if b.cached is not None:
                self.sender(b.car_index, b.cached)
                b.sends += 1

        self._tick += 1
        return True

    def run(self, hz: float = 140.0) -> None:
        """Uruchom petle glowna. Blokuje do Ctrl+C lub stop()."""
        # Attach agents jednokrotnie na starcie
        if not self.world.refresh():
            print("[Manager] World.refresh() failed - czy driver zaladowany + RL uruchomiony?")
            return

        for b in self._bindings:
            ctx = AgentContext(
                world=self.world,
                self_car_index=b.car_index,
                self_player_index=b.player_index,
                tick=0,
                dt=0.0,
            )
            try:
                b.agent.on_attach(ctx)
            except Exception as e:
                print(f"[Manager] on_attach({b.agent.name}) error: {e}")
            period = self._decision_period(b.agent)
            rate = f"{1.0/period:.1f} Hz" if period > 0 else f"{hz:.0f} Hz (co tick)"
            # Supervisor chodzi co tick, ale siec pod nim throttluje sie sama -
            # bez tego log klamie, ze Nexto mysli 240 razy na sekunde.
            base = getattr(b.agent, "base", None)
            base_hz = getattr(base, "decision_hz", None) if base is not None else None
            if base_hz:
                rate += f", w tym {base.name} @ {base_hz:g} Hz"
            print(f"[Manager] {b.agent.name}: decyzje @ {rate}, "
                  f"wysylka do gry @ {hz:.0f} Hz")
            if self.gate_on_play_state:
                print(f"[Manager] bramka stanu gry ON - pauza w menu/replayu/"
                      f"odliczaniu, monitoring @ {self.idle_hz:g} Hz")

        interval = 1.0 / max(hz, 1e-6)
        prev_t = time.perf_counter()
        self._stop = False
        try:
            while not self._stop:
                now = time.perf_counter()
                dt = now - prev_t
                prev_t = now
                self.tick_once(dt)
                elapsed = time.perf_counter() - now
                # W pauzie zwalniamy - nie ma po co odpytywac pamieci 240x/s,
                # gdy czekamy na powrot do meczu.
                step = interval if not self.idling else (1.0 / max(self.idle_hz, 1.0))
                remaining = step - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            print("\n[Manager] stopped by Ctrl+C")
        finally:
            self.unbind_all()
