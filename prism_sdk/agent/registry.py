"""
Rejestr botow - podpinanie wlasnego bota bez dotykania launchera.

Wczesniej dodanie bota wymagalo dopisania kolejnego `if name == ...` w
`launch_sdk.py:_load_agent()`. Teraz wystarczy wrzucic plik do `bots/` i
oznaczyc klase dekoratorem:

    # bots/moj_bot.py
    from prism_sdk.agent   import Agent, bot
    from prism_sdk.control import ControllerState

    @bot("moj", aliases=["mojbot"], desc="Moj pierwszy bot")
    class MojBot(Agent):
        name = "MojBot"
        decision_hz = None          # None = decyzja co tick (240 Hz)

        def act(self) -> ControllerState:
            w = self._ctx.world
            return ControllerState(throttle=1.0)

Potem:
    python launch_sdk.py --run --bot moj --send patch
    python launch_sdk.py --list-bots

`discover()` importuje wszystkie moduly z `bots/` (i dowolnych innych katalogow),
wiec sama obecnosc pliku wystarcza - nie trzeba go nigdzie rejestrowac recznie.

Bot moze przyjmowac argumenty z linii polecen przez `--bot-arg klucz=wartosc`;
trafiaja do konstruktora jako kwargs (patrz `create()`).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from prism_sdk.agent.base import Agent


@dataclass
class BotEntry:
    """Jeden zarejestrowany bot."""
    key: str                       # nazwa w CLI (--bot <key>)
    cls: Type[Agent]
    desc: str = ""
    aliases: List[str] = field(default_factory=list)
    #: Kwargs domyslne przekazywane do konstruktora
    defaults: Dict[str, Any] = field(default_factory=dict)

    @property
    def all_names(self) -> List[str]:
        return [self.key] + list(self.aliases)


#: key/alias -> BotEntry (aliasy wskazuja ten sam obiekt)
_REGISTRY: Dict[str, BotEntry] = {}


def bot(key: str, aliases: Optional[List[str]] = None, desc: str = "",
        **defaults: Any) -> Callable[[Type[Agent]], Type[Agent]]:
    """Dekorator rejestrujacy klase agenta pod nazwa `key`.

    `defaults` to kwargs wstrzykiwane do konstruktora przy `create()` -
    wygodne, gdy jeden kod obsluguje kilka wariantow bota.
    """
    def _wrap(cls: Type[Agent]) -> Type[Agent]:
        entry = BotEntry(key=key, cls=cls, desc=desc,
                         aliases=list(aliases or []), defaults=dict(defaults))
        for n in entry.all_names:
            existing = _REGISTRY.get(n)
            if existing is not None and existing.cls is not cls:
                raise ValueError(
                    f"nazwa bota '{n}' juz zajeta przez {existing.cls.__name__} "
                    f"(chcial ja {cls.__name__})")
            _REGISTRY[n] = entry
        return cls
    return _wrap


def register(key: str, cls: Type[Agent], desc: str = "",
             aliases: Optional[List[str]] = None, **defaults: Any) -> None:
    """Rejestracja imperatywna - dla klas, ktorych nie chcemy dekorowac
    (np. adaptery wytrenowanych sieci, ladowane leniwie)."""
    bot(key, aliases=aliases, desc=desc, **defaults)(cls)


# ==================================================================
# Auto-discovery
# ==================================================================

def discover(dirs: Optional[List[str]] = None, verbose: bool = False) -> int:
    """Zaimportuj wszystkie moduly .py z podanych katalogow (domyslnie `bots/`).

    Zwraca liczbe zaimportowanych modulow. Blad w jednym pliku nie przerywa
    calosci - reszta botow ma dzialac dalej (wypisujemy ostrzezenie).
    """
    if dirs is None:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        dirs = [os.path.join(root, "bots")]

    count = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        if d not in sys.path:
            sys.path.insert(0, d)
        for fn in sorted(os.listdir(d)):
            if fn.startswith("_") or ".cpython-" in fn:
                continue
            if fn.endswith(".py"):
                stem = fn[:-3]
            elif fn.endswith(".pyc"):
                stem = fn[:-4]
            else:
                continue
            mod_name = f"prism_bots_{stem}"
            # Idempotencja: discover() bywa wolany kilka razy w jednym procesie
            # (--list-bots, potem _load_agent). Bez tego re-import tworzylby
            # DRUGI obiekt klasy pod ta sama nazwa i @bot rzucalby "nazwa juz
            # zajeta" - mylacy blad o kolizji bota z samym soba.
            if mod_name in sys.modules:
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    mod_name, os.path.join(d, fn))
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                count += 1
                if verbose:
                    print(f"[registry] zaladowano bots/{fn}")
            except Exception as e:
                sys.modules.pop(mod_name, None)   # nie zostawiaj polimportu
                print(f"[registry] pominieto bots/{fn}: {type(e).__name__}: {e}")
    return count


# ==================================================================
# Query / create
# ==================================================================

def available() -> List[BotEntry]:
    """Unikalne wpisy (bez duplikatow z aliasow), posortowane po kluczu."""
    seen, out = set(), []
    for entry in _REGISTRY.values():
        if id(entry) in seen:
            continue
        seen.add(id(entry))
        out.append(entry)
    return sorted(out, key=lambda e: e.key)


def get(name: str) -> BotEntry:
    entry = _REGISTRY.get(name.lower().strip())
    if entry is None:
        names = ", ".join(e.key for e in available()) or "(pusto - discover() nie znalazl nic)"
        raise KeyError(f"nieznany bot '{name}'. Dostepne: {names}")
    return entry


def create(name: str, **kwargs: Any) -> Agent:
    """Zbuduj instancje bota. kwargs nadpisuja `defaults` z rejestracji."""
    entry = get(name)
    merged = dict(entry.defaults)
    merged.update(kwargs)
    try:
        return entry.cls(**merged)
    except TypeError as e:
        raise TypeError(
            f"nie moge zbudowac '{name}' ({entry.cls.__name__}) z argumentami "
            f"{merged}: {e}") from e


def describe() -> str:
    """Tabelka do wypisania w CLI (--list-bots)."""
    entries = available()
    if not entries:
        return "(brak zarejestrowanych botow - czy `bots/` istnieje?)"
    w = max(len(e.key) for e in entries)
    lines = []
    for e in entries:
        alias = f"  (alias: {', '.join(e.aliases)})" if e.aliases else ""
        hz = getattr(e.cls, "decision_hz", None)
        rate = f" [{hz:g} Hz]" if hz else ""
        lines.append(f"  {e.key.ljust(w)}  {e.desc}{alias}{rate}")
    return "\n".join(lines)
