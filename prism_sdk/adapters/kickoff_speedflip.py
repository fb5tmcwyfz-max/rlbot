"""
Speed flip na kickoffie - maszyna stanow zamiast tabeli tikow.

DLACZEGO NIE TABELA
-------------------
`KICKOFF_NUMPY` z RLBotPacka to 168 wierszy po jednym tiku gry. Ma trzy wady,
ktore w naszym runtime bola bardziej niz pod RLBotem:

1. JEDNA TABELA NA WSZYSTKIE SPAWNY. Pierwsze 60 tikow (skret w lewo, potem
   skok) jest napisane pod spawn NAROZNY. Ze spawnu srodkowego ta sama
   sekwencja to bezsensowny podskok w miejscu. Botimus Prime rozwiazuje to
   wprost - jego `SpeedFlipDodgeKickoff` ma w docstringu "Works only on corner
   kickoffs".
2. STALY MOMENT SKOKU. Tabela skacze na tiku 60 niezaleznie od tego, jak
   auto rozpedzilo sie naprawde. Botimus czeka na PREDKOSC (>800 uu/s).
3. OKNA PO 4 TIKI. Puszczenie skoku i dodge trwaja po 33 ms. My taktujemy
   zegarem sciennym, wiec jedno zaciecie petli potrafi je uciac. Botimus ma
   okna 100/100/50 ms - dwa do trzech razy szersze.

Do tego timingi rozjezdzaja sie merytorycznie: tabela nie trzyma pitcha w gore
podczas pierwszego skoku (nos opada, zanim przyjdzie dodge) i anuluje flipa
samym pitchem, bez rolla i yawa.

ZRODLO TIMINGOW
---------------
BotimusPrime, `maneuvers/jumps/speed_flip.py`:

    FIRST_JUMP_DURATION  = 0.1
    BETWEEN_JUMPS_DELAY  = 0.1
    SECOND_JUMP_DURATION = 0.05
    faza 1  (0.00-0.10)  jump=1  pitch=+1
    faza 2  (0.10-0.20)  jump=0  pitch=+1
    faza 3  (0.20-0.25)  jump=1  pitch=-1  roll=-0.3*dir
    faza 4  (0.25+)      jump=0  pitch=+1  roll=-1*dir  yaw=-1*dir

`dir` bierze sie z tego, po ktorej stronie auta jest pilka - nie jest zaszyty
na sztywno, dzieki czemu ten sam kod dziala z obu narozy i dla obu druzyn.

ZNAKI OSI
---------
Konwencja `ControllerState` (patrz jego docstring): pitch>0 = nos DO GORY,
yaw>0 = nos w PRAWO, roll>0 = prawy bok W DOL. `pitch=-1` w fazie 3 to wiec
dodge do PRZODU, a `pitch=+1` w fazie 4 jest przeciwny do niego - i to jest
caly flip cancel.

Gdyby flip wychodzil lustrzanie odwrotnie, odwroc znak jednym przelacznikiem:
    PRISM_FLIP_DIR=-1 python run_sdk.py --run
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np

from prism_sdk.control import ControllerState

#: Progi czasowe liczone od startu manewru (sekundy) - patrz zrodlo wyzej.
T_JUMP1 = 0.10
T_GAP = 0.20
T_DODGE = 0.25
TIMEOUT = 2.0

#: Predkosc, przy ktorej zaczynamy flipa. Botimus: 800 dla wariantu z
#: dodgem w pilke, 1050 dla samego speed flipa. Bierzemy nizszy prog - na
#: kickoffie liczy sie, zeby zdazyc przed przeciwnikiem.
TRIGGER_SPEED = 800.0

#: Awaryjny prog czasu: jesli auto z jakiegos powodu nie rozpedza sie
#: (kolizja, demo), i tak nie stoimy w miejscu w nieskonczonosc.
MAX_ACCEL_TIME = 1.2

#: Globalne odwrocenie strony flipa - do jednorazowej weryfikacji znaku.
_DIR_SIGN = 1.0 if os.environ.get("PRISM_FLIP_DIR", "1").strip() != "-1" else -1.0


class SpeedFlipKickoff:
    """Kickoff z narozników: rozpedzenie -> speed flip -> oddanie sterowania.

    Uzycie (stan trzymany po stronie wolajacego):

        kf = SpeedFlipKickoff()
        kf.reset()
        cs = kf.step(dt, speed, ball_local_y, on_ground)
        if kf.finished: ...   # oddaj sterowanie sieci
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._t = 0.0                 # czas od startu manewru
        self._accel_t = 0.0           # czas rozpedzania (przed flipem)
        self._flipping = False
        self._dir = 1.0
        self.finished = False

    # ------------------------------------------------------------------
    @staticmethod
    def ball_local_y(car_pos, car_rot_mtx, ball_pos) -> float:
        """Pozycja pilki w osi PRAWO auta. Ujemna = pilka po lewej.

        To jest odpowiednik `local(car, ball)[1]` z rlutilities. Kolumna 1
        macierzy obrotu to wektor "w prawo" auta.
        """
        try:
            rel = np.asarray(ball_pos, dtype=np.float64)[:3] - \
                np.asarray(car_pos, dtype=np.float64)[:3]
            right = np.asarray(car_rot_mtx, dtype=np.float64)[:, 1]
            return float(np.dot(rel, right))
        except Exception:
            return 0.0

    def step(self, dt: float, speed: float, ball_local_y: float,
             on_ground: bool) -> ControllerState:
        """Jeden tik manewru. `dt` w sekundach (czas rzeczywisty)."""
        self._t += max(dt, 0.0)

        if not self._flipping:
            self._accel_t += max(dt, 0.0)
            # Rozpedzanie na wprost, pelen gaz i boost. Kierunku nie
            # korygujemy - auto stoi juz nosem w strone pilki, a skret
            # zabralby predkosc potrzebna do flipa.
            if speed >= TRIGGER_SPEED or self._accel_t >= MAX_ACCEL_TIME:
                self._flipping = True
                self._t = 0.0
                # Strona jak u Botimusa: `right_handed = local(ball).y < 0`,
                # czyli gdy pilka jest po LEWEJ, flip idzie "prawoskretnie".
                self._dir = (1.0 if ball_local_y < 0 else -1.0) * _DIR_SIGN
                return self._flip_state(0.0)
            return ControllerState(throttle=1.0, boost=True)

        t = self._t
        # Koniec: wrocilismy na kola po pelnym manewrze albo timeout.
        if t > TIMEOUT or (on_ground and t > 0.5):
            self.finished = True
        return self._flip_state(t)

    # ------------------------------------------------------------------
    def _flip_state(self, t: float) -> ControllerState:
        d = self._dir
        if t < T_JUMP1:
            # Skok 1. Pitch w GORE, zeby nos nie opadl przed dodgem - tego
            # tabela Nexto nie robila w ogole.
            return ControllerState(throttle=1.0, boost=True, jump=True, pitch=1.0)
        if t < T_GAP:
            # Przerwa. Skok MUSI byc puszczony, inaczej gra nie zobaczy
            # drugiego wcisniecia i dodge'a nie bedzie.
            return ControllerState(throttle=1.0, boost=True, jump=False, pitch=1.0)
        if t < T_DODGE:
            # Dodge do przodu, przechylony rollem - to z przechylenia bierze
            # sie ukos, a nie z yawa.
            return ControllerState(throttle=1.0, boost=True, jump=True,
                                   pitch=-1.0, roll=-0.3 * d)
        # Cancel: pitch przeciwny do dodge'a, plus roll i yaw - wszystkie trzy
        # osie naraz. Tabela Nexto anulowala samym pitchem.
        return ControllerState(throttle=1.0, boost=True, jump=False,
                               pitch=1.0, roll=-1.0 * d, yaw=-1.0 * d)

    def describe(self) -> str:
        if not self._flipping:
            return f"rozpedzanie ({self._accel_t:.2f}s)"
        for lim, name in ((T_JUMP1, "skok1"), (T_GAP, "przerwa"),
                          (T_DODGE, "dodge")):
            if self._t < lim:
                return f"{name} t={self._t:.3f} dir={self._dir:+.0f}"
        return f"cancel t={self._t:.3f} dir={self._dir:+.0f}"
