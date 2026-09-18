"""Prosty dumper — wyciaga native RVA dla KAZDEJ UFunction w Rocket League.

Uzycie:
    python rl_full_dump.py                                # dump wszystkiego
    python rl_full_dump.py --update-offsets              # auto-patch SETVI_RVA
    python rl_full_dump.py --filter SetVehicle,Boost     # tylko pasujace nazwy
    python rl_full_dump.py --out custom.json             # inny plik output

Wyjscie:
    rl_dumped_functions.json  {
      "meta": {...},
      "globals": {"GObjects_RVA": "0x...", "GNames_RVA": "0x..."},
      "functions": {
        "SetVehicleInput": [
          {"native_rva": "0x36E050", "gobjects_idx": 76631, "class": "Function"},
          ...
        ],
        ...
      }
    }

Do wyszukania nowego RVA po update RL:
    python rl_full_dump.py --filter <nazwa_funkcji>
    # -> zwraca native_rva dla wszystkich instances tej funkcji
    # -> jesli 3+ celuja w to samo -> to jest prawidlowy RVA
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, r"C:\Users\Wnuczek\Desktop\Nexto")
from prism_sdk.low.driver import Driver
from prism_sdk.low.reflection import Reflection

# Standardowe UE3 offsety (potwierdzone przez rlsdk-main + rlsdk-main/packages/epic-games/offsets/Core.ts).
UOBJECT_NAME_OFF   = 0x48   # FName Index -> GNames
UOBJECT_CLASS_OFF  = 0x50   # UClass* Class
UFUNCTION_FUNC_OFF = 0x158  # void* Func (native handler LUB script dispatcher)

# W tej wersji RL wspolny script bytecode interpreter siedzi pod tym RVA
# (~34% UFunction wskazuje na niego bo to sa script functions, nie native).
# Wykrywany dynamicznie przez find_script_dispatcher() ale mamy fallback:
SCRIPT_DISPATCHER_RVA_HINT = 0x36E050

# Kernel-mode / kod range dla RL x64 (native ptr musi wpasc w image + rozsadny .text)
def is_plausible_rva(rva: int) -> bool:
    return 0x1000 <= rva <= 0x02500000


def find_script_dispatcher(refl: Reflection, drv: Driver, sample_size: int = 500) -> int | None:
    """Znajdz najczestszy RVA pod UFunction.Func offset - to jest wspolny
    script bytecode interpreter (~34% UFunctions go dzieli). Native functions
    beda mialy unikalne RVA rozne od tego."""
    from collections import Counter
    rva_counts: Counter = Counter()
    collected = 0
    for class_name, instances in refl._by_class.items():
        for idx, addr in instances[:3]:  # kilka z kazdej klasy
            if collected >= sample_size:
                break
            try:
                ptr = drv.read_ptr(addr + UFUNCTION_FUNC_OFF)
                if ptr == 0 or ptr < drv.module_base:
                    continue
                if ptr > drv.module_base + 0x10000000:
                    continue
                rva = ptr - drv.module_base
                if is_plausible_rva(rva):
                    rva_counts[rva] += 1
                    collected += 1
            except Exception:
                continue
        if collected >= sample_size:
            break
    if not rva_counts:
        return None
    top_rva, top_cnt = rva_counts.most_common(1)[0]
    ratio = top_cnt / collected
    # Jesli >20% funkcji dzieli 1 RVA to na 100% jest to dispatcher
    if ratio > 0.20:
        return top_rva
    return None


def dump_all_functions(refl: Reflection, drv: Driver, script_dispatcher: int | None) -> tuple[dict, dict, int]:
    """Zwraca (native_functions, script_functions, total).

    native_functions: nazwa -> lista instances z UNIKALNYM native RVA (prawdziwe C++)
    script_functions: nazwa -> lista instances wskazujacych na script dispatcher
    """
    native_by_name = defaultdict(list)
    script_by_name = defaultdict(list)

    total = 0
    for class_name, instances in refl._by_class.items():
        for idx, addr in instances:
            total += 1
            try:
                native_ptr = drv.read_ptr(addr + UFUNCTION_FUNC_OFF)
                if native_ptr == 0:
                    continue
                if native_ptr < drv.module_base or native_ptr > drv.module_base + 0x10000000:
                    continue
                rva = native_ptr - drv.module_base
                if not is_plausible_rva(rva):
                    continue

                name_idx = drv.read_i32(addr + UOBJECT_NAME_OFF)
                name = refl._resolve_name(name_idx)
                if not name:
                    continue

                entry = {
                    "native_rva": f"0x{rva:X}",
                    "native_rva_int": rva,
                    "gobjects_idx": idx,
                    "class": class_name,
                }

                if script_dispatcher is not None and rva == script_dispatcher:
                    script_by_name[name].append(entry)
                else:
                    native_by_name[name].append(entry)
            except Exception:
                continue

    return dict(native_by_name), dict(script_by_name), total


def group_by_best_rva(instances: list) -> dict:
    """Dla listy instances o tej samej nazwie, znajdz najczestsze native_rva
    (konsensus = prawidlowa funkcja, single-hit = wrapper/alias/subclass)."""
    if not instances:
        return {"instances": 0, "best_rva": None, "confidence": "none"}
    rva_counts = Counter(i["native_rva_int"] for i in instances)
    top = rva_counts.most_common(1)[0]
    best_rva = top[0]
    best_count = top[1]
    confidence = "high" if best_count >= 3 else ("medium" if best_count == 2 else "low")
    return {
        "instances": len(instances),
        "best_rva": f"0x{best_rva:X}",
        "best_rva_count": best_count,
        "confidence": confidence,
        "all_rvas": [f"0x{r:X} ({c}x)" for r, c in rva_counts.most_common()],
    }


def maybe_update_kernel_hook(new_setvi_rva: int, dry_run: bool = False) -> None:
    """Auto-patch SETVI_RVA w kernel_hook.py (z backupem)."""
    khook = r"C:\Users\Wnuczek\Desktop\Nexto\prism_sdk\control\kernel_hook.py"
    try:
        with open(khook, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"[!] Nie moge przeczytac {khook}: {e}")
        return

    m = re.search(r"^SETVI_RVA\s*=\s*(0x[0-9A-Fa-f]+)", content, re.MULTILINE)
    if not m:
        print("[!] SETVI_RVA nie znalezione w kernel_hook.py")
        return
    old = m.group(1)
    new = f"0x{new_setvi_rva:X}"
    if old.lower() == new.lower():
        print(f"[=] SETVI_RVA juz ustawione na {new}, nic do zmiany")
        return

    if dry_run:
        print(f"[dry-run] SETVI_RVA: {old} -> {new}")
        return

    # Backup
    try:
        with open(khook + ".bak", "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass
    new_content = re.sub(
        r"^SETVI_RVA\s*=\s*0x[0-9A-Fa-f]+",
        f"SETVI_RVA = {new}",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    with open(khook, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"[+] SETVI_RVA: {old} -> {new}  (backup: kernel_hook.py.bak)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Prosty dumper UFunction RVAs dla RL")
    ap.add_argument("--out", default=r"C:\Users\Wnuczek\Desktop\Nexto\rl_dumped_functions.json",
                    help="Sciezka do output JSON")
    ap.add_argument("--filter", default=None,
                    help="Tylko funkcje ktorych nazwa zawiera podany substring(i), przecinek=OR")
    ap.add_argument("--update-offsets", action="store_true",
                    help="Dodatkowo auto-patch SETVI_RVA w prism_sdk/control/kernel_hook.py")
    ap.add_argument("--show-only", type=int, default=50,
                    help="Ile top-funkcji pokazac na stdout (default 50)")
    args = ap.parse_args()

    print("[*] Attach + reflection...")
    drv = Driver()
    if not drv.attach():
        print("[!] Attach failed - driver zaladowany? RL uruchomiony?")
        return 1
    print(f"[+] PID={drv.pid}  module_base=0x{drv.module_base:X}")

    refl = Reflection(drv)
    if not refl.rebuild():
        print("[!] Reflection.rebuild() failed - GObjects RVA moze byc stary")
        print(f"    Aktualne w reflection.py: GOBJECTS_RVA=0x{__import__('prism_sdk.low.reflection', fromlist=['GOBJECTS_RVA']).GOBJECTS_RVA:X}")
        print("    Uruchom rl_dump_globals.py zeby znalezc nowy")
        return 1

    named_count = sum(len(v) for v in refl._by_class.values())
    print(f"[+] Reflection rebuilt: {named_count} named objects, {len(refl._by_class)} unique class names")

    print("[*] Wykrywam script bytecode dispatcher (wspolny RVA dla script UFunctions)...")
    dispatcher = find_script_dispatcher(refl, drv)
    if dispatcher is not None:
        print(f"[+] Script dispatcher: 0x{dispatcher:X}  (funkcje wskazujace tu = script, nie native)")
    else:
        print("[*] Nie wykryto pojedynczego dispatcher-a - traktuje wszystko jako native")

    print("[*] Ekstraktuje UFunction native pointers...")
    t0 = time.time()
    functions, script_functions, scanned = dump_all_functions(refl, drv, dispatcher)
    print(f"[+] Zeskanowano {scanned} obiektow w {time.time()-t0:.1f}s -> "
          f"{len(functions)} native, {len(script_functions)} script")

    # Grupowanie: nazwa -> best_rva + confidence
    grouped = {name: group_by_best_rva(inst) for name, inst in functions.items()}

    # Filter
    if args.filter:
        needles = [n.strip().lower() for n in args.filter.split(",") if n.strip()]
        grouped = {n: v for n, v in grouped.items()
                   if any(needle in n.lower() for needle in needles)}
        print(f"[*] Filter '{args.filter}' -> {len(grouped)} matches")

    # Sort: high confidence pierw, potem po nazwie
    conf_order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    sorted_names = sorted(grouped.keys(),
                          key=lambda n: (conf_order[grouped[n]["confidence"]], n))

    # Stdout - top N
    print()
    print(f"[*] Top {min(args.show_only, len(sorted_names))} functions:")
    print(f"  {'name':<45} {'best_rva':<12} {'conf':<8} {'inst':<5}")
    print(f"  {'-'*45} {'-'*12} {'-'*8} {'-'*5}")
    for name in sorted_names[:args.show_only]:
        info = grouped[name]
        print(f"  {name[:44]:<45} {(info['best_rva'] or 'n/a'):<12} "
              f"{info['confidence']:<8} {info['instances']:<5}")

    if len(sorted_names) > args.show_only:
        print(f"  ... ({len(sorted_names) - args.show_only} more, see JSON)")

    # JSON output
    from prism_sdk.low.reflection import GOBJECTS_RVA, GNAMES_RVA
    output = {
        "meta": {
            "dumped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rl_module_base": f"0x{drv.module_base:X}",
            "rl_pid": drv.pid,
            "reflection_offsets": {
                "UObject.Name":   f"0x{UOBJECT_NAME_OFF:X}",
                "UObject.Class":  f"0x{UOBJECT_CLASS_OFF:X}",
                "UFunction.Func": f"0x{UFUNCTION_FUNC_OFF:X}",
            },
            "script_dispatcher_rva": f"0x{dispatcher:X}" if dispatcher else None,
            "script_functions_count": len(script_functions),
            "native_functions_count": len(functions),
            "notes": "confidence=high -> >=3 UFunction instances celuja w te sama native. "
                     "medium -> 2. low -> 1 (moze byc wrapper/alias, weryfikuj recznie). "
                     "Script functions (osobna sekcja) uzywaja bytecode interpretera "
                     "spod script_dispatcher_rva - hookowanie tego RVA lapie WSZYSTKIE "
                     "script calls, filtruj po argumentach w hooku.",
        },
        "globals": {
            "GObjects_RVA": f"0x{GOBJECTS_RVA:X}",
            "GNames_RVA":   f"0x{GNAMES_RVA:X}",
        },
        "functions": {
            name: {
                "best_rva":       grouped[name]["best_rva"],
                "confidence":     grouped[name]["confidence"],
                "instances":      grouped[name]["instances"],
                "all_rvas":       grouped[name]["all_rvas"],
                "detail":         functions[name],
            }
            for name in sorted_names
        },
        "script_functions": {
            name: {
                "instances": len(script_functions[name]),
                "classes":   sorted(set(i["class"] for i in script_functions[name])),
            }
            for name in sorted(script_functions.keys())
        },
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print()
    print(f"[+] JSON output: {args.out}  ({len(sorted_names)} functions)")

    # Auto-update SetVehicleInput
    if args.update_offsets:
        setvi = grouped.get("SetVehicleInput")
        if setvi and setvi["best_rva"]:
            new_rva = int(setvi["best_rva"], 16)
            print()
            print(f"[*] --update-offsets: SetVehicleInput najlepszy RVA = {setvi['best_rva']} "
                  f"(confidence={setvi['confidence']}, {setvi['best_rva_count']} instances)")
            if setvi["confidence"] in ("high", "medium"):
                maybe_update_kernel_hook(new_rva)
            else:
                print(f"[!] Confidence '{setvi['confidence']}' - nie updateuje automatycznie. "
                      "Uzyj recznie: SETVI_RVA = " + setvi["best_rva"])
        else:
            print("[!] SetVehicleInput nie znaleziono - nic do update'u")

    return 0


if __name__ == "__main__":
    sys.exit(main())
