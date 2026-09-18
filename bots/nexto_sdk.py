"""
Nexto v2 na prism_sdk - sterowanie z pamieci gry, taktowanie jak w RLBocie.

Klucz `--bot nexto` (patrz `run_sdk.py`).

CO DOKLADNIE JEST "JAK W RLBOCIE"
--------------------------------
Siec i wybor akcji byly identyczne juz wczesniej (te same wagi
`nexto-model.pt`, ta sama tablica 90 akcji, argmax po logitach). Roznica,
przez ktora bot pod Managerem gral gorzej, siedziala w DWOCH rzeczach z
`bot.py` - obie odtwarza `prism_sdk/adapters/rlbot_timing.py`:

  1. Faza tikow. RLBotowa petla liczy akcje na poczatku okna osmiu tikow,
     ale wysyla ja dopiero tik przed jego koncem (`ticks >= tick_skip - 1`).
     To wyrownuje opoznienie do warunkow z treningu (tick_skip=8 @ 120 fps).
     Manager sam z siebie wysylal akcje natychmiast po policzeniu - inna faza,
     a te modele sa wrazliwe wlasnie na timing flipow i kontaktu w powietrzu.

  2. Skryptowany kickoff. `hardcoded_kickoffs=True` jest w `bot.py` domyslne:
     stala sekwencja 168 tikow przejmuje sterowanie na czas kickoffu, razem
     z regula "kto jedzie" (najblizszy, przy remisie w promieniu 10 uu -
     lewy). Kickoff zdarza sie po kazdym golu, wiec to najbardziej widoczny
     fragment meczu.

CO SIE ROZNI OD `bot.py` (swiadomie)
------------------------------------
`beta`. RLBotowy Nexto ma `beta=1` (argmax) na cala gre i podbija do 0.5
tylko na `is_kickoff_pause`. U nas kickoff rozgrywa skrypt, wiec sampling
mialby znaczenie wylacznie dla auta, ktore kickoffu NIE bierze - przez
niecala sekunde na gola. Zostaje czysty argmax.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from prism_sdk.agent import bot
from prism_sdk.adapters.nexto import NextoAgent
from prism_sdk.adapters.rlbot_timing import RLBotTimingMixin

#: Wagi leza w korzeniu projektu, nie w `pretrained_agents/nexto/`, zeby nie
#: trzymac 1.8 MB w dwoch kopiach. Adapter przyjmuje sciezke w konstruktorze.
MODEL_PATH = os.path.join(_ROOT, "nexto-model.pt")


@bot("nexto", aliases=["nexto_rl", "nexto_rlbot"],
     desc="Nexto v2 (obs 231+, 90 akcji) - taktowanie i kickoff jak w RLBocie",
     model_path=MODEL_PATH)
class Nexto(RLBotTimingMixin, NextoAgent):
    """Kolejnosc baz MA ZNACZENIE.

    MRO musi byc `mixin -> NextoAgent -> Agent`, zeby `act()` mixinu opakowal
    `act()` modelu: mixin decyduje, KIEDY policzona akcja trafia do gry, a
    model tylko ja liczy. Odwrotna kolejnosc = mixin nigdy nie zostaje
    wywolany i wracamy do sterowania bez fazy tikow.
    """

    name = "Nexto"


@bot("nexto_raw", desc="Nexto bez taktowania RLBota - decyzja co 15 Hz, bez kickoffu",
     model_path=MODEL_PATH)
class NextoRaw(NextoAgent):
    """Wersja bez mixinu - do porownania A/B w tym samym meczu.

    Gra zauwazalnie slabiej (zwlaszcza kickoffy). Trzymana wylacznie po to,
    zeby dalo sie zmierzyc, ile daje samo taktowanie.
    """

    name = "NextoRaw"
