"""
HumanizerAgent v2 - per-tick INTERPOLACJA zamiast smoothingu.

Poprzednia wersja (EMA) tlumila skoki, ale kosztem skilla:
  raw Nexto: throttle 1.0 -> humanizer wysylal 0.55 (avg z prev)
  bot nigdy nie mial pelnego throttle -> tracil predkosc.

Wersja 2: INTERPOLACJA miedzy decyzjami Nexto (15Hz) w tempie tickow Managera
(120Hz). Nexto co 66ms daje TARGET, humanizer w ciagu 8 klatek dochodzi z
poprzedniej wartosci do tego targetu. Bot OSIAGA decyzje sieci (skill 100%),
ale plynnie - bez teleportacji z -1 do +1 w jednym ticku.

Kluczowa roznica:
  EMA:   out = alpha*target + (1-alpha)*prev   [nigdy nie osiaga target]
  LERP:  out = start + (target-start) * min(1, elapsed/period)  [osiaga w 66ms]

Uzycie w launcherze:
    from prism_sdk.adapters.humanizer import HumanizerAgent
    from prism_sdk.adapters import NextoAgent
    agent = HumanizerAgent(NextoAgent())

Parametry:
    interp_scale - mnoznik dlugosci interpolacji (default 1.0 = pelne decision_period).
                   0.5 = interpoluj w polowie czasu (szybsze, mniej smooth).
                   1.5 = 1.5x wolniej (bardziej smooth, ale target "pozostaje w tyle").
    ease         - "linear" (default), "smoothstep" (3p^2-2p^3), "ease_out" (1-(1-p)^2)
    dead_zone    - male wartosci -> 0 (default 0.03)
    verbose      - loguj interpolacje
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from prism_sdk.agent import Agent, AgentContext
from prism_sdk.control import ControllerState


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ease_linear(t: float) -> float:
    return t


def _ease_smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _ease_out(t: float) -> float:
    # 1 - (1-t)^2 - szybki start, powolne konczenie
    inv = 1.0 - t
    return 1.0 - inv * inv


_EASES = {
    "linear": _ease_linear,
    "smoothstep": _ease_smoothstep,
    "ease_out": _ease_out,
}


class HumanizerAgent(Agent):
    """Wrapper agenta ktory INTERPOLUJE miedzy decyzjami base.

    Aktywnosc:
      * Manager wola act() co tick (bo decision_hz=None u nas)
      * Wewnetrznie odmierzam bazowy decision_period (od decision_hz base)
      * Co period wolam base.observe + base.act -> nowy TARGET
      * Miedzy: interpoluje od start (poprzedni output) do TARGET, tempo
        zgodne z ease() i elapsed/(period * interp_scale)
    """

    #: Decision_hz=None = Manager wola act() co tick (120Hz przy default --hz)
    decision_hz = None

    def __init__(self, base: Agent,
                 ground_scale: float = 1.0,
                 air_scale: float = 0.18,
                 ground_ease: str = "linear",
                 air_ease: str = "ease_out",
                 air_axes_passthrough: bool = False,
                 dead_zone: float = 0.04,
                 verbose: bool = False):
        """
        ground_scale         - interp_scale gdy auto na ziemi (default 1.0 = 66ms).
                               Pelne 66ms = plynne skrety, throttle nie skacze.
        air_scale            - interp_scale gdy auto w powietrzu (default 0.18 = 12ms).
                               Krotka interpolacja = wygladza wizualny skok, ale osiaga
                               target Nexto w 1-2 klatkach @ 120Hz. Nexto sieciowa
                               dostaje reakcje niemal natychmiast, nie generuje dodge'ow.
        ground_ease          - "linear" (default). Ground handling najbardziej przewidywalne
                               gdy interpolacja idzie stalym tempem.
        air_ease             - "ease_out" (default). Szybki start, powolne dojechanie -
                               siec widzi zmiane od razu (aerial timing dziala), ale wizualnie
                               plynne bo koncowe klatki mniejsze zmiany. Trzy opcje:
                               linear/smoothstep/ease_out.
        air_axes_passthrough - True = w powietrzu ZERO smoothingu (natychmiast target).
                               Domyslnie False bo ease_out + air_scale=0.18 daje smoothing
                               bez utraty skilla. Ustaw True gdy dalej flipuje w bok.
        dead_zone            - male wartosci zerowane (default 0.04).
        """
        super().__init__(name=f"Humanized({base.name})")
        self.base = base

        # Bazowy decision_period (sekundy) - od base.decision_hz
        base_hz = getattr(base, "decision_hz", None)
        if base_hz is None or base_hz <= 0:
            # Base myslisz co tick -> humanizer nic nie interpoluje, pass-through
            self._base_period = 0.0
        else:
            self._base_period = 1.0 / base_hz

        self.ground_scale = ground_scale
        self.air_scale = air_scale
        self.ground_ease_fn = _EASES.get(ground_ease, _ease_linear)
        self.air_ease_fn    = _EASES.get(air_ease,    _ease_out)
        self.air_axes_passthrough = air_axes_passthrough
        self.dead_zone = dead_zone
        self.verbose = verbose

        # Runtime state - inicjalizowane przez _init_state (te same pola zerowane
        # tez w on_attach() i reset())
        self._init_state()

    def _init_state(self):
        """Reset wewnetrznego stanu interpolacji (init/attach/reset)."""
        self._t_last_decision = 0.0
        self._start = ControllerState.neutral()
        self._target = ControllerState.neutral()
        self._prev_out = ControllerState.neutral()
        self._decisions = 0
        self._first_call = True

    def set_param(self, key: str, value) -> bool:
        """Runtime setter dla parametrow humanizera (thread-safe: pojedyncze
        przypisanie atomic w Pythonie). Uzywane przez PipeServer -> PrismUI
        slidery. Zwraca True gdy klucz znany."""
        if key == "air_scale":
            self.air_scale = float(value)
        elif key == "ground_scale":
            self.ground_scale = float(value)
        elif key == "dead_zone":
            self.dead_zone = float(value)
        elif key == "air_axes_passthrough":
            self.air_axes_passthrough = bool(value)
        elif key == "ground_ease":
            self.ground_ease_fn = _EASES.get(str(value), _ease_linear)
        elif key == "air_ease":
            self.air_ease_fn = _EASES.get(str(value), _ease_out)
        else:
            return False
        return True

    # -- lifecycle: deleguj do base --

    def on_attach(self, ctx: AgentContext) -> None:
        super().on_attach(ctx)
        self.base.on_attach(ctx)
        self._init_state()

    def on_detach(self) -> None:
        self.base.on_detach()
        super().on_detach()

    def observe(self, ctx: AgentContext) -> None:
        # Zapamietaj kontekst - base.observe() wolamy PIERO gdy jest nowa decyzja
        # (oszczedza CPU: Nexto obs_builder to droga operacja co tick)
        self._ctx = ctx

    def reset(self) -> None:
        """Wolane przez Manager po pauzie/respawnie - wyczysc interpolacje."""
        self._init_state()
        reset_fn = getattr(self.base, "reset", None)
        if callable(reset_fn):
            reset_fn()

    # -- glowna logika --

    def _apply_dead_zone(self, x: float) -> float:
        return 0.0 if abs(x) < self.dead_zone else x

    def _clamp(self, x: float) -> float:
        if x > 1.0: return 1.0
        if x < -1.0: return -1.0
        return x

    def _car_on_ground(self) -> bool:
        """True gdy nasze auto jest na ziemi. Default True gdy nie wiemy
        (bezpiecznie - na ziemi silniejsze smoothing = mniejsze ryzyko dziwnych
        akcji jesli detekcja zawiedzie)."""
        try:
            car = self._ctx.world.cars[self._ctx.self_car_index]
            return bool(getattr(car, "is_on_ground", True))
        except Exception:
            return True

    def act(self) -> ControllerState:
        now = time.perf_counter()

        # 1) Czy czas na nowa decyzje base?
        need_new_target = (
            self._first_call
            or self._base_period <= 0
            or (now - self._t_last_decision) >= self._base_period
        )

        if need_new_target:
            # Pytaj base o nowa decyzje. Base widzi swiat DOPIERO teraz (raz na 66ms),
            # nie co tick - stad drastyczna oszczednosc CPU na obs_builder.
            try:
                self.base.observe(self._ctx)
                raw = self.base.act()
                if not isinstance(raw, ControllerState):
                    # Base zwrocil cos zlego - trzymaj poprzedni target
                    raw = self._target
            except Exception as e:
                print(f"[Humanizer] base.act() error: {e}")
                raw = self._target

            # Nowy target. Interpolacja zaczyna od aktualnego OUTPUT (nie od
            # poprzedniego targetu) - jesli poprzednia interpolacja nie dotarla
            # do konca (Nexto szybko zmienil zdanie), continue od tego gdzie
            # jestesmy, nie skacz.
            self._start = self._prev_out
            self._target = raw
            self._t_last_decision = now
            self._decisions += 1
            self._first_call = False
            elapsed = 0.0
        else:
            elapsed = now - self._t_last_decision

        # 2) Ground vs Air - inne interpolacje (scale + ease)
        on_ground = self._car_on_ground()
        if on_ground:
            scale = self.ground_scale
            ease_fn = self.ground_ease_fn
        else:
            scale = self.air_scale
            ease_fn = self.air_ease_fn

        if self._base_period > 0 and scale > 0:
            interp_duration = self._base_period * scale
            progress = min(1.0, elapsed / interp_duration)
        else:
            progress = 1.0  # scale=0 albo brak base_period -> natychmiast target

        alpha = ease_fn(progress)

        # 3) Osi analog - interpoluj (throttle/steer maja sens tylko na ziemi)
        thr = self._lerp_axis(self._start.throttle, self._target.throttle, alpha)
        ste = self._lerp_axis(self._start.steer,    self._target.steer,    alpha)

        # W powietrzu z passthrough - pitch/yaw/roll skacza (max skill, zero smooth)
        if not on_ground and self.air_axes_passthrough:
            pit = self._apply_dead_zone(self._clamp(self._target.pitch))
            yaw = self._apply_dead_zone(self._clamp(self._target.yaw))
            rol = self._apply_dead_zone(self._clamp(self._target.roll))
        else:
            # Default: short interpolacja z ease_out - wygladza skok, osiaga target
            # w 1-2 klatkach @ 120Hz. Nexto dostaje reakcje niemal natychmiast.
            pit = self._lerp_axis(self._start.pitch, self._target.pitch, alpha)
            yaw = self._lerp_axis(self._start.yaw,   self._target.yaw,   alpha)
            rol = self._lerp_axis(self._start.roll,  self._target.roll,  alpha)

        # 3) Bity - DYSKRETNE, biore z target BEZ debounce.
        # Debounce (jak w v1) blokowal flipy - Nexto ma pattern jump->release->jump
        # co 66ms, min_hold=90ms wyrzucal drugi jump = fail flip. Zaufaj sieci.
        out = ControllerState(
            throttle=thr, steer=ste, pitch=pit, yaw=yaw, roll=rol,
            jump=self._target.jump,
            boost=self._target.boost,
            handbrake=self._target.handbrake,
        )

        self._prev_out = out

        if self.verbose and need_new_target:
            print(f"[Humanizer] decision #{self._decisions} target={self._target}")

        return out

    def _lerp_axis(self, a: float, b: float, t: float) -> float:
        v = _lerp(a, b, t)
        v = self._apply_dead_zone(v)
        return self._clamp(v)
