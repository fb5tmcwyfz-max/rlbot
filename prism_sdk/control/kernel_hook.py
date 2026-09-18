"""
Kernel-patch SetVehicleInput hook.

Zamiast pisac do FVehicleInputs w petli (race z game logic) - patchujemy
prologue SetVehicleInput na JMP do naszego shellcode ktory podmienia
zawartosc newInputPtr (RDX) na nasze wartosci PRZED tym jak original
SetVehicleInput je skopiuje do Vehicle_TA.INPUT.

Efekt: gra sama pisze nasze wartosci na kazdej klatce fizyki. Zero race,
zero polling. CPU: dokladnie 0% (poza jednym IOCTL_WRITE na Manager tick).

Layout allocation w RL (256B, RWX):
    0x000..0x05C  detour_stub  (93B x64 shellcode)
    0x080..0x0A0  trampoline   (12B stolen + 14B abs jmp back = 26B)
    0x100..0x140  shared_buffer (32B FVehicleInputs + 2B replicated + 1B enabled)

Update: Python pisze do (alloc_base + 0x100) przez kwrite/write, 60Hz.
"""

from __future__ import annotations

import struct
from typing import Optional

from prism_sdk.control.controller_state import ControllerState
from prism_sdk.control.player_controller import PlayerControllerChannel, MODE_OVERRIDE
from prism_sdk.control.vehicle_inputs   import f_to_u8 as _f_to_u8, pack_input as _pack_input
from prism_sdk.low import mem
from prism_sdk.world.local import resolve_local_chain


# Offsety w allocation
SHELLCODE_OFFSET  = 0x00
TRAMPOLINE_OFFSET = 0x80
SHARED_OFFSET     = 0x100

# Layout shared buffer (od SHARED_OFFSET)
SHARED_INPUT_OFF   = 0x00   # 32B FVehicleInputs
SHARED_REP_OFF     = 0x20   # 2B (throttle u8 + steer u8)
SHARED_ENABLED_OFF = 0x22   # 1B
# 0x23..0x27 padding
SHARED_CAR_ADDR_OFF = 0x28  # 8B - adres NASZEGO Vehicle_TA (filter przeciw
                             # nadpisywaniu inputu innych graczy w multiplayer)

# SetVehicleInput - ZMIERZONE 2026-08-22 deassemblacja zywego procesu.
#
# Poprzednia wartosc 0x36E050 byla BLEDNA: to RVA wspolnego interpretera
# bytecode'u UnrealScript. Wniosek "3 z 4 instancji UFunction wskazuja ten
# adres" opieral sie na instancjach SKRYPTOWYCH, ktorych `Func` z definicji
# wskazuje dispatcher. Patch w tym miejscu lapal KAZDE wywolanie skryptowe
# w grze zamiast inputu auta.
#
# Wpis z dumpa (0xE3B090) tez nie jest implementacja - to thunk skryptowy,
# ktory konczy sie na `call qword ptr [rax+0x830]`, czyli wywolaniu
# WIRTUALNYM. Wlasciwa funkcja siedzi w vtable pod +0x830:
#
#   +0x000  48 83 EC 58             sub rsp, 0x58           4 B
#   +0x004  80 B9 CB 00 00 00 02    cmp byte [rcx+0xcb], 2  7 B  -> 11
#   +0x00B  4C 8B C1                mov r8, rcx             3 B  -> 14
#   +0x00E  0F 82 77 04 00 00       jb  ...                 (relatywny)
#   +0x014  0F 10 02                movups xmm0, [rdx]      <- czyta NASZ input
#   +0x023  0F 11 81 FC 07 00 00    movups [rcx+0x7fc], xmm0
#
# Konwencja rejestrow potwierdzona: RCX = this (Car_TA), RDX = FVehicleInputs*.
# Ponowna weryfikacja po update gry:
#     python disasm_vfunc.py --slot 0x830
SETVI_RVA = 0xEDEB90

# Granica instrukcji wypada dokladnie na 14 B - tyle samo, ile zajmuje
# absolutny `jmp [rip+0]` + qword. Zadna z trzech kradzionych instrukcji
# nie jest RIP-relative ani skokiem, wiec mozna je odtworzyc bit w bit w
# trampolinie. Skok `jb` z +0x0E jest relatywny, ale ZOSTAJE na miejscu.
STOLEN_SIZE = 14

ALLOC_SIZE = 256


def _build_detour_stub(shared_addr: int, trampoline_addr: int) -> bytes:
    """Ekwiwalent w assembly:
      push rax; push r10; push r11
      mov r10, shared_addr
      mov al, [r10+0x22]           ; enabled byte
      test al, al
      jz done_write                 ; skip if not enabled

      ; NEW: filter po `this` pointer - overrideujemy TYLKO nasze auto
      ;      (inaczej w private match/online popsulibysmy input przeciwnikow)
      mov rax, [r10+0x28]           ; nasz car_addr (0 = 'wszystkie', filter off)
      test rax, rax
      jz skip_car_check             ; jesli 0, nie filtruj
      cmp rax, rcx
      jne done_write                 ; nie nasze auto -> skip
    skip_car_check:

      ; copy 32B [r10] -> [rdx]
      mov rax, [r10+0]; mov [rdx+0], rax
      mov rax, [r10+8]; mov [rdx+8], rax
      mov rax, [r10+16]; mov [rdx+16], rax
      mov rax, [r10+24]; mov [rdx+24], rax
      ; replicated -> [rcx+0x804/5]
      mov al, [r10+0x20]; mov [rcx+0x804], al
      mov al, [r10+0x21]; mov [rcx+0x805], al
    done_write:
      pop r11; pop r10; pop rax
      mov r11, trampoline_addr
      jmp r11
    """
    sc = bytearray()
    sc += bytes([0x50])                        # push rax
    sc += bytes([0x41, 0x52])                  # push r10
    sc += bytes([0x41, 0x53])                  # push r11

    sc += bytes([0x49, 0xBA]) + struct.pack("<Q", shared_addr)   # mov r10, imm64

    sc += bytes([0x41, 0x8A, 0x42, 0x22])      # mov al, [r10+0x22]  (enabled)
    sc += bytes([0x84, 0xC0])                  # test al, al

    jz_enabled_pos = len(sc)
    sc += bytes([0x74, 0x00])                  # jz done_write (skip all)

    # Load our_car_addr, jesli 0 -> pomin cmp (filter off, override wszystkie)
    sc += bytes([0x49, 0x8B, 0x42, 0x28])      # mov rax, [r10+0x28]
    sc += bytes([0x48, 0x85, 0xC0])            # test rax, rax
    jz_carcheck_pos = len(sc)
    sc += bytes([0x74, 0x00])                  # jz skip_car_check (rax==0, filter off)
    # rax != 0, sprawdz czy == rcx (thisPtr)
    sc += bytes([0x48, 0x39, 0xC8])            # cmp rax, rcx
    jne_pos = len(sc)
    sc += bytes([0x75, 0x00])                  # jne done_write (nie nasze auto)

    # skip_car_check: label
    skip_check_label = len(sc)
    sc[jz_carcheck_pos + 1] = skip_check_label - (jz_carcheck_pos + 2)

    # === Copy input ===
    for off in (0x00, 0x08, 0x10, 0x18):
        sc += bytes([0x49, 0x8B, 0x42, off])   # mov rax, [r10+off]
        sc += bytes([0x48, 0x89, 0x42, off])   # mov [rdx+off], rax
    # ReplicatedThrottle/Steer: USUNIETE.
    #
    # Shellcode pisal dwa bajty pod [rcx+0x804] i [rcx+0x805]. Po update
    # gry pod 0x804 siedzi FLOAT PITCH ze struktury INPUT (0x7FC + 0x08) -
    # zmierzone deassemblacja, patrz komentarz przy SETVI_RVA. Wpisywanie
    # tam bajtu 0..255 rozjezdzalo pitcha auta w powietrzu, czyli psulo
    # dokladnie te mechaniki, dla ktorych ten kanal powstal.
    #
    # Prawdziwe offsety tych pol sa NIEZNANE (prawdopodobnie 0x81C/0x81D,
    # ale nikt tego nie zmierzyl). Kanal replikacji do serwera i tak
    # obsluguje PlayerControllerChannel, wiec nic tu nie tracimy.

    # done_write: label
    done_label = len(sc)
    # Patch JZ (enabled=0)
    offset_jz_enabled = done_label - (jz_enabled_pos + 2)
    assert 0 <= offset_jz_enabled < 128, f"jz enabled skip too far: {offset_jz_enabled}"
    sc[jz_enabled_pos + 1] = offset_jz_enabled
    # Patch JNE (car mismatch)
    offset_jne = done_label - (jne_pos + 2)
    assert 0 <= offset_jne < 128, f"jne skip too far: {offset_jne}"
    sc[jne_pos + 1] = offset_jne

    # done_write body:
    sc += bytes([0x41, 0x5B])                  # pop r11
    sc += bytes([0x41, 0x5A])                  # pop r10
    sc += bytes([0x58])                        # pop rax

    sc += bytes([0x49, 0xBB]) + struct.pack("<Q", trampoline_addr)  # mov r11, imm64
    sc += bytes([0x41, 0xFF, 0xE3])            # jmp r11

    return bytes(sc)


def _build_trampoline(stolen_bytes: bytes, resume_addr: int) -> bytes:
    """Odtwarza skradziony prolog, potem skacze absolutnie za patch.

    stolen_bytes[0..13] = sub rsp,0x58 ; cmp byte [rcx+0xcb],2 ; mov r8,rcx
    Zadna z nich nie jest RIP-relative ani skokiem, wiec odtworzenie bit
    w bit jest poprawne.
    """
    assert len(stolen_bytes) == STOLEN_SIZE, f"expected {STOLEN_SIZE} stolen bytes, got {len(stolen_bytes)}"
    tr = bytearray()
    tr += stolen_bytes                              # 12B
    tr += bytes([0xFF, 0x25, 0x00, 0x00, 0x00, 0x00])  # jmp qword ptr [rip+0]
    tr += struct.pack("<Q", resume_addr)            # 8B abs addr
    return bytes(tr)


def _build_patch(detour_addr: int) -> bytes:
    """14B patch na poczatek SetVehicleInput:

        FF 25 00 00 00 00   jmp qword ptr [rip+0]
        <qword>             detour_addr

    Skok absolutny przez pamiec, wiec NIE dotykamy zadnego rejestru -
    inaczej niz poprzedni wariant `mov rax, imm64; jmp rax`, ktory
    niszczyl RAX. Tamten kompromis byl wymuszony 12-bajtowa granica
    instrukcji w prologu dispatchera; prawdziwy SetVehicleInput ma
    czysta granice dokladnie na 14 B, wiec nie jest juz potrzebny.
    """
    patch = bytes([0xFF, 0x25, 0x00, 0x00, 0x00, 0x00]) + \
        struct.pack("<Q", detour_addr)
    assert len(patch) == STOLEN_SIZE, (
        f"patch ma {len(patch)} B, a kradniemy {STOLEN_SIZE} B - "
        f"trampolina wrocilaby w srodek instrukcji")
    return patch


class KernelPatchHook:
    """Instaluje kernel patch SetVehicleInput. Update inputu przez update()."""

    def __init__(self, world, verbose: bool = False):
        self.world = world
        self.verbose = verbose
        self._installed = False
        self._alloc_base = 0
        self._stolen_bytes = b""
        self._patch_target = 0

    def install(self) -> None:
        drv = self.world.driver
        pid = drv.pid
        setvi_va = drv.module_base + SETVI_RVA
        self._patch_target = setvi_va

        # Read current bytes SetVehicleInput
        current = drv.read_bytes(setvi_va, STOLEN_SIZE)
        # Prologue po update 2026-08-11:
        #   40 53              push rbx
        #   55                 push rbp
        #   56                 push rsi
        #   57                 push rdi
        #   48 81 EC C8 00 00 00   sub rsp, 0xC8
        # (12B do czystej granicy; bajt 13+ to `48 8B XX` - mov, nie stealujemy)
        # Prolog PRAWDZIWEGO SetVehicleInput (vtable+0x830 -> RVA 0xEDEB90):
        #   48 83 EC 58              sub rsp, 0x58
        #   80 B9 CB 00 00 00 02     cmp byte ptr [rcx+0xcb], 2
        #   4C 8B C1                 mov r8, rcx
        # Poprzednia wartosc (405355565748 81 EC C8 00 00 00) byla prologiem
        # DISPATCHERA skryptow - patrz komentarz przy SETVI_RVA.
        # Zweryfikuj po update gry:  python verify_hook.py
        expected = bytes.fromhex("4883EC5880B9CB000000024C8BC1")

        if current != expected:
            # Sprawdz czy to nasz stary patch (mov rax+jmp rax pattern) - user zabil
            # Pythona przez Task Manager wiec close() sie nie odpalilo. Auto-recover.
            # Nasz patch to teraz 14B `jmp qword ptr [rip+0]` + adres.
            # Stary wariant (`mov rax, imm64; jmp rax`) tez rozpoznajemy -
            # ktos moze miec go jeszcze zalozonego z poprzedniej wersji.
            is_our_patch = (
                current[:2] == bytes([0xFF, 0x25])
                or (current[:2] == bytes([0x48, 0xB8])
                    and current[10:12] == bytes([0xFF, 0xE0])))
            if is_our_patch:
                if self.verbose:
                    old_target = int.from_bytes(current[6:14], "little")
                    # (dla wariantu FF 25 adres tez siedzi na offsecie 6)
                    print(f"[KernelPatchHook] wykryto STARY patch (JMP -> 0x{old_target:X})")
                    print(f"[KernelPatchHook] auto-recovery: przywracam oryginalne bajty...")
                try:
                    mem.patch_code(pid, setvi_va, expected)
                    if self.verbose:
                        print(f"[KernelPatchHook] recovery OK, kontynuuje instalacje")
                    current = expected
                except Exception as e:
                    raise RuntimeError(f"Auto-recovery patch_code failed: {e}")
            else:
                raise RuntimeError(
                    f"SetVehicleInput bytes zmienione (RL update?):\n"
                    f"  expected: {expected.hex()}\n"
                    f"  got:      {current.hex()}\n"
                    f"Nie moge patchowac bezpiecznie. Restart RL i sprobuj ponownie."
                )

        # Alloc RWX w RL
        self._alloc_base = mem.alloc_rwx(pid, ALLOC_SIZE)
        detour_addr     = self._alloc_base + SHELLCODE_OFFSET
        trampoline_addr = self._alloc_base + TRAMPOLINE_OFFSET
        shared_addr     = self._alloc_base + SHARED_OFFSET
        resume_addr     = setvi_va + STOLEN_SIZE

        if self.verbose:
            print(f"[KernelPatchHook] alloc @ 0x{self._alloc_base:X}")
            print(f"  detour:     0x{detour_addr:X}")
            print(f"  trampoline: 0x{trampoline_addr:X}")
            print(f"  shared:     0x{shared_addr:X}")
            print(f"  SetVI:      0x{setvi_va:X}")
            print(f"  resume:     0x{resume_addr:X}")

        # Compose shellcode + trampoline
        shellcode  = _build_detour_stub(shared_addr, trampoline_addr)
        trampoline = _build_trampoline(current, resume_addr)

        # Init shared buffer: 32B input + 2B rep + 1B enabled + 5B pad + 8B car_addr = 48B
        shared_init = bytearray(64)
        shared_init[0x00:0x20] = _pack_input(ControllerState.neutral())
        shared_init[0x20:0x22] = bytes([0x80, 0x80])
        shared_init[0x22] = 0     # enabled=0
        # car_addr = 0 -> shellcode traktuje jako "filter off, wszystkie auta"
        # Manager potem update'uje na konkretny car_addr

        drv.write_bytes(detour_addr,     shellcode)
        drv.write_bytes(trampoline_addr, trampoline)
        drv.write_bytes(shared_addr,     bytes(shared_init))

        # Patch SetVehicleInput
        patch = _build_patch(detour_addr)
        self._stolen_bytes = mem.patch_code(pid, setvi_va, patch)
        if self._stolen_bytes != current:
            raise RuntimeError(
                f"Stolen bytes z patch_code nie zgadzaja sie z read: "
                f"read={current.hex()} vs patched_from={self._stolen_bytes.hex()}"
            )

        self._shared_addr = shared_addr
        self._installed = True
        if self.verbose:
            print(f"[KernelPatchHook] installed OK (enabled=0, filter_car=0)")

    def update(self, cs: ControllerState, car_addr: int = 0) -> None:
        """Update shared buffer. car_addr=0 -> override wszystkich (freeplay),
        != 0 -> filter only ten car (multiplayer)."""
        if not self._installed: return
        drv = self.world.driver
        # 32B input + 2B rep + 1B enabled + 5B pad = 40B
        payload = bytearray(40)
        payload[0x00:0x20] = _pack_input(cs)
        payload[0x20]      = _f_to_u8(cs.throttle)
        payload[0x21]      = _f_to_u8(cs.steer)
        payload[0x22]      = 0x01
        drv.write_bytes(self._shared_addr, bytes(payload))
        # Osobny write dla car_addr (bo nie zmienia sie kazdy tick, tylko przy respawn)
        if car_addr != getattr(self, "_last_car_addr", None):
            drv.write_bytes(self._shared_addr + SHARED_CAR_ADDR_OFF,
                            car_addr.to_bytes(8, "little"))
            self._last_car_addr = car_addr

    def uninstall(self) -> None:
        """Przywroc oryginalne bajty SetVehicleInput. Alloc leak przyjety."""
        if not self._installed: return
        pid = self.world.driver.pid
        # Najpierw wylacz (zeby hook przestal pisac zanim usuniemy JMP)
        try:
            self.world.driver.write_bytes(self._shared_addr + SHARED_ENABLED_OFF, bytes([0]))
        except Exception:
            pass
        # Restore original prologue
        try:
            mem.patch_code(pid, self._patch_target, self._stolen_bytes)
        except Exception as e:
            print(f"[KernelPatchHook] uninstall patch_code failed: {e}")
        self._installed = False


class PatchSender:
    """Sender dla Managera - KernelPatchHook (fizyka) + PlayerController (replikacja).

    KRYTYCZNE dla trybow z respawnami (mecze): adres Vehicle_TA zmienia sie
    po kazdym respawnie (goal, reset). Zamiast polegac na world.cars[car_index]
    (ktora sie reorderuje/zmienia), CO TICK detektujemy fresh adres przez
    LocalPlayers chain (GameEvent + 0x360 -> PC -> Pawn). To dziala w kazdym
    trybie i po kazdym respawnie.

    Ten sam chain daje nam PlayerController, wiec przy okazji zasilamy DRUGI
    kanal (pc_channel=True): PC.OverrideInput + bOverrideInput. Patch zalatwia
    lokalna fizyke, PC zalatwia to co leci w pakiecie ruchu do serwera -
    bez tego w multiplayer serwer widzi neutral i koryguje auto (rubber band).
    Szczegoly: prism_sdk/control/player_controller.py
    """

    def __init__(self, world, verbose: bool = False,
                 pc_channel: bool = True, pc_mode: str = MODE_OVERRIDE):
        self.world = world
        self.verbose = verbose
        self.hook = KernelPatchHook(world, verbose=verbose)
        self.hook.install()
        self.pc = (PlayerControllerChannel(world, mode=pc_mode, verbose=verbose)
                   if pc_channel else None)
        self._last_pawn = 0
        self._last_warn = 0.0

    def __call__(self, car_index: int, cs: ControllerState) -> None:
        # Freshly detect pawn - car_index ignorowany bo indeksy niestabilne
        chain = resolve_local_chain(self.world)
        pawn = chain.pawn
        if pawn == 0:
            # Lancuch wskaznikow chwilowo nie wypalil. Zanim oddamy sterowanie,
            # sprobuj utrzymac OSTATNIE znane auto - ale tylko jesli nadal
            # istnieje na liscie aut gry.
            #
            # Wczesniej kazde takie potkniecie dawalo natychmiastowy
            # "input suspend": shellcode przestawal pisac, PC oddawal override,
            # auto na ulamek sekundy przechodzilo pod kontrole nikogo. Przy
            # kilku takich dziurach na sekunde sterowanie bylo szarpane.
            #
            # Warunek "nadal na liscie" jest tu istotny dla BEZPIECZENSTWA:
            # po respawnie stary obiekt auta jest zwalniany, a zapis pod
            # zwolniony adres przez sterownik kernela to prosta droga do BSOD.
            keep = 0
            if self._last_pawn:
                try:
                    if any(c.address == self._last_pawn for c in self.world.cars):
                        keep = self._last_pawn
                except Exception:
                    keep = 0
            if keep:
                self.hook.update(cs, car_addr=keep)
                if self.pc is not None:
                    self.pc.apply(cs, pc=chain.pc)
                return
            # Auta naprawde nie ma (respawn/menu/spectator) - nie ma do czego pisac.
            self.hook.update(cs, car_addr=0xFFFFFFFFFFFFFFFF)  # invalid = nie matchuje nikomu
            if self.pc is not None:
                self.pc.release()      # nie zostawiaj wiszacego override na PC
            self._last_pawn = 0
            return
        if pawn != self._last_pawn and self.verbose:
            print(f"[PatchSender] local pawn -> 0x{pawn:X}"
                  + (f" (change from 0x{self._last_pawn:X})" if self._last_pawn else ""))
            self._last_pawn = pawn
        self.hook.update(cs, car_addr=pawn)
        if self.pc is not None:
            self.pc.apply(cs, pc=chain.pc)

    def close(self) -> None:
        if self.pc is not None:
            try:
                self.pc.release()
            except Exception:
                pass

        # WYZERUJ WEJSCIE W SAMYM AUCIE - inaczej bot "rusza sam" po zamknieciu.
        #
        # Samo `hook.update(neutral, car_addr=0)` NIE wystarcza: car_addr=0 nie
        # pasuje do zadnego auta, wiec shellcode nigdy tych zer nie zapisze.
        # Po zdjeciu patcha w Vehicle_TA.INPUT zostawala ostatnia wartosc
        # (np. throttle=1), a gra nadpisuje ten obszar dopiero gdy gracz sam
        # ruszy sterowaniem. Efekt: po Ctrl+C auto ruszalo z gazem na kazdym
        # kickoffie po golu.
        #
        # Piszemy wiec zera WPROST do struktury auta, zanim zdejmiemy hooka.
        try:
            from prism_sdk.control.vehicle_inputs import pack_input
            from prism_sdk.low import offsets as _O
            neutral = pack_input(ControllerState.neutral())
            targets = set()
            try:
                chain = resolve_local_chain(self.world)
                if chain.pawn:
                    targets.add(chain.pawn)
            except Exception:
                pass
            if self._last_pawn:
                targets.add(self._last_pawn)
            for addr in targets:
                # Tylko auta, ktore nadal istnieja - zapis pod zwolniony adres
                # przez sterownik kernela to ryzyko BSOD.
                if any(c.address == addr for c in self.world.cars):
                    self.world.driver.write_bytes(addr + _O.Vehicle.INPUT, neutral)
        except Exception as e:
            print(f"[PatchSender] nie udalo sie wyzerowac inputu auta: {e}")

        try:
            self.hook.update(ControllerState.neutral(), car_addr=0)
        except Exception:
            pass
        self.hook.uninstall()
