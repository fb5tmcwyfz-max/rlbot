"""RL GObjects/GNames AOB scanner via PrismKernel.

Skanuje sekcje .text RocketLeague.exe przez driver PrismKernel szukajac
odwolan do TArray<UObject*> (GObjects) i TArray<FNameEntry*> (GNames).

Weryfikuje kandydatow probujac odczytac strukture TArray:
    +0x0: Data (pointer, must be 0x0000_0001_0000_0000 <= x < 0x0000_8000_0000_0000)
    +0x8: Count (int32, sensownie 1000..500000)
    +0xC: Max (int32, >= Count)

Zwraca top-3 kandydatow. Najczestsza (i pierwsza) jest zwykle GObjects.
GNames = GObjects - 0x48 (stala od UE3+).

Uzycie:
    1. Uruchom Rocket League (dowolny state - menu, freeplay, match)
    2. Zaladuj PrismKernel przez Prism.exe
    3. python rl_dump_globals.py
    4. Wyjscie: nowe RVA do wklejenia w prism_sdk/low/offsets.py

Uwaga: skan ~50 MB .text moze zajac 1-3 minuty (chunked read via IOCTL).
"""
from __future__ import annotations

import struct
import sys
import time
from collections import Counter

sys.path.insert(0, r"C:\Users\Wnuczek\Desktop\Nexto")
from prism_sdk.low.driver import Driver


# AOB patterns szukane w kodzie .text.
# Format: (bajty jako lista int, offset rel32 od poczatku patternu, opis)
# ? = wildcard (dowolny bajt) - reprezentowany jako -1
PATTERNS = [
    # mov rcx, [rip+rel32]  ;  test rcx, rcx  ;  jz/je ...
    ([0x48, 0x8B, 0x0D, -1, -1, -1, -1, 0x48, 0x85, 0xC9], 3, "mov rcx, [rel GObj]; test rcx,rcx"),
    # mov rax, [rip+rel32]  ;  test rax, rax  ;  jz ...
    ([0x48, 0x8B, 0x05, -1, -1, -1, -1, 0x48, 0x85, 0xC0], 3, "mov rax, [rel GObj]; test rax,rax"),
    # lea rcx, [rip+rel32]  ;  call ... (dostep do TArray by adres)
    ([0x48, 0x8D, 0x0D, -1, -1, -1, -1, 0xE8], 3, "lea rcx, [rel GObj]; call"),
    # mov r8, [rip+rel32]
    ([0x4C, 0x8B, 0x05, -1, -1, -1, -1], 3, "mov r8, [rel GObj]"),
]


def chunked_read(drv, addr, size, chunk=0x10000):
    """Czyta duzy blok w kawalkach 64KB. Zwraca bytes lub pusto jesli fail."""
    out = bytearray()
    off = 0
    while off < size:
        n = min(chunk, size - off)
        try:
            data = drv.read_bytes(addr + off, n)
            if not data:
                # Czasami strona jest niedostepna - pomin, kontynuuj
                out.extend(b"\x00" * n)
            else:
                out.extend(data)
                if len(data) < n:
                    out.extend(b"\x00" * (n - len(data)))
        except Exception:
            out.extend(b"\x00" * n)
        off += n
        # Progress bar
        if (off // chunk) % 100 == 0:
            pct = 100 * off / size
            sys.stdout.write(f"\r  reading .text: {pct:.1f}%")
            sys.stdout.flush()
    print()
    return bytes(out)


def scan_pattern(text_bytes, text_rva, pattern_bytes, rel_off, pattern_len):
    """Szuka wystapien patternu, zwraca listę RVA target-ow."""
    targets = []
    plen = pattern_len
    tlen = len(text_bytes) - plen
    # Preload prefix bytes for fast filter
    p0, p1, p2 = pattern_bytes[0], pattern_bytes[1], pattern_bytes[2]
    for i in range(tlen):
        if text_bytes[i] != p0: continue
        if text_bytes[i+1] != p1: continue
        if text_bytes[i+2] != p2: continue
        # Full match check (skip wildcards)
        ok = True
        for j in range(plen):
            pb = pattern_bytes[j]
            if pb == -1:
                continue
            if text_bytes[i+j] != pb:
                ok = False
                break
        if not ok: continue
        # Decode rel32
        rel = struct.unpack("<i", text_bytes[i+rel_off:i+rel_off+4])[0]
        # Target = instr_start + full_instruction_length + rel32
        # For "mov reg, [rip+rel32]" instr length = 7, rel_off = 3, so target from i+7
        instr_len = rel_off + 4
        target_rva = text_rva + i + instr_len + rel
        targets.append(target_rva)
    return targets


def parse_pe(drv, base):
    """Zwraca listę sekcji: [(name, rva_start, rva_end)]."""
    hdr = drv.read_bytes(base, 0x1000)
    if len(hdr) < 0x100:
        raise RuntimeError("Nie moge odczytac PE headera")
    if hdr[:2] != b"MZ":
        raise RuntimeError(f"Nie MZ signature: {hdr[:2]!r}")
    e_lfanew = struct.unpack_from("<i", hdr, 0x3C)[0]
    if hdr[e_lfanew:e_lfanew+4] != b"PE\x00\x00":
        raise RuntimeError("Nie PE signature")
    num_sections = struct.unpack_from("<H", hdr, e_lfanew + 6)[0]
    opt_hdr_size = struct.unpack_from("<H", hdr, e_lfanew + 20)[0]
    sect_start = e_lfanew + 24 + opt_hdr_size
    sections = []
    for i in range(num_sections):
        so = sect_start + i * 40
        name = hdr[so:so+8].rstrip(b"\x00").decode("ascii", "ignore")
        virt_size = struct.unpack_from("<I", hdr, so + 8)[0]
        virt_addr = struct.unpack_from("<I", hdr, so + 12)[0]
        sections.append((name, virt_addr, virt_addr + virt_size))
    return sections


def verify_tarray(drv, base, rva):
    """Sprawdz czy pod tym RVA jest sensowna TArray<X*> struktura.
    Zwraca (is_valid, data_ptr, count, max).

    Kryteria (poprawione — user-mode VA moze byc niski, np. 0x15AD24C0):
      - Data: 0x10000..0x7FFF_FFFF_FFFF (od 64KB nullpage do 128TB user limit)
      - Data 8-byte aligned (TArray Data zawsze jest, alokator)
      - Count: 100..5_000_000
      - Max: Count..5_000_000
    """
    try:
        buf = drv.read_bytes(base + rva, 16)
        if len(buf) < 16:
            return (False, 0, 0, 0)
        data_ptr, count, max_ = struct.unpack("<QII", buf)
        # Data must be in valid x64 user-mode range (above nullpage, below kernel)
        if data_ptr < 0x10000 or data_ptr > 0x0000_7FFF_FFFF_FFFF:
            return (False, data_ptr, count, max_)
        # Data must be 8-byte aligned (heap alloc)
        if data_ptr & 0x7:
            return (False, data_ptr, count, max_)
        # Count sanity
        if count < 100 or count > 5_000_000:
            return (False, data_ptr, count, max_)
        # Max sanity
        if max_ < count or max_ > 5_000_000:
            return (False, data_ptr, count, max_)
        return (True, data_ptr, count, max_)
    except Exception:
        return (False, 0, 0, 0)


def main():
    print("[*] Attaching to Rocket League...")
    drv = Driver()
    if not drv.attach():
        print("[!] Attach failed - PrismKernel loaded? RL running?")
        return 1

    base = drv.module_base
    print(f"[+] Base: 0x{base:X}")

    print("[*] Parsing PE headers...")
    sections = parse_pe(drv, base)
    text = next((s for s in sections if s[0] == ".text"), None)
    if not text:
        print("[!] .text section not found")
        return 1
    print(f"[+] .text: 0x{text[1]:X} - 0x{text[2]:X}  ({(text[2]-text[1])//1024//1024} MB)")

    # Rozsadnie ograniczmy skan .text (RL .text moze byc 50-80 MB, to duzo do
    # przeczytania przez IOCTL). Rzeczywisty .text to normalnie 20-40 MB.
    # UWAGA: mozesz zwiekszyc gdyby scan nie znalazl nic sensownego.
    scan_size = min(text[2] - text[1], 60 * 1024 * 1024)

    print(f"[*] Reading {scan_size // 1024 // 1024} MB of .text (chunked, ~1-3 min)...")
    t0 = time.time()
    text_bytes = chunked_read(drv, base + text[1], scan_size)
    print(f"[+] Read {len(text_bytes) // 1024 // 1024} MB in {time.time()-t0:.1f}s")

    print("[*] Scanning for AOB patterns...")
    all_hits = Counter()
    for pat, rel_off, desc in PATTERNS:
        t0 = time.time()
        hits = scan_pattern(text_bytes, text[1], pat, rel_off, len(pat))
        print(f"  [{desc}] {len(hits)} matches ({time.time()-t0:.1f}s)")
        for h in hits:
            all_hits[h] += 1

    # Top 20 najczestszych targetow
    print()
    print("[*] Top 20 candidate RVAs (sorted by reference count):")
    top = all_hits.most_common(20)
    for rva, cnt in top:
        # Filter to plausible .data range (RL globals are usually in .data)
        # Skip if outside module image
        # RL .data typically 0x2000000 - 0x2900000 range
        marker = ""
        if 0x2000000 <= rva <= 0x3000000:
            marker = "  (in typical .data range)"
        print(f"  0x{rva:08X}  refs={cnt}{marker}")

    print()
    print("[*] Verifying TArray structure at top candidates...")
    print()
    print(f"  {'RVA':<12} {'refs':<6} {'Data ptr':<20} {'Count':<8} {'Max':<8} valid?")
    print(f"  {'-'*12} {'-'*6} {'-'*20} {'-'*8} {'-'*8} ------")
    verified = []
    for rva, cnt in top[:20]:
        ok, data_ptr, count, max_ = verify_tarray(drv, base, rva)
        marker = "*** GObjects/GNames candidate ***" if ok else ""
        print(f"  0x{rva:08X}  {cnt:<6} 0x{data_ptr:016X}  {count:<8} {max_:<8} {marker}")
        if ok:
            verified.append((rva, cnt, data_ptr, count, max_))

    print()
    if not verified:
        print("[!] Zaden kandydat nie ma waznej struktury TArray.")
        print("    Mozliwe przyczyny:")
        print("     - RL uzywa innej struktury niz TArray<UObject*>")
        print("     - Skan pominal .text (za maly scan_size)")
        print("     - Wszystkie odwolania sa przez cache/loop counter")
        return 1

    print("[+] VALID CANDIDATES:")
    for rva, cnt, data_ptr, count, max_ in verified[:5]:
        print(f"  0x{rva:08X}: TArray with {count} entries (Data=0x{data_ptr:X})")

    # Best guess: GObjects has more entries than GNames (typically 10k+ vs a few k)
    # Or highest ref count
    print()
    verified.sort(key=lambda v: -v[1])  # by ref count desc
    if verified:
        gobj = verified[0]
        print(f"[BEST GUESS] GObjects RVA = 0x{gobj[0]:08X}  (Count={gobj[3]}, refs={gobj[1]})")
        # GNames should be at GObjects - 0x48 per OFFSETS.md
        gnames_guess = gobj[0] - 0x48
        gn_ok, gn_data, gn_count, gn_max = verify_tarray(drv, base, gnames_guess)
        if gn_ok:
            print(f"[BEST GUESS] GNames   RVA = 0x{gnames_guess:08X}  (Count={gn_count})  [verified via -0x48 rule]")
        else:
            print(f"[!] GObjects-0x48 (0x{gnames_guess:X}) does not verify as TArray - GNames offset may differ this build")

        print()
        print("=== NEW OFFSETS ===")
        print(f"GOBJECTS_RVA = 0x{gobj[0]:08X}")
        print(f"GNAMES_RVA   = 0x{gnames_guess:08X}")
        print()
        print("Update in: bots/rl/prism/prism_sdk/low/offsets.py")

    return 0


if __name__ == "__main__":
    sys.exit(main())
