"""
Agent base class + wspolny kontekst.

Kontrakt Agenta:
    * `observe(ctx)`  - dostaje snapshot swiata i informacje o wlasnym aucie
    * `act()`         - zwraca ControllerState

Cykl zycia:
    on_attach(ctx)     - raz na starcie (agent wie kim jest w swiecie)
    for each tick:
        observe(ctx)   - odswiez swoja wewnetrzna reprezentacje
        act() -> cs    - policz akcje
    on_detach()        - koniec, zwolnij zasoby
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from prism_sdk.control import ControllerState


@dataclass
class AgentContext:
    """Snapshot swiata + ktore auto kontrolujemy.

    Trzymamy referencje a nie kopie - bot moze przechodzic po
    ctx.world.players[] itd. World jest ciagle refresh'owany przez Manager
    - miedzy `observe` a `act` bot ma spojny snapshot bo Manager wywoluje je
    natychmiast po sobie w jednym tick'u.
    """
    world: Any                    # prism_sdk.world.World - avoiding circular import
    self_car_index: int           # ktore auto (indeks w world.cars) kontrolujemy
    self_player_index: int        # ktore player (indeks w world.players) to my
    tick: int                     # numer klatki od attach'a
    dt: float                     # czas od poprzedniego ticku (sekundy)


class Agent:
    """Bazowa klasa agenta.

    Bot dziedziczy i implementuje act(). Domyslne observe() nic nie robi -
    wiekszosc botow po prostu odczytuje `self._ctx.world` w act().

    Dodatkowe informacje jesli bot potrzebuje historii miedzy tickami:
    trzymaj je jako self._foo - klasa jest per-instancja per-run.
    """

    #: Wyswietlana nazwa (do logow)
    name: str = "UnnamedAgent"

    #: Jak czesto agent ma PODEJMOWAC DECYZJE (None = co tick Managera).
    #:
    #: KRYTYCZNE dla wytrenowanych sieci. Nexto/Opti trenowane sa z
    #: tick_skip=8 przy 120 fps gry, czyli JEDNA decyzja co 8 klatek fizyki
    #: (~15 Hz), trzymana przez cale 8 klatek. Odpalanie act() co tick przy
    #: 240 Hz to 16x czestsza decyzja niz w treningu: akcja migocze miedzy
    #: klatkami, a jump/dodge (ktore wymagaja przytrzymania i czystej krawedzi
    #: press/release przez kilka klatek fizyki) rozsypuja sie - flipy sie nie
    #: odpalaja albo lecą w zla strone.
    #:
    #: Manager wysyla ostatnia decyzje do gry CO TICK (240 Hz) - zmienia sie
    #: tylko czestotliwosc myslenia, nie czestotliwosc sterowania.
    decision_hz: Optional[float] = None

    def __init__(self, name: Optional[str] = None):
        if name is not None:
            self.name = name
        self._ctx: Optional[AgentContext] = None

    # -- lifecycle hooks (opcjonalne) --

    def on_attach(self, ctx: AgentContext) -> None:
        """Wolane raz przez Manager przy podpieciu agenta."""
        self._ctx = ctx

    def on_detach(self) -> None:
        """Zwolnij zasoby (torch model, otwarte pliki itp.)."""
        self._ctx = None

    # -- per-tick --

    def observe(self, ctx: AgentContext) -> None:
        """Odswiez wewnetrzny stan. Domyslnie - zapamietaj tylko referencje."""
        self._ctx = ctx

    def act(self) -> ControllerState:
        """Policz akcje. MUSI byc nadpisane."""
        raise NotImplementedError(f"{self.name}.act() nie zaimplementowany")

    # -- utilities --

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
