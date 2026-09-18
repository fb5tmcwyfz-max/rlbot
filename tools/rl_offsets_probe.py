"""Runtime probe UProperty.Offset_Internal offset.
Iteruje UProperty i pokazuje raw wartosci pod kandydatami offsetu w strukturze
UProperty - wybieramy ten offset ktory ma sensowne wartosci (byte offset w klasie)."""
from __future__ import annotations
import sys
sys.path.insert(0, r"C:\Users\Wnuczek\Desktop\Nexto")
from prism_sdk.low.driver import Driver
from prism_sdk.low.reflection import Reflection, UOBJECT_NAME_IDX, UOBJECT_CLASS

UOBJECT_OUTER = 0x40  # znalezione poprzednim probe


def find_props_in(drv, refl, class_name, want_names):
    """Zwraca [(name, addr, meta_class), ...] - wszystkie UProperty ktorych Outer
    ma name==class_name i sami maja name w want_names."""
    found = []
    seen = set()
    for prop_name in want_names:
        candidates = refl._by_class.get(prop_name, [])
        for idx, addr in candidates:
            if addr in seen:
                continue
            try:
                outer = drv.read_ptr(addr + UOBJECT_OUTER)
                if outer == 0:
                    continue
                outer_name = refl._resolve_name(drv.read_i32(outer + UOBJECT_NAME_IDX))
                if outer_name != class_name:
                    continue
                meta_cls = drv.read_ptr(addr + UOBJECT_CLASS)
                meta_name = refl._resolve_name(drv.read_i32(meta_cls + UOBJECT_NAME_IDX)) if meta_cls else "?"
                if not meta_name.endswith("Property"):
                    continue
                found.append((prop_name, addr, meta_name))
                seen.add(addr)
            except Exception:
                pass
    return found


def main():
    print("[*] Attach + reflection...")
    drv = Driver()
    if not drv.attach():
        print("[!] Attach failed"); return 1
    refl = Reflection(drv)
    if not refl.rebuild():
        print("[!] Reflection.rebuild() failed"); return 1
    print(f"[+] Reflection: {sum(len(v) for v in refl._by_class.values())} obiektow, "
          f"{len(refl._by_class)} unique classes")
    print()

    # Ball_TA - zbieramy wszystkie znane property, patrzymy jakie wartosci sa pod
    # kazdym kandydatem offsetu w strukturze UProperty
    want = ["Radius", "GameEvent", "PredictedPositions", "OldLocation", "TrajectoryComponent",
            "HitTeamNum", "LastHitWorldTime", "ReplicatedScale", "ReplicatedGravityScale",
            "VisualRadius", "ReplicatedExplosionData"]

    print("[*] Szukam UProperty w Ball_TA...")
    props = find_props_in(drv, refl, "Ball_TA", want)
    if not props:
        print("[!] Nie znaleziono zadnego UProperty w Ball_TA"); return 2
    print(f"[+] Znaleziono {len(props)} UProperty:")
    for name, addr, meta in props:
        print(f"    {name:<30} @ 0x{addr:X}  meta={meta}")
    print()

    # Wypisujemy raw values pod kandydatami 0x30-0x80 (int32) dla kazdego property.
    # Ten kandydat gdzie wartosci sa w 0x80-0x1000 range = to jest PropertyOffset.
    candidates = list(range(0x30, 0x80, 4))
    print(f"[*] Wartosci int32 pod kandydatami offsetu (szukamy tabeli gdzie wartosci "
          f"wygladaja jak byte offsety w klasie - rosnace, w zakresie 0x80-0xFFF):")
    print()

    # Print header
    hdr = f"    {'name':<25} "
    for c in candidates:
        hdr += f"{'+0x%02X' % c:>8} "
    print(hdr)
    print("    " + "-" * (25 + 9 * len(candidates)))

    # Print rows
    rows_by_offset = {c: [] for c in candidates}
    for name, addr, meta in props:
        row = f"    {name[:25]:<25} "
        for c in candidates:
            try:
                val = drv.read_i32(addr + c)
                row += f"{val & 0xFFFFFFFF:>8X} "
                rows_by_offset[c].append(val)
            except Exception:
                row += "     ??? "
                rows_by_offset[c].append(None)
        print(row)

    # Ocena kolumn - ktora ma sensowne wartosci?
    print()
    print("[*] Ocena kandydatow (liczba wartosci w range 0x40-0xFFF = plausible field offset):")
    print()
    for c in candidates:
        vals = [v for v in rows_by_offset[c] if v is not None]
        plausible = sum(1 for v in vals if 0x40 <= v <= 0xFFF)
        distinct = len(set(vals))
        print(f"    +0x{c:02X}: plausible={plausible}/{len(vals)}  distinct={distinct}/{len(vals)}  "
              f"sample={[hex(v) for v in vals[:5]]}")
    print()

    # Auto-guess: kolumna z max plausible, potem max distinct
    scored = []
    for c in candidates:
        vals = [v for v in rows_by_offset[c] if v is not None]
        plausible = sum(1 for v in vals if 0x40 <= v <= 0xFFF)
        distinct = len(set(vals))
        scored.append((plausible, distinct, c))
    scored.sort(reverse=True)
    best = scored[0]
    print(f"[+] NAJPRAWDOPODOBNIEJ: UPROPERTY_OFFSET = +0x{best[2]:X}  "
          f"(plausible={best[0]}, distinct={best[1]})")

    # Wypisz Ball_TA offsety pod tym kandydatem
    print()
    print(f"[+] Ball_TA offsety pod +0x{best[2]:X}:")
    for name, addr, meta in props:
        try:
            v = drv.read_i32(addr + best[2])
            print(f"    {name:<30} = 0x{v & 0xFFFFFFFF:04X}   (meta={meta})")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
