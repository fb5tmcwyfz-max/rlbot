"""Disasm dowolnego RVA - do inspekcji funkcji z dumpa."""
import sys
sys.path.insert(0, r"C:\Users\Wnuczek\Desktop\Nexto")
from prism_sdk.low.driver import Driver

def main():
    if len(sys.argv) < 2:
        print("uzycie: python rl_disasm_rva.py <rva_hex> [bytes=200] [count=50]")
        return 1
    rva = int(sys.argv[1], 16)
    n_bytes = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    n_count = int(sys.argv[3]) if len(sys.argv) > 3 else 50

    drv = Driver()
    if not drv.attach(): return 1
    addr = drv.module_base + rva
    data = drv.read_bytes(addr, n_bytes)
    drv.detach()
    if not data:
        print("[!] read failed"); return 1

    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    print(f"[+] RVA=0x{rva:X}  Base=0x{drv.module_base:X}  Addr=0x{addr:X}")
    print()
    for i, ins in enumerate(md.disasm(data, addr)):
        if i >= n_count: break
        raw = " ".join(f"{b:02X}" for b in ins.bytes)
        print(f"  +0x{ins.address - addr:03X}  {raw:<26} {ins.mnemonic:<8} {ins.op_str}")

if __name__ == "__main__":
    main()
