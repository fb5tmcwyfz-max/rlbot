"""Znajdz vtable slot Car_TA ktory wskazuje na funkcje piszaca do [rcx + 0x7FC]
(offset INPUT w Vehicle_TA). To jest SetVehicleInput w bieacym buildzie."""
import sys
sys.path.insert(0, r"C:\Users\Wnuczek\Desktop\Nexto")
from prism_sdk.low.driver import Driver
from prism_sdk.low.reflection import Reflection
from prism_sdk.low import offsets as O

CAR = ("Car_TA", "Car_Freeplay_TA", "Car_Season_TA", "Car_KnockOut_TA", "Vehicle_TA")

def main():
    drv = Driver()
    if not drv.attach(): return 1
    refl = Reflection(drv)
    if not refl.rebuild(): return 1

    car = 0; used = ""
    for n in CAR:
        a = refl.highest(n)
        if a: car, used = a, n; break
    if not car:
        print("[!] no Car"); return 1
    vtable = drv.read_ptr(car)
    print(f"[+] {used} @ 0x{car:X}  vtable=0x{vtable:X}  INPUT_off=0x{O.Vehicle.INPUT:X}")
    print()

    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)

    input_off = O.Vehicle.INPUT
    hits = []
    print(f"[*] Scanning vtable [0..0x1000] for functions writing to [rcx + 0x{input_off:X}]...")
    for slot in range(0, 0x1000, 8):
        try:
            fn = drv.read_ptr(vtable + slot)
            if fn == 0: continue
            rva = fn - drv.module_base
            if not (0x1000 <= rva <= 0x02500000): continue
            code = drv.read_bytes(fn, 256)
            if len(code) < 16: continue
            for ins in md.disasm(code, fn):
                if ins.address - fn > 200: break
                s = ins.op_str
                # Szukamy write do [rcx + input_off] lub blisko
                if ("[rcx" in s or "[rdi" in s or "[rbx" in s) and "," in s:
                    # znajdz offset w [reg + 0xNNN]
                    for reg in ("rcx", "rdi", "rbx"):
                        if f"[{reg}" in s:
                            part = s.split(f"[{reg}")[1].split("]")[0]
                            try:
                                if "+" in part:
                                    off = int(part.split("+")[1].strip(), 0)
                                    if abs(off - input_off) <= 0x20:
                                        hits.append((slot, rva, off, reg, ins.mnemonic, s))
                                        break
                            except: pass
                    if hits and hits[-1][0] == slot:
                        break
        except: pass

    print(f"[+] Znaleziono {len(hits)} slotow piszacych blisko INPUT:")
    print()
    print(f"    {'slot':<8} {'rva':<12} {'off':<8} {'reg':<5} {'mnemonic':<10} op_str")
    print(f"    {'-'*8} {'-'*12} {'-'*8} {'-'*5} {'-'*10} {'-'*50}")
    for slot, rva, off, reg, mn, s in hits:
        print(f"    0x{slot:<6X} 0x{rva:<10X} 0x{off:<6X} {reg:<5} {mn:<10} {s}")

    if hits:
        # Wybierz slot ktorego offset dokladnie matches INPUT
        best = None
        for h in hits:
            if h[2] == input_off:
                best = h; break
        if not best: best = hits[0]
        slot, rva, off, reg, mn, s = best
        print()
        print(f"[+] SETVI_RVA (nowy) = 0x{rva:X}   (vtable slot 0x{slot:X})")
        print(f"    Dla kernel_hook.py: SETVI_RVA = 0x{rva:X}")
    else:
        print("[!] Zero slotow z write do +INPUT - moze SetVehicleInput uzywa innego offsetu")
        # Fallback - pokaz cokolwiek z zapisami do 0x7NN/0x8NN
        print()
        print("[*] Fallback: sloty z zapisami do +0x7XX / +0x8XX:")
        for slot in range(0, 0x1000, 8):
            try:
                fn = drv.read_ptr(vtable + slot)
                if fn == 0: continue
                rva = fn - drv.module_base
                if not (0x1000 <= rva <= 0x02500000): continue
                code = drv.read_bytes(fn, 100)
                if len(code) < 16: continue
                # szybki hex-match: 0x7?? lub 0x8?? w little-endian
                for i in range(len(code) - 3):
                    if code[i+3] == 0 and code[i+2] == 0 and code[i+1] in (0x07, 0x08):
                        # to jest immediate offset 0x0700..0x08FF
                        off_val = code[i] | (code[i+1] << 8)
                        if 0x700 <= off_val <= 0x900:
                            print(f"    slot=0x{slot:X} rva=0x{rva:X} imm_off=0x{off_val:X}")
                            break
            except: pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
