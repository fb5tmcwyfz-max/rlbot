"""
Event detection - obserwuje zmiany stanu miedzy tickami i emituje eventy.

Uzycie:
    tracker = EventTracker()
    tracker.on("goal",       lambda e: print("GOAL!", e.team_num))
    tracker.on("ball_touch", lambda e: print("touched by team", e.team_num))
    tracker.on("kickoff",    lambda e: print("kickoff!"))
    tracker.on("demolition", lambda e: print("demo!", e.attacker, e.victim))

    for tick in range(...):
        world.refresh()
        tracker.update(world)   # wywoluje callbacki jesli byla zmiana

Eventy sa emit'owane raz - miedzy stanem poprzedniej ramki a aktualnej.
Bot ktory chce reagowac na eventy powinien albo:
  * uzyc tracker.on(...) - callbacki, wolane synchronicznie z update()
  * przeczytac tracker.last_events posle update()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class Event:
    """Bazowy event."""
    kind: str
    tick: int
    time: float = 0.0


@dataclass
class GoalEvent(Event):
    team_num: int = -1              # ktora druzyna strzelila
    blue_score: int = 0
    orange_score: int = 0


@dataclass
class BallTouchEvent(Event):
    team_num: int = -1              # kto dotknal (0/1/-1)
    car_addr: int = 0
    world_time: float = 0.0         # game time touch


@dataclass
class KickoffEvent(Event):
    round_num: int = 0


@dataclass
class DemolitionEvent(Event):
    victim_pri: int = 0             # ktory Player dostal demo
    attacker_pri: int = 0           # kto zdemolował (0 gdy nieznane)


@dataclass
class RoundEndEvent(Event):
    """Runda skonczona - moze byc gol albo end-of-time."""
    was_goal: bool = False
    scoring_team: int = -1


class EventTracker:
    """Sledzi zmiany stanu World i emituje eventy przy przejsciach.

    Nie trzyma pelnego snapshotu poprzedniej ramki - tylko krytyczne pola
    (scores, ball touches, round state, demolish counters).
    """

    def __init__(self):
        # subscribers: kind -> list callback
        self._callbacks: Dict[str, List[Callable[[Event], None]]] = {}
        # snapshot poprzednich krytycznych wartosci
        self._prev_scores: Dict[int, int] = {}   # team_num -> score
        self._prev_ball_touch_time: float = 0.0
        self._prev_round_active: Optional[bool] = None
        self._prev_round_num: int = -1
        self._prev_demo_counts: Dict[int, int] = {}   # pri_addr -> demolish count victim
        self._tick: int = 0
        # historia bufor
        self.last_events: List[Event] = []

    # ---- subscription ----

    def on(self, kind: str, callback: Callable[[Event], None]) -> None:
        """Zarejestruj callback na typ eventu.
        kind ∈ {'goal', 'ball_touch', 'kickoff', 'demolition', 'round_end'}."""
        self._callbacks.setdefault(kind, []).append(callback)

    def _emit(self, event: Event) -> None:
        self.last_events.append(event)
        for cb in self._callbacks.get(event.kind, []):
            try:
                cb(event)
            except Exception as e:
                print(f"[EventTracker] callback error on {event.kind}: {e}")

    # ---- main update ----

    def update(self, world) -> List[Event]:
        """Wywoluj co refresh(). Zwraca liste eventow emitowanych w tym tick.

        Uwaga: `world` musi byc juz refresh'owane. `tick` jest wewnetrzny -
        rozne od world tick jesli update() jest wolane nieregularnie.
        """
        self._tick += 1
        self.last_events = []
        game_time = world.match.time_remaining

        # 1. Score change -> goal
        for tnum, team in world.teams.items():
            prev = self._prev_scores.get(tnum, team.score)
            if team.score > prev:
                self._emit(GoalEvent(
                    kind="goal", tick=self._tick, time=game_time,
                    team_num=tnum,
                    blue_score  = world.teams.get(0).score if world.teams.get(0) else 0,
                    orange_score= world.teams.get(1).score if world.teams.get(1) else 0,
                ))
            self._prev_scores[tnum] = team.score

        # 2. Ball touch - LastHitWorldTime zmiana
        if world.ball.last_hit:
            wt = world.ball.last_hit.world_time
            if wt > self._prev_ball_touch_time and wt > 0:
                self._emit(BallTouchEvent(
                    kind="ball_touch", tick=self._tick, time=game_time,
                    team_num=world.ball.last_hit.team_num,
                    car_addr=world.ball.last_hit.car_addr,
                    world_time=wt,
                ))
                self._prev_ball_touch_time = wt

        # 3. Round active transition (kickoff detection)
        cur_active = world.match.is_round_active
        cur_round = world.match.round_num
        if self._prev_round_active is not None:
            # false -> true = kickoff start
            if not self._prev_round_active and cur_active:
                self._emit(KickoffEvent(
                    kind="kickoff", tick=self._tick, time=game_time,
                    round_num=cur_round,
                ))
            # true -> false = round end
            elif self._prev_round_active and not cur_active:
                # Sprawdz czy score sie zmienil w tej klatce - wtedy to gol,
                # inaczej moze time-out
                scored = any(
                    world.teams.get(tn) and world.teams[tn].score > self._prev_scores.get(tn, 0) - 1
                    for tn in (0, 1)
                )
                # (prev_scores juz zaktualizowane w kroku 1)
                self._emit(RoundEndEvent(
                    kind="round_end", tick=self._tick, time=game_time,
                    was_goal=world.ball.last_hit is not None,
                    scoring_team=world.match.match_winner_team,
                ))
        self._prev_round_active = cur_active
        self._prev_round_num    = cur_round

        # 4. Demolitions - kazdy Player ma match_demolishes (jako sprawca)
        #    liczymy delta per PRI zeby wiedziec kto zdemol'owal kogo
        for p in world.players:
            prev = self._prev_demo_counts.get(p.pri_address, p.match_demolishes)
            if p.match_demolishes > prev:
                # p to sprawca, ale nie wiemy kto ofiara bezposrednio z PRI.
                # Emit event bez victim (wykryjemy przez Car.AttackerPRI ktos inny).
                self._emit(DemolitionEvent(
                    kind="demolition", tick=self._tick, time=game_time,
                    attacker_pri=p.pri_address,
                    victim_pri=0,
                ))
            self._prev_demo_counts[p.pri_address] = p.match_demolishes

        return list(self.last_events)
