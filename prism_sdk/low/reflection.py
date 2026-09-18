"""
GObjects/GNames reflection scanner.

RL uzywa Unreal Engine 3 reflection - kazdy obiekt gry (Ball, Car, Team,
GameEvent) jest w globalnej tablicy `GObjects`, a jego nazwa klasy jest w
`GNames` pod indeksem czytanym z UObject+0x48. Skaner iteruje wszystkie
obiekty i buduje mape "class_name -> list[(index, address)]".

Zoptymalizowane:
  * pojedynczy batch read GObjects (jedna IOCTL zamiast N)
  * pojedynczy batch read GNames
  * name cache per instance - resolwacja nazwy po indeksie w O(1) po pierwszym
    trafieniu

Uzycie:
    scanner = Reflection(driver)
    scanner.rebuild()                          # jednorazowy scan (~kilka ms)
    ball_addr = scanner.highest("Ball_TA")     # zywa instancja (najwyzszy idx)
    all_cars = scanner.find("Car_Freeplay_TA") # wszystkie dopasowania
    scanner.rebuild()  # ponow gdy pilka/auta zniknely (respawn/menu)
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from prism_sdk.low.driver import Driver

# Offsety globalne dla builda Epic Games (potwierdzone przez rlsdk-main)
GOBJECTS_RVA         = 0x024171A0
GNAMES_RVA           = GOBJECTS_RVA - 0x48   # stala relacja miedzy nimi
UOBJECT_NAME_IDX     = 0x48                  # FName.Index (int32) w UObject
UOBJECT_NAME_NUM     = 0x4C                  # FName.Number (int32) - suffix; metaklasy=0
UOBJECT_CLASS        = 0x50                  # UObject.Class -> UClass*
USTRUCT_SUPER        = 0x80                  # UStruct.SuperField -> parent UClass*
FNAMEENTRY_TEXT      = 0x18                  # WCHAR string w FNameEntry


class Reflection:
    """Skaner GObjects. Cache nazw miedzy rebuild'ami dla szybkosci."""

    def __init__(self, driver: Driver):
        self.driver = driver
        # mapa: klasa -> [(gobjects_index, obj_addr), ...]
        self._by_class: Dict[str, List[Tuple[int, int]]] = {}
        # cache indeks -> nazwa klasy (utrzymywany miedzy rebuild'ami)
        self._name_cache: Dict[int, Optional[str]] = {}
        self._gnames_entries: Tuple[int, ...] = ()
        self._last_rebuild_ok: bool = False

    # -- API --

    def rebuild(self) -> bool:
        """Peny scan GObjects + GNames. Wywoluj przy pierwszym uzyciu i gdy
        obiekty zmieniaja adresy (respawn, menu, nowa runda)."""
        drv = self.driver
        if not drv.attached or drv.module_base == 0:
            return False

        gnames_ptr, gnames_cnt, _ = drv.read_tarray(drv.module_base + GNAMES_RVA)
        gobjs_ptr,  gobjs_cnt,  _ = drv.read_tarray(drv.module_base + GOBJECTS_RVA)
        if gnames_ptr == 0 or gobjs_ptr == 0 or gnames_cnt == 0 or gobjs_cnt == 0:
            self._last_rebuild_ok = False
            return False

        # batch read GNames entries (pointerowa tablica)
        self._gnames_entries = drv.read_ptr_array(gnames_ptr, gnames_cnt)
        if not self._gnames_entries:
            self._last_rebuild_ok = False
            return False

        # batch read GObjects entries
        obj_ptrs = drv.read_ptr_array(gobjs_ptr, gobjs_cnt)
        if not obj_ptrs:
            self._last_rebuild_ok = False
            return False

        by_class: Dict[str, List[Tuple[int, int]]] = {}
        for i, obj_ptr in enumerate(obj_ptrs):
            if obj_ptr == 0 or not (0x10000 <= obj_ptr < 0x7FFFFFFFFFFF):
                continue
            name_idx = drv.read_i32(obj_ptr + UOBJECT_NAME_IDX)
            name = self._resolve_name(name_idx)
            if name is None:
                continue
            by_class.setdefault(name, []).append((i, obj_ptr))

        self._by_class = by_class
        self._last_rebuild_ok = True
        return True

    def find(self, class_name: str) -> List[Tuple[int, int]]:
        """Zwraca [(gobjects_index, obj_addr), ...] - moze byc puste."""
        return self._by_class.get(class_name, [])

    def highest(self, class_name: str) -> Optional[int]:
        """Adres zywej instancji (najwyzszy indeks w GObjects) albo None.
        Instancja z najnizszym indeksem to zwykle CDO (Class Default Object) -
        archetyp klasy, ma zawsze zerowe pola. Zywa instancja jest na wyzszym.
        """
        matches = self._by_class.get(class_name, [])
        if not matches:
            return None
        return max(matches, key=lambda m: m[0])[1]

    def all(self) -> Dict[str, List[Tuple[int, int]]]:
        """Kopia calej mapy (do diagnostyki)."""
        return dict(self._by_class)

    def stats(self) -> Dict[str, int]:
        """Ile obiektow per klasa (do diagnostyki)."""
        return {k: len(v) for k, v in self._by_class.items()}

    def is_a(self, obj_addr: int, ancestor_name: str, max_depth: int = 16) -> bool:
        """Sprawdz czy obiekt dziedziczy po klasie o nazwie `ancestor_name`.
        Chodzi po UObject.Class -> UStruct.SuperField chain. Odporne na
        podklasy per-tryb (Dropshot=GameEvent_Breakout_TA, Season=GameEvent_Season_TA, etc.).
        """
        drv = self.driver
        try:
            cls = drv.read_ptr(obj_addr + UOBJECT_CLASS)
        except Exception:
            return False
        for _ in range(max_depth):
            if cls == 0: return False
            try:
                name_idx = drv.read_i32(cls + UOBJECT_NAME_IDX)
            except Exception:
                return False
            name = self._resolve_name(name_idx)
            if name == ancestor_name:
                return True
            try:
                cls = drv.read_ptr(cls + USTRUCT_SUPER)
            except Exception:
                return False
        return False

    def find_by_ancestor(self, ancestor_name: str) -> List[Tuple[int, int]]:
        """Zwraca wszystkie ZYWE instancje dziedziczace po `ancestor_name`.

        Filtruje:
          * metaklasy (UClass definitions w GObjects) - FName.Number = 0
          * "Default__"/"Archetype" name prefixes
          * grupy gdzie is_a matches[0] jest False (cache per class name)
        """
        drv = self.driver
        results: List[Tuple[int, int]] = []
        isa_cache: Dict[str, bool] = {}
        for name, matches in self._by_class.items():
            if name.startswith("Default__") or name.startswith("Archetype"):
                continue

            # Znajdz pierwsza NIE-metaklase (FName.Number > 0) do is_a check
            probe_addr = 0
            for _, addr in matches:
                try:
                    num = drv.read_i32(addr + UOBJECT_NAME_NUM)
                    if num > 0:  # instance (metaklasa ma Number=0)
                        probe_addr = addr
                        break
                except Exception:
                    continue
            if probe_addr == 0:
                continue  # wszystkie to metaklasy - pomin ta grupe

            ok = isa_cache.get(name)
            if ok is None:
                ok = self.is_a(probe_addr, ancestor_name)
                isa_cache[name] = ok
            if not ok:
                continue

            # Wez tylko instancje (Number > 0)
            for idx, addr in matches:
                try:
                    if drv.read_i32(addr + UOBJECT_NAME_NUM) > 0:
                        results.append((idx, addr))
                except Exception:
                    pass
        return results

    def highest_by_ancestor(self, ancestor_name: str) -> Optional[int]:
        """Adres zywej instancji dziedziczacej po `ancestor_name` (max index)."""
        matches = self.find_by_ancestor(ancestor_name)
        if not matches:
            return None
        return max(matches, key=lambda m: m[0])[1]

    @property
    def ok(self) -> bool:
        return self._last_rebuild_ok

    # -- internal --

    def _resolve_name(self, idx: int) -> Optional[str]:
        if idx < 0 or idx >= len(self._gnames_entries):
            return None
        cached = self._name_cache.get(idx, "__MISS__")
        if cached != "__MISS__":
            return cached  # moze byc None (celowo cachujemy negative)
        entry_ptr = self._gnames_entries[idx]
        if entry_ptr == 0:
            self._name_cache[idx] = None
            return None
        name = self.driver.read_ascii_from_wide(entry_ptr + FNAMEENTRY_TEXT)
        self._name_cache[idx] = name
        return name
