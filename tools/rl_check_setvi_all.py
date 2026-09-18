"""Sprawdz vtable+0x830 dla WSZYSTKICH klas pochodnych Vehicle_TA i pokaz co siedzi."""
import sys
sys.path.insert(0, r"C:\Users\Wnuczek\Desktop\Nexto")
from prism_sdk.low.driver import Driver
from prism_sdk.low.reflection import Reflection

CANDIDATES = [
    "Car_TA", "Car_Freeplay_TA", "Car_Season_TA", "Car_KnockOut_TA",
    "Car_Breakout_TA", "Vehicle_TA", "Vehicle_Freeplay_TA",
]

def main():
    drv = Driver()
    if not drv.attach(): return 1
    refl = Reflection(drv)
    if not refl.rebuild(): return 1

    print(f"[+] module_base = 0x{drv.module_base:X}")
    print()
    print(f"    {'class':<25} {'instance':<18} {'vtable RVA':<14} {'slot 0x830 fn RVA'}")
    print(f"    {'-'*25} {'-'*18} {'-'*14} {'-'*20}")

    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)

    setvi_rvas = {}
    for cls in CANDIDATES:
        # sprawdz WSZYSTKIE instancje (nie tylko highest) - wybieramy pierwsza NIE-CDO
        instances = refl._by_class.get(cls, [])
        if not instances:
            print(f"    {cls:<25} (brak instancji)")
            continue
        # Pierwszy non-CDO (FName.Number > 0)
        addr = 0
        for idx, a in instances:
            try:
                num = drv.read_i32(a + 0x4C)
                if num > 0:
                    addr = a; break
            except Exception: pass
        if not addr:
            # CDO fallback
            addr = max(instances, key=lambda m: m[0])[1]
        vtable = drv.read_ptr(addr)
        if not vtable:
            print(f"    {cls:<25} 0x{addr:X}  (no vtable)")
            continue
        fn = drv.read_ptr(vtable + 0x830)
        rva = fn - drv.module_base if fn else 0
        vtable_rva = vtable - drv.module_base
        print(f"    {cls:<25} 0x{addr:<16X} 0x{vtable_rva:<12X} 0x{rva:X}")
        setvi_rvas.setdefault(rva, []).append(cls)

    # Pokaz disasm kazdego unikalnego RVA
    print()
    print("[*] Disasm kandydatow (pierwsze 20 instrukcji):")
    for rva, classes in setvi_rvas.items():
        if rva == 0: continue
        print()
        print(f"    ===== RVA 0x{rva:X}  (klasy: {', '.join(classes)}) =====")
        data = drv.read_bytes(drv.module_base + rva, 200)
        if not data: continue
        for i, ins in enumerate(md.disasm(data, drv.module_base + rva)):
            if i >= 15: break
            raw = " ".join(f"{b:02X}" for b in ins.bytes)
            note = ""
            if "movups" in ins.mnemonic and ("[rcx" in ins.op_str or "[rdi" in ins.op_str):
                note = "  *** MOVE TO INPUT ***"
            print(f"      +0x{ins.address - (drv.module_base + rva):03X}  {ins.mnemonic:<8} {ins.op_str}{note}")

if __name__ == "__main__":
    sys.exit(main())
