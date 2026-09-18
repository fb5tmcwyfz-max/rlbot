"""
prism_dump - czytnik `rl_dump_full.txt` (pelny dump SDK Rocket League).

PO CO TO ISTNIEJE
-----------------
`prism_sdk` ma RVA i globale WPISANE NA SZTYWNO, w dwoch roznych miejscach:

    prism_sdk/low/offsets.py     GNAMES_RVA / GOBJECTS_RVA
    prism_sdk/low/reflection.py  GNAMES_RVA / GOBJECTS_RVA   (druga kopia!)
    prism_sdk/control/kernel_hook.py   SETVI_RVA

Po kazdym update Rocket League te liczby sie rozjezdzaja, a dwie kopie tej
samej stalej rozjezdzaja sie takze miedzy soba. Dump jest jedynym zrodlem
prawdy dla tego builda - ten modul go parsuje i wstrzykuje wartosci do SDK
ZANIM cokolwiek dotknie pamieci gry.

CO JEST W DUMPIE (a czego nie ma)
---------------------------------
JEST:   GObjects/GNames RVA, RVA 3308 funkcji natywnych z poziomem pewnosci,
        16542 nazwy funkcji skryptowych, RVA dispatchera bytecode'u,
        offsety refleksji UObject.Name / UObject.Class / UFunction.Func.
NIE MA: offsetow pol (Ball.RADIUS, Vehicle.INPUT itd.). Te siedza dalej w
        `prism_sdk/low/offsets.py` i musza byc weryfikowane recznie.

UZYCIE
------
    python prism_dump.py                  # podsumowanie + porownanie z SDK
    python prism_dump.py --find SetVehicle
    python prism_dump.py --script SetVehicleInput

    # z kodu (robi to za Ciebie run_sdk.py):
    from prism_dump import RLDump
    RLDump.load().apply_to_sdk()
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

DUMP_FILENAME = "rl_dump_full.txt"

#: Kolejnosc od najlepszej. `none` = dumper nie znalazl ani jednego RVA.
CONFIDENCE_ORDER = ("high", "medium", "low", "none")

_SECTION_RE = re.compile(r"^\s{2}([A-Z][A-Z0-9 ()\-]*?)\s*(?:\(\d+\).*)?$")

# `  ApplyForces                                        0xE30110       high     18`
_NATIVE_ROW_RE = re.compile(
    r"^\s{2}(?P<name>\S+)\s+(?P<rva>0x[0-9A-Fa-f]+|n/a)\s+"
    r"(?P<conf>high|medium|low|none)\s+(?P<inst>\d+)\s*$"
)

# `  ZoneSort                                           1      ZoneSort`
_SCRIPT_ROW_RE = re.compile(r"^\s{2}(?P<name>\S+)\s+(?P<inst>\d+)\s+\S")

_KV_RE = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z0-9_. ]*?)\s*[:=]\s*(?P<val>\S.*?)\s*$")


class DumpError(RuntimeError):
    pass


@dataclass
class NativeFunction:
    name: str
    rva: Optional[int]
    confidence: str
    instances: int
    #: Dumper przycina nazwy dluzsze niz 50 znakow do `...`. Takiego wpisu nie
    #: wolno dopasowywac po pelnej nazwie - stad flaga.
    truncated: bool = False

    def __str__(self) -> str:
        rva = f"0x{self.rva:X}" if self.rva is not None else "n/a"
        return f"{self.name} = {rva} ({self.confidence}, {self.instances} inst)"


@dataclass
class ApplyReport:
    """Co `apply_to_sdk()` faktycznie zmienilo i co znalazlo po drodze."""
    changed: List[str] = field(default_factory=list)
    ok: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def print(self, prefix: str = "[dump]") -> None:
        for line in self.ok:
            print(f"{prefix} {line}")
        for line in self.changed:
            print(f"{prefix} ZMIANA: {line}")
        for line in self.warnings:
            print(f"{prefix} UWAGA:  {line}")


class RLDump:
    """Sparsowany `rl_dump_full.txt`.

    Sekcja CLASS INDEX (59 z 82 tysiecy linii) jest POMIJANA - to reverse
    index funkcji po klasach, do niczego tu nie potrzebny, a parsowanie go
    kosztuje wiecej niz cala reszta pliku razem wzieta.
    """

    _cache: Dict[str, "RLDump"] = {}

    def __init__(self, path: str):
        self.path: str = path
        self.meta: Dict[str, str] = {}
        self.globals: Dict[str, int] = {}
        #: lowercase name -> NativeFunction
        self.native: Dict[str, NativeFunction] = {}
        #: lowercase nazwy funkcji dispatchowanych przez bytecode interpreter
        self.script: Set[str] = set()
        self._parse()

    # ------------------------------------------------------------------
    # Wczytanie
    # ------------------------------------------------------------------

    @staticmethod
    def find_dump(start: Optional[str] = None) -> str:
        """Znajdz `rl_dump_full.txt` - obok tego pliku, potem u przodkow."""
        here = os.path.dirname(os.path.abspath(start or __file__))
        d = here
        while True:
            candidate = os.path.join(d, DUMP_FILENAME)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(d)
            if parent == d:
                raise DumpError(
                    f"nie znalazlem {DUMP_FILENAME} ani w {here}, ani u zadnego "
                    f"z katalogow nadrzednych")
            d = parent

    @classmethod
    def load(cls, path: Optional[str] = None) -> "RLDump":
        """Sparsuj dump (raz na proces - wynik jest cache'owany po sciezce)."""
        resolved = os.path.abspath(path) if path else cls.find_dump()
        cached = cls._cache.get(resolved)
        if cached is None:
            cached = cls(resolved)
            cls._cache[resolved] = cached
        return cached

    def _parse(self) -> None:
        section = ""
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            prev_was_rule = False
            for raw in fh:
                line = raw.rstrip("\n")

                if line.startswith("====="):
                    prev_was_rule = True
                    continue
                if prev_was_rule:
                    prev_was_rule = False
                    m = _SECTION_RE.match(line)
                    if m:
                        section = m.group(1).strip()
                        # Tabela "BY RVA" to ta sama tresc posortowana inaczej,
                        # a CLASS INDEX jest nam niepotrzebny i ogromny.
                        if section.startswith("CLASS INDEX"):
                            break
                        continue

                if section == "META":
                    self._parse_meta(line)
                elif section == "GLOBALS":
                    self._parse_global(line)
                elif section.startswith("NATIVE FUNCTIONS") and "BY RVA" not in section:
                    self._parse_native(line)
                elif section.startswith("SCRIPT FUNCTIONS"):
                    self._parse_script(line)

        if not self.globals:
            raise DumpError(f"{self.path}: brak sekcji GLOBALS - to nie jest dump SDK")
        if not self.native:
            raise DumpError(f"{self.path}: brak funkcji natywnych - dump uciety?")

    def _parse_meta(self, line: str) -> None:
        m = _KV_RE.match(line)
        if m:
            self.meta[m.group("key").strip()] = m.group("val").strip()

    def _parse_global(self, line: str) -> None:
        m = _KV_RE.match(line)
        if not m:
            return
        try:
            self.globals[m.group("key").strip()] = int(m.group("val"), 0)
        except ValueError:
            pass

    def _parse_native(self, line: str) -> None:
        m = _NATIVE_ROW_RE.match(line)
        if not m:
            return
        name = m.group("name")
        rva_s = m.group("rva")
        entry = NativeFunction(
            name=name.rstrip("."),
            rva=(int(rva_s, 16) if rva_s != "n/a" else None),
            confidence=m.group("conf"),
            instances=int(m.group("inst")),
            truncated=name.endswith("..."),
        )
        self.native[entry.name.lower()] = entry

    def _parse_script(self, line: str) -> None:
        m = _SCRIPT_ROW_RE.match(line)
        if m:
            self.script.add(m.group("name").rstrip(".").lower())

    # ------------------------------------------------------------------
    # Zapytania
    # ------------------------------------------------------------------

    @property
    def script_dispatcher_rva(self) -> Optional[int]:
        """RVA wspolnego interpretera bytecode'u UnrealScript.

        KLUCZOWE przy hookowaniu: kazda funkcja skryptowa ma `UFunction.Func`
        rownym TEJ wartosci. Jesli "znalezione" RVA funkcji rowna sie tej
        liczbie, to nie jest jej adres - to adres dispatchera, a patch w tym
        miejscu lapie WSZYSTKIE wywolania skryptowe w grze.
        """
        raw = self.meta.get("Script dispatcher RVA")
        try:
            return int(raw, 0) if raw else None
        except ValueError:
            return None

    @property
    def dumped_at(self) -> str:
        return self.meta.get("Dumped at", "n/a")

    def reflection_offset(self, key: str) -> Optional[int]:
        """np. reflection_offset("UObject.Name") -> 0x48."""
        raw = self.meta.get(key)
        try:
            return int(raw, 0) if raw else None
        except ValueError:
            return None

    def get(self, name: str) -> Optional[NativeFunction]:
        return self.native.get(name.lower())

    def native_rva(self, name: str, min_confidence: str = "low",
                   allow_dispatcher: bool = False) -> Optional[int]:
        """RVA funkcji natywnej albo None.

        `min_confidence` odsiewa slabe trafienia: 'high' = przynajmniej 3
        instancje UFunction wskazuja ten sam adres, 'medium' = 2, 'low' = 1
        (moze byc wrapper/alias - weryfikuj recznie).
        """
        entry = self.get(name)
        if entry is None or entry.rva is None:
            return None
        limit = CONFIDENCE_ORDER.index(min_confidence)
        if CONFIDENCE_ORDER.index(entry.confidence) > limit:
            return None
        if not allow_dispatcher and entry.rva == self.script_dispatcher_rva:
            return None
        return entry.rva

    def is_script(self, name: str) -> bool:
        return name.lower() in self.script

    def find(self, needle: str, limit: int = 40) -> List[NativeFunction]:
        needle = needle.lower()
        hits = [e for k, e in self.native.items() if needle in k]
        hits.sort(key=lambda e: (CONFIDENCE_ORDER.index(e.confidence), e.name.lower()))
        return hits[:limit]

    # ------------------------------------------------------------------
    # Wstrzykniecie do prism_sdk
    # ------------------------------------------------------------------

    def apply_to_sdk(self, apply_setvi: bool = False) -> ApplyReport:
        """Nadpisz stale w `prism_sdk` wartosciami z dumpa.

        Bezpieczne domyslnie: podmieniane sa TYLKO globale (GObjects/GNames),
        bo to czyste dane - zly adres konczy sie nieudanym skanem, nie crashem
        gry. `SETVI_RVA` jest raportowany, ale NIE podmieniany bez wyraznej
        zgody - patrz komentarz przy `_check_setvi`.

        Musi byc wolane PRZED `World().attach()`; `reflection.py` czyta swoja
        stala dopiero w `rebuild()`, wiec sama podmiana atrybutu modulu
        wystarczy - nie trzeba nic przeladowywac.
        """
        report = ApplyReport()

        dumped = self.dumped_at
        report.ok.append(f"{os.path.basename(self.path)} (dump z {dumped}), "
                         f"{len(self.native)} funkcji natywnych")

        self._apply_globals(report)
        self._check_reflection_offsets(report)
        self._check_setvi(report, apply_setvi)
        return report

    def _apply_globals(self, report: ApplyReport) -> None:
        from prism_sdk.low import offsets as O
        from prism_sdk.low import reflection as R

        pairs = (("GOBJECTS_RVA", "GObjects_RVA"), ("GNAMES_RVA", "GNames_RVA"))
        for attr, dump_key in pairs:
            want = self.globals.get(dump_key)
            if want is None:
                report.warnings.append(f"dump nie ma {dump_key} - zostawiam wartosc z SDK")
                continue
            # Dwie kopie tej samej stalej - obie musza dostac te sama wartosc,
            # inaczej Reflection skanuje pod innym adresem niz reszta SDK.
            for module in (O, R):
                have = getattr(module, attr, None)
                if have == want:
                    continue
                setattr(module, attr, want)
                report.changed.append(
                    f"{module.__name__}.{attr}: 0x{have:X} -> 0x{want:X}"
                    if isinstance(have, int) else
                    f"{module.__name__}.{attr} = 0x{want:X}")
        if not report.changed:
            report.ok.append(f"globale zgodne z SDK "
                             f"(GObjects=0x{self.globals.get('GObjects_RVA', 0):X}, "
                             f"GNames=0x{self.globals.get('GNames_RVA', 0):X})")

    def _check_reflection_offsets(self, report: ApplyReport) -> None:
        """Offsety refleksji sa w SDK w kilku miejscach - tu tylko weryfikacja.

        Ich zmiana to przebudowa polowy `low/`, wiec nie ruszamy ich
        automatycznie; chodzi o to, zeby rozjazd nie przeszedl niezauwazony.
        """
        from prism_sdk.low import offsets as O
        from prism_sdk.low import reflection as R

        checks = (
            ("UObject.Name", R.UOBJECT_NAME_IDX, "reflection.UOBJECT_NAME_IDX"),
            ("UObject.Name", O.UOBJECT_NAME_IDX, "offsets.UOBJECT_NAME_IDX"),
            ("UObject.Class", R.UOBJECT_CLASS, "reflection.UOBJECT_CLASS"),
        )
        for dump_key, have, label in checks:
            want = self.reflection_offset(dump_key)
            if want is not None and want != have:
                report.warnings.append(
                    f"{label} = 0x{have:X}, a dump mowi {dump_key} = 0x{want:X} "
                    f"- refleksja bedzie czytac nazwy z zlego offsetu")

    def _check_setvi(self, report: ApplyReport, apply: bool) -> None:
        """SetVehicleInput - hook kernelowy celuje dzis w dispatcher skryptow.

        `kernel_hook.SETVI_RVA` zostal ustawiony na podstawie "3 z 4 instancji
        UFunction wskazuja ten adres". Dump pokazuje, dlaczego to bledny wniosek:
        te trzy instancje to funkcje SKRYPTOWE, a ich `Func` z definicji
        wskazuje wspolny interpreter bytecode'u. Patch w tym miejscu przechwytuje
        kazde wywolanie skryptowe w grze, nie input auta.

        Nie podmieniam tego automatycznie, bo `STOLEN_SIZE = 12` w
        `kernel_hook.py` zostal policzony dla PROLOGU DISPATCHERA. Inna funkcja
        = inny prolog; skopiowanie 12 bajtow w losowym miejscu rozetnie
        instrukcje w polowie i wywali gre. Zmiana RVA wymaga ponownego
        policzenia granicy instrukcji - stad `--apply-setvi` jako swiadoma decyzja.
        """
        from prism_sdk.control import kernel_hook as KH

        have = getattr(KH, "SETVI_RVA", None)
        entry = self.get("SetVehicleInput")
        disp = self.script_dispatcher_rva

        if have is not None and disp is not None and have == disp:
            report.warnings.append(
                f"kernel_hook.SETVI_RVA = 0x{have:X} to RVA DISPATCHERA SKRYPTOW "
                f"(patrz META dumpa), nie SetVehicleInput - patch w tym miejscu "
                f"lapie wszystkie wywolania skryptowe. Kanal 'patch' jest niepewny; "
                f"uzywaj --send pc.")
        if entry is None or entry.rva is None:
            report.warnings.append("dump nie zna natywnego SetVehicleInput")
            return

        script_note = " (+3 instancje skryptowe)" if self.is_script("SetVehicleInput") else ""
        report.ok.append(f"dump: natywny SetVehicleInput = 0x{entry.rva:X} "
                         f"({entry.confidence}, {entry.instances} inst){script_note}")

        if not apply:
            if have != entry.rva:
                report.warnings.append(
                    f"nie podmieniam SETVI_RVA (0x{have:X} -> 0x{entry.rva:X}) - "
                    f"wymaga policzenia nowego STOLEN_SIZE dla prologu tej funkcji. "
                    f"Swiadomie: --apply-setvi")
            return

        KH.SETVI_RVA = entry.rva
        report.changed.append(f"kernel_hook.SETVI_RVA: 0x{have:X} -> 0x{entry.rva:X}")
        report.warnings.append(
            f"SETVI_RVA podmienione, ale STOLEN_SIZE={getattr(KH, 'STOLEN_SIZE', '?')} "
            f"jest dalej z prologu starej funkcji - zweryfikuj bajty "
            f"(python prism_dump.py --prologue SetVehicleInput) zanim uzyjesz --send patch")

    # ------------------------------------------------------------------
    # Diagnostyka
    # ------------------------------------------------------------------

    def describe(self) -> str:
        disp = self.script_dispatcher_rva
        lines = [
            f"plik:                {self.path}",
            f"dump z:              {self.dumped_at}",
            f"base przy dumpie:    {self.meta.get('RL module base', 'n/a')}",
            f"funkcje natywne:     {len(self.native)}",
            # Dumper przycina nazwy do 50 znakow, wiec dlugie nazwy skryptowe
            # zlewaja sie w jedna - stad mniej unikatow niz instancji w META.
            f"funkcje skryptowe:   {len(self.script)} unikalnych nazw "
            f"(META dumpa: {self.meta.get('Script functions', '?')} instancji)",
            f"dispatcher skryptow: " + (f"0x{disp:X}" if disp else "n/a"),
            "globale:",
        ]
        for k, v in self.globals.items():
            lines.append(f"  {k:<20} = 0x{v:X}")
        lines.append("offsety refleksji:")
        for k in ("UObject.Name", "UObject.Class", "UFunction.Func"):
            v = self.reflection_offset(k)
            if v is not None:
                lines.append(f"  {k:<20} = 0x{v:X}")
        counts = {c: 0 for c in CONFIDENCE_ORDER}
        for e in self.native.values():
            counts[e.confidence] = counts.get(e.confidence, 0) + 1
        lines.append("pewnosc RVA:         " +
                     ", ".join(f"{c}={counts[c]}" for c in CONFIDENCE_ORDER))
        return "\n".join(lines)


# ======================================================================
# CLI
# ======================================================================

def _cmd_prologue(dump: RLDump, name: str) -> int:
    """Odczytaj pierwsze bajty funkcji z ZYWEGO procesu.

    Bez tego nie da sie odpowiedzialnie zmienic celu patcha: trzeba zobaczyc
    prawdziwy prolog, zeby wiedziec, gdzie wypada granica instrukcji.
    """
    entry = dump.get(name)
    if entry is None or entry.rva is None:
        print(f"[!] dump nie zna funkcji natywnej '{name}'")
        return 1
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from prism_sdk.low.driver import Driver

    drv = Driver()
    if not drv.attach():
        print("[!] attach nieudany - driver zaladowany? RL uruchomiony?")
        return 1
    try:
        data = drv.read_bytes(drv.module_base + entry.rva, 24)
    finally:
        drv.detach()
    if not data:
        print(f"[!] odczyt spod 0x{entry.rva:X} nie powiodl sie")
        return 1
    print(f"{entry.name} @ base+0x{entry.rva:X} ({entry.confidence}):")
    print("  " + " ".join(f"{b:02X}" for b in data))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="czytnik rl_dump_full.txt")
    p.add_argument("--dump", default="", help="sciezka do rl_dump_full.txt")
    p.add_argument("--find", default="", help="szukaj funkcji natywnej po fragmencie nazwy")
    p.add_argument("--script", default="", help="sprawdz, czy nazwa jest funkcja skryptowa")
    p.add_argument("--prologue", default="",
                   help="odczytaj prolog funkcji z zywego procesu (wymaga drivera)")
    p.add_argument("--apply", action="store_true",
                   help="wstrzyknij wartosci do prism_sdk i wypisz raport")
    p.add_argument("--apply-setvi", action="store_true",
                   help="z --apply: podmien takze SETVI_RVA (patrz ostrzezenie)")
    args = p.parse_args(argv)

    try:
        dump = RLDump.load(args.dump or None)
    except DumpError as e:
        print(f"[!] {e}")
        return 1

    if args.find:
        hits = dump.find(args.find)
        if not hits:
            print(f"(brak trafien dla '{args.find}')")
            return 1
        for e in hits:
            mark = " [tez skryptowa]" if dump.is_script(e.name) else ""
            trunc = " [nazwa przycieta w dumpie]" if e.truncated else ""
            print(f"  {e}{mark}{trunc}")
        return 0

    if args.script:
        print(f"'{args.script}': "
              + ("jest funkcja skryptowa (dispatch przez bytecode interpreter)"
                 if dump.is_script(args.script) else "nie ma jej wsrod funkcji skryptowych"))
        entry = dump.get(args.script)
        if entry:
            print(f"  natywnie: {entry}")
        return 0

    if args.prologue:
        return _cmd_prologue(dump, args.prologue)

    print(dump.describe())
    print()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        report = dump.apply_to_sdk(apply_setvi=args.apply_setvi)
    except ImportError as e:
        print(f"[!] nie moge zaimportowac prism_sdk: {e}")
        return 1
    report.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
