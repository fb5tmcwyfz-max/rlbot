"""Konwertuje rl_dumped_functions.json na czytelny .txt.

Uzycie:
    python rl_dump_to_txt.py                  # generuje rl_dump_full.txt
    python rl_dump_to_txt.py --out custom.txt

Zawartosc TXT:
    1. Metadata (dumped_at, base, offsety, dispatcher)
    2. Globals (GObjects, GNames RVA)
    3. Native functions (posortowane alfabetycznie): name -> RVA (conf, instances)
    4. Script functions (posortowane alfabetycznie): name -> instances + classes
    5. Class index: klasa -> lista funkcji w niej
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict


DEFAULT_JSON = r"C:\Users\Wnuczek\Desktop\Nexto\rl_dumped_functions.json"
DEFAULT_OUT  = r"C:\Users\Wnuczek\Desktop\Nexto\rl_dump_full.txt"


def hr(char="=", n=80):
    return char * n


def section(title, char="="):
    return f"\n{hr(char)}\n  {title}\n{hr(char)}\n"


def format_meta(meta: dict) -> str:
    lines = [section("META", "=")]
    lines.append(f"Dumped at:              {meta.get('dumped_at', 'n/a')}")
    lines.append(f"RL module base:         {meta.get('rl_module_base', 'n/a')}")
    lines.append(f"RL PID:                 {meta.get('rl_pid', 'n/a')}")
    lines.append("")
    off = meta.get("reflection_offsets", {})
    lines.append("Reflection offsets:")
    for k, v in off.items():
        lines.append(f"  {k:<20} = {v}")
    lines.append("")
    lines.append(f"Script dispatcher RVA:  {meta.get('script_dispatcher_rva', 'n/a')}")
    lines.append(f"Native functions:       {meta.get('native_functions_count', 0)}")
    lines.append(f"Script functions:       {meta.get('script_functions_count', 0)}")
    lines.append("")
    lines.append("Notes:")
    for note in (meta.get("notes") or "").split(". "):
        note = note.strip()
        if note:
            lines.append(f"  {note}.")
    return "\n".join(lines) + "\n"


def format_globals(globals_dict: dict) -> str:
    lines = [section("GLOBALS", "=")]
    for k, v in globals_dict.items():
        lines.append(f"  {k:<20} = {v}")
    return "\n".join(lines) + "\n"


def format_native(functions: dict) -> str:
    """Tabela: name -> best_rva | confidence | instances."""
    lines = [section(f"NATIVE FUNCTIONS ({len(functions)})", "=")]
    lines.append(f"  {'name':<50} {'best_rva':<14} {'conf':<8} {'inst':<6}")
    lines.append(f"  {'-'*50} {'-'*14} {'-'*8} {'-'*6}")

    conf_order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    sorted_names = sorted(functions.keys(),
                          key=lambda n: (conf_order.get(functions[n]["confidence"], 9), n.lower()))
    for name in sorted_names:
        info = functions[name]
        rva = info.get("best_rva") or "n/a"
        conf = info.get("confidence", "?")
        inst = info.get("instances", 0)
        # Truncate very long names
        display_name = name if len(name) <= 50 else (name[:47] + "...")
        lines.append(f"  {display_name:<50} {rva:<14} {conf:<8} {inst:<6}")
    return "\n".join(lines) + "\n"


def format_script(script_functions: dict) -> str:
    """Tabela: name -> instances + klasy (skrocone jesli >3)."""
    lines = [section(f"SCRIPT FUNCTIONS ({len(script_functions)}) - dispatched", "=")]
    lines.append("  (wszystkie te funkcje maja Func = script bytecode dispatcher)")
    lines.append("")
    lines.append(f"  {'name':<50} {'inst':<6} classes")
    lines.append(f"  {'-'*50} {'-'*6} {'-'*40}")

    for name in sorted(script_functions.keys(), key=str.lower):
        info = script_functions[name]
        inst = info.get("instances", 0)
        classes = info.get("classes", [])
        if len(classes) > 4:
            classes_str = ", ".join(classes[:4]) + f", ... (+{len(classes)-4})"
        else:
            classes_str = ", ".join(classes)
        display_name = name if len(name) <= 50 else (name[:47] + "...")
        lines.append(f"  {display_name:<50} {inst:<6} {classes_str}")
    return "\n".join(lines) + "\n"


def format_class_index(functions: dict, script_functions: dict) -> str:
    """Reverse index: klasa -> lista funkcji nalezacych do niej."""
    by_class = defaultdict(lambda: {"native": [], "script": []})

    for name, info in functions.items():
        for det in info.get("detail", []):
            cls = det.get("class", "?")
            by_class[cls]["native"].append((name, det.get("native_rva")))

    for name, info in script_functions.items():
        for cls in info.get("classes", []):
            by_class[cls]["script"].append(name)

    lines = [section(f"CLASS INDEX ({len(by_class)} classes)", "=")]
    lines.append("  Klasa -> funkcje ktore siedza w tej klasie w GObjects.")
    lines.append("")

    for cls in sorted(by_class.keys(), key=str.lower):
        entry = by_class[cls]
        nat = entry["native"]
        scr = entry["script"]
        total = len(nat) + len(scr)
        if total == 0:
            continue
        lines.append(f"  {cls}  ({len(nat)} native, {len(scr)} script)")
        # Dedup native by (name, rva) preserving order
        seen_nat = set()
        for name, rva in sorted(nat):
            key = (name, rva)
            if key in seen_nat: continue
            seen_nat.add(key)
            lines.append(f"    NATIVE  {name:<45} {rva}")
        # Dedup script names
        for name in sorted(set(scr)):
            lines.append(f"    SCRIPT  {name}")
        lines.append("")
    return "\n".join(lines) + "\n"


def format_by_rva(functions: dict) -> str:
    """Tabela posortowana po RVA - do lookupa 'co jest w tym adresie'."""
    lines = [section(f"NATIVE FUNCTIONS BY RVA ({len(functions)})", "=")]
    lines.append("  Posortowane po adresie - do lookupa 'jaka funkcja siedzi w tym RVA'.")
    lines.append("")
    lines.append(f"  {'RVA':<14} {'name':<50} {'conf':<8} {'inst':<6}")
    lines.append(f"  {'-'*14} {'-'*50} {'-'*8} {'-'*6}")

    pairs = []
    for name, info in functions.items():
        rva_str = info.get("best_rva")
        if not rva_str:
            continue
        try:
            rva_int = int(rva_str, 16)
        except Exception:
            continue
        pairs.append((rva_int, rva_str, name, info))

    for rva_int, rva_str, name, info in sorted(pairs):
        conf = info.get("confidence", "?")
        inst = info.get("instances", 0)
        display_name = name if len(name) <= 50 else (name[:47] + "...")
        lines.append(f"  {rva_str:<14} {display_name:<50} {conf:<8} {inst:<6}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Konwerter JSON->TXT dla RL dumpa")
    ap.add_argument("--json", default=DEFAULT_JSON, help="wejsciowy JSON (default: rl_dumped_functions.json)")
    ap.add_argument("--out",  default=DEFAULT_OUT,  help="wyjsciowy TXT (default: rl_dump_full.txt)")
    args = ap.parse_args()

    print(f"[*] Czytam {args.json}...")
    try:
        with open(args.json, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[!] Nie moge otworzyc JSON: {e}")
        print("    Uruchom najpierw: python rl_full_dump.py")
        return 1

    meta = data.get("meta", {})
    globs = data.get("globals", {})
    functions = data.get("functions", {})
    script_functions = data.get("script_functions", {})

    print(f"[+] Load: {len(functions)} native, {len(script_functions)} script functions")

    print("[*] Generuje sekcje...")
    sections = [
        "# " + hr("=", 76),
        "#   PRISM RL SDK FULL DUMP",
        "#   Auto-wygenerowane z rl_dumped_functions.json",
        "# " + hr("=", 76),
        "",
        format_meta(meta),
        format_globals(globs),
        format_native(functions),
        format_by_rva(functions),
        format_script(script_functions),
        format_class_index(functions, script_functions),
        "",
        "# " + hr("=", 76),
        "#   END OF DUMP",
        "# " + hr("=", 76),
    ]

    txt = "\n".join(sections)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(txt)

    import os
    size = os.path.getsize(args.out)
    print(f"[+] Zapisano: {args.out}")
    print(f"    Rozmiar: {size:,} bajtow ({size/1024:.1f} KB)")
    print(f"    Linii:   {txt.count(chr(10)):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
