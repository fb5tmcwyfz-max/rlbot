"""Weryfikator offsetow pol - znajduje live instance i sprawdza czy kazdy
offset w offsets.py zwraca sensowna wartosc. Dla broken offsetow robi scan.

Uzycie:
    python rl_verify_offsets.py                    # tylko raport
    python rl_verify_offsets.py --patch            # patchuje offsets.py
"""
from __future__ import annotations
import argparse
import re
import struct
import sys
sys.path.insert(0, r"C:\Users\Wnuczek\Desktop\Nexto")
from prism_sdk.low.driver import Driver
from prism_sdk.low.reflection import Reflection, UOBJECT_NAME_IDX, UOBJECT_CLASS

# Testy dla kazdego pola: (class_python_name, field_python_name, expected_class_in_reflection,
#                          old_offset, type_size, validator_func, hint_for_scan)
# validator zwraca True/False - czy wartosc pod tym offsetem wyglada OK.

def is_float_in(low, high):
    def check(val_bytes):
        if len(val_bytes) < 4: return False
        f, = struct.unpack("<f", val_bytes[:4])
        return low <= f <= high
    return check

def is_ptr_valid():
    def check(val_bytes):
        if len(val_bytes) < 8: return False
        p, = struct.unpack("<Q", val_bytes[:8])
        return 0x10000 <= p <= 0x00007FFFFFFF0000
    return check

def is_ptr_zero_or_valid():
    def check(val_bytes):
        if len(val_bytes) < 8: return False
        p, = struct.unpack("<Q", val_bytes[:8])
        return p == 0 or (0x10000 <= p <= 0x00007FFFFFFF0000)
    return check

def is_int_in(low, high):
    def check(val_bytes):
        if len(val_bytes) < 4: return False
        i, = struct.unpack("<i", val_bytes[:4])
        return low <= i <= high
    return check

def is_u8_in(low, high):
    def check(val_bytes):
        if len(val_bytes) < 1: return False
        return low <= val_bytes[0] <= high
    return check

def is_tarray_valid():
    """TArray = {ptr, count, max}. count/max nienagatywne, count<=max, ptr valid or (count==0 & ptr==0)."""
    def check(val_bytes):
        if len(val_bytes) < 16: return False
        ptr, count, mx = struct.unpack("<Qii", val_bytes[:16])
        if count == 0 and mx == 0 and ptr == 0: return True
        if not (0 <= count <= mx <= 100000): return False
        return 0x10000 <= ptr <= 0x00007FFFFFFF0000
    return check


# (klasa_reflection, field_name_w_offsets_py, old_offset, type_size, validator)
# Dobrane pod aktualne offsets.py.
TESTS = [
    # Actor - baza dla wszystkich renderable
    ("Ball_TA", "Actor.LOCATION",              0x90,  12, is_float_in(-30000, 30000)),
    ("Ball_TA", "Actor.VELOCITY",              0x1A8, 12, is_float_in(-6000, 6000)),
    ("Ball_TA", "Actor.ANGULAR_VELOCITY",      0x1C0, 12, is_float_in(-100, 100)),

    # RBActor - fizyka replicated
    ("Ball_TA", "RBActor.REPLICATED_RBSTATE",  0x610, 4,  is_float_in(-30000, 30000)),

    # Ball
    ("Ball_TA", "Ball.RADIUS",                 0x0888, 4, is_float_in(50, 200)),
    ("Ball_TA", "Ball.VISUAL_RADIUS",          0x0890, 4, is_float_in(50, 200)),
    ("Ball_TA", "Ball.HIT_TEAM_NUM",           0x08A9, 1, is_u8_in(0, 2)),
    ("Ball_TA", "Ball.LAST_HIT_WORLD_TIME",    0x08C8, 4, is_float_in(-1, 100000)),
    ("Ball_TA", "Ball.GAME_EVENT",             0x0900, 8, is_ptr_valid()),
    ("Ball_TA", "Ball.PREDICTED_POSITIONS",    0x0970, 16, is_tarray_valid()),

    # Vehicle/Car (znajdujemy przez find_by_ancestor Vehicle_TA)
    ("Car_TA", "Vehicle.CAR_MESH",             0x7D8, 8, is_ptr_zero_or_valid()),
    ("Car_TA", "Vehicle.INPUT",                0x7FC, 4, is_float_in(-2, 2)),  # THROTTLE float
    ("Car_TA", "Vehicle.PLAYER_CONTROLLER",    0x828, 8, is_ptr_zero_or_valid()),
    ("Car_TA", "Vehicle.PRI",                  0x830, 8, is_ptr_zero_or_valid()),
    ("Car_TA", "Vehicle.BOOST_COMPONENT",      0x870, 8, is_ptr_zero_or_valid()),
    ("Car_TA", "Vehicle.JUMP_COMPONENT",       0x888, 8, is_ptr_zero_or_valid()),

    # Car
    ("Car_TA", "Car.GAME_EVENT",               0x0AB8, 8, is_ptr_zero_or_valid()),

    # PRI (znajdujemy przez find_by_ancestor)
    ("PRI_TA", "PRI.PLAYER_ID",                0x02A8, 4, is_int_in(0, 200)),
    ("PRI_TA", "PRI.TEAM",                     0x02B0, 8, is_ptr_zero_or_valid()),
    ("PRI_TA", "PRI.MATCH_SCORE",              0x0458, 4, is_int_in(0, 999999)),
    ("PRI_TA", "PRI.MATCH_GOALS",              0x045C, 4, is_int_in(0, 999)),
    ("PRI_TA", "PRI.CAR",                      0x0498, 8, is_ptr_zero_or_valid()),

    # GameEvent Soccar (znajdujemy przez find_by_ancestor)
    ("GameEvent_Soccar_TA", "GameEventSoccar.PLAYERS",        0x0330, 16, is_tarray_valid()),
    ("GameEvent_Soccar_TA", "GameEventSoccar.PRIS",           0x0340, 16, is_tarray_valid()),
    ("GameEvent_Soccar_TA", "GameEventSoccar.CARS",           0x0350, 16, is_tarray_valid()),
    ("GameEvent_Soccar_TA", "GameEventSoccar.GAME_BALLS",     0x0908, 16, is_tarray_valid()),

    # Team
    ("Team_Soccar_TA", "TeamInfo.SCORE",                      0x027C, 4, is_int_in(0, 20)),
    ("Team_Soccar_TA", "TeamInfo.TEAM_INDEX",                 0x0280, 4, is_int_in(0, 1)),
    ("Team_Soccar_TA", "Team.MEMBERS",                        0x0318, 16, is_tarray_valid()),
]


def get_instance(refl, class_name, drv):
    """Zwraca adres zywej instancji klasy - najpierw exact match, potem po ancestor,
    zawsze pomija metaklasy (CDO)."""
    addr = None
    # exact
    exact = refl._by_class.get(class_name, [])
    if exact:
        addr = max(exact, key=lambda m: m[0])[1]
    # ancestor fallback dla klas ktore maja subklasy per-tryb
    if addr is None or _is_meta(drv, addr):
        cand = refl.highest_by_ancestor(class_name)
        if cand:
            addr = cand
    return addr


def _is_meta(drv, addr):
    """FName.Number = 0 = metaklasa (CDO)."""
    try:
        num = drv.read_i32(addr + 0x4C)
        return num == 0
    except Exception:
        return True


def scan_for_offset(drv, instance_addr, validator, type_size, hint_offset, window=0x400):
    """Skanuj +-window wokol hint_offset, wracaj offset ktorych wartosc walidator zaakceptuje.
    Zwraca list of (offset, sample_bytes)."""
    results = []
    step = 4  # scan co 4 bajty (natural alignment)
    lo = max(0, hint_offset - window)
    hi = hint_offset + window
    for off in range(lo, hi + 1, step):
        try:
            b = drv.read_bytes(instance_addr + off, type_size)
            if len(b) == type_size and validator(b):
                results.append((off, b))
        except Exception:
            pass
    return results


def fmt_value(b, type_size):
    if type_size >= 8:
        p, = struct.unpack("<Q", b[:8])
        return f"ptr=0x{p:X}"
    if type_size >= 4:
        i, = struct.unpack("<i", b[:4])
        f, = struct.unpack("<f", b[:4])
        return f"i32={i} f32={f:.3f}"
    return f"u8={b[0]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patch", action="store_true",
                    help="Patchuj offsets.py z nowymi wartosciami (backup .bak)")
    args = ap.parse_args()

    print("[*] Attach + reflection...")
    drv = Driver()
    if not drv.attach():
        print("[!] Attach failed"); return 1
    refl = Reflection(drv)
    if not refl.rebuild():
        print("[!] Reflection.rebuild() failed"); return 1
    print(f"[+] Reflection: {sum(len(v) for v in refl._by_class.values())} obiektow")
    print()

    # Cache addresow per klasa
    addr_cache = {}
    for cls_name, _, _, _, _ in TESTS:
        if cls_name not in addr_cache:
            addr_cache[cls_name] = get_instance(refl, cls_name, drv)

    print("[*] Live instance addresses:")
    for c, a in addr_cache.items():
        print(f"    {c:<30} = {'0x%X' % a if a else 'NOT FOUND'}")
    print()

    print("[*] Verify + scan:")
    print(f"    {'field':<45} {'old':<8} {'status':<10} {'new':<8} {'value'}")
    print(f"    {'-'*45} {'-'*8} {'-'*10} {'-'*8} {'-'*40}")

    proposals = {}   # field -> new offset (dla patch)
    for cls_name, field_path, old_off, type_size, validator in TESTS:
        addr = addr_cache.get(cls_name)
        if not addr:
            print(f"    {field_path:<45} 0x{old_off:<6X} NOINST")
            continue
        try:
            b = drv.read_bytes(addr + old_off, type_size)
            if len(b) == type_size and validator(b):
                print(f"    {field_path:<45} 0x{old_off:<6X} OK         -        {fmt_value(b, type_size)}")
                continue
        except Exception:
            pass

        # Old failed - scan
        hits = scan_for_offset(drv, addr, validator, type_size, old_off)
        # Preferuj hit najblizszy old_off
        if hits:
            hits.sort(key=lambda h: abs(h[0] - old_off))
            new_off, sample = hits[0]
            print(f"    {field_path:<45} 0x{old_off:<6X} STALE      0x{new_off:<6X} {fmt_value(sample, type_size)}")
            proposals[field_path] = new_off
        else:
            print(f"    {field_path:<45} 0x{old_off:<6X} BROKEN     -        (nic nie pasuje w +-0x400)")

    print()
    if not proposals:
        print("[+] Zaden offset nie wymaga zmiany - wszystko OK lub brak instance.")
        return 0

    print(f"[*] Proponowane zmiany ({len(proposals)}):")
    for k, v in proposals.items():
        print(f"    {k:<45} -> 0x{v:X}")

    if not args.patch:
        print()
        print("[i] Uruchom z --patch aby zaaplikowac do offsets.py")
        return 0

    # Patchuj offsets.py
    off_py = r"C:\Users\Wnuczek\Desktop\Nexto\prism_sdk\low\offsets.py"
    with open(off_py, encoding="utf-8") as f:
        src = f.read()
    with open(off_py + ".bak", "w", encoding="utf-8") as f:
        f.write(src)

    changed = 0
    for field_path, new_off in proposals.items():
        # field_path = "Ball.RADIUS" lub "Vehicle.INPUT" etc
        # W offsets.py: `class Ball: ... RADIUS = 0x0888    # ...`
        # Znajdz w bloku class Ball i podmien wartosc
        cls, field = field_path.split(".", 1)
        # Regex: `    FIELD = 0x...` z ewentualnym komentarzem po
        pattern = re.compile(
            rf"(\n\s+{re.escape(field)}\s*=\s*)0x[0-9A-Fa-f]+",
            re.MULTILINE
        )
        new_src, n = pattern.subn(rf"\g<1>0x{new_off:04X}", src, count=1)
        if n > 0:
            src = new_src
            changed += 1
        else:
            print(f"[!] Nie znalazlem w offsets.py: {field_path}")

    with open(off_py, "w", encoding="utf-8") as f:
        f.write(src)
    print()
    print(f"[+] Zaktualizowano {changed}/{len(proposals)} pol w offsets.py (backup .bak)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
