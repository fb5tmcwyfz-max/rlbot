"""
run_sdk - uruchamia bota przez prism_sdk (pamiec gry + kernel driver).

Nie ma tu RLBota. Stan gry jest czytany bezposrednio z procesu Rocket League
przez sterownik PrismKernel, a input wraca do gry kanalem SDK. Powtorzone
zostalo natomiast TAKTOWANIE, ktore mial RLBotowy `bot.py` - okno osmiu tikow
z akcja wchodzaca na siodmym oraz skryptowany kickoff. Szczegoly w
`bots/nexto_sdk.py` i `prism_sdk/adapters/rlbot_timing.py`.

KOLEJNOSC URUCHAMIANIA
----------------------
    1. Zaladuj sterownik PrismKernel (jako Administrator).
    2. Odpal Rocket League i wejdz do meczu / freeplay.
    3. python run_sdk.py --run

Sprawdzenie bez gry i bez sterownika:

    python run_sdk.py --check        # dump + import sieci + konstrukcja bota
    python run_sdk.py --list-bots

DLACZEGO DOMYSLNIE `--send pc`
------------------------------
Zaladowany sterownik wystawia tylko READ i WRITE. Kanaly `patch` i `kernel`
wymagaja IOCTL-i ALLOC_RWX / PATCH_CODE / AUTOWRITE, ktorych w tym buildzie
sterownika nie ma - trzeba by go przebudowac. `pc` pisze do
PlayerController_TA (OverrideInput) zwyklym WRITE, wiec dziala od reki.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: Bot uruchamiany bez `--bot`. Rejestracja siedzi w `bots/`.
DEFAULT_BOT = "nexto"

#: Tempo WYSYLKI inputu. 120 Hz = tempo fizyki gry i jeden zapis na klatke.
#: Wyzej nie ma sensu: zmierzony `world.refresh()` trwa ~7 ms (p95 9.2 ms),
#: czyli sufit to i tak ~140 Hz, a gonienie 240 Hz zabiera budzet inferencji.
DEFAULT_HZ = 120.0


# ======================================================================
# Dump SDK -> stale w prism_sdk
# ======================================================================

def apply_dump(dump_path: str = "", apply_setvi: bool = False, quiet: bool = False) -> bool:
    """Wstrzyknij wartosci z `rl_dump_full.txt` do `prism_sdk`.

    MUSI byc wolane przed `World().attach()` - `Reflection` czyta swoje
    GObjects/GNames dopiero w `rebuild()`, ale sam `attach()` juz go odpala.
    """
    from prism_dump import RLDump, DumpError

    try:
        dump = RLDump.load(dump_path or None)
    except DumpError as e:
        print(f"[!] dump SDK: {e}")
        print("    Bez dumpa SDK jedzie na stalych wpisanych na sztywno - "
              "po update gry beda nieaktualne.")
        return False
    report = dump.apply_to_sdk(apply_setvi=apply_setvi)
    if not quiet:
        report.print()
    return True


# ======================================================================
# Sender
# ======================================================================

def build_sender(world, kind: str, pc_mode: str, no_pc: bool):
    """Kanal, ktorym input trafia do gry. None = tylko log akcji."""
    if kind == "print":
        print("[i] sender 'print' - akcje leca na konsole, gra ich nie dostanie")
        return None

    if kind == "pc":
        from prism_sdk.control import PCSender
        print(f"[+] PCSender (mode={pc_mode}) - PlayerController_TA.OverrideInput")
        return PCSender(world, mode=pc_mode, verbose=True)

    if kind == "kernel":
        from prism_sdk.control import KernelSender
        print("[+] KernelSender - kernel-side auto-writer do FVehicleInputs")
        return KernelSender(world, verbose=True, pc_channel=not no_pc, pc_mode=pc_mode)

    if kind == "patch":
        # Patch celuje dzis w RVA dispatchera skryptow, nie w SetVehicleInput -
        # `prism_dump.py` wypisuje to jako UWAGA przy kazdym starcie. Do czasu
        # policzenia nowego STOLEN_SIZE ten kanal jest niepewny.
        from prism_sdk.control import PatchSender
        print("[!] PatchSender - patchuje kod gry. Przeczytaj UWAGE o SETVI_RVA wyzej.")
        return PatchSender(world, verbose=True, pc_channel=not no_pc, pc_mode=pc_mode)

    raise ValueError(f"nieznany kanal '{kind}'")


# ======================================================================
# Wykrycie naszego auta
# ======================================================================

def shutdown(sender, world) -> None:
    """Zamknij kanal i odepnij sie - w TEJ kolejnosci.

    Musi byc wolane na KAZDEJ sciezce wyjscia, nie tylko po petli
    Managera. Kanal `patch` zostawia w kodzie gry 14-bajtowy detour;
    wyjscie bez `close()` (np. timeout czekania na auto) zostawialo go
    zainstalowanego, wskazujacego na alokacje po nieistniejacym juz
    procesie. Kolejna instalacja to wprawdzie rozpoznaje i cofa, ale
    do tego czasu w .text gry siedzi skok w prozne miejsce.
    """
    if sender is not None and hasattr(sender, "close"):
        try:
            sender.close()
        except Exception as e:
            print(f"[!] sender.close(): {type(e).__name__}: {e}")
    try:
        world.detach()
    except Exception:
        pass


def wait_for_local_car(world, timeout: float = 60.0) -> int:
    """Indeks lokalnego gracza w `world.players` albo -1.

    Auto rozpoznajemy po TOZSAMOSCI (LocalPlayers -> PlayerController -> Pawn),
    nie po pozycji na liscie: `world.cars` bywa niekompletne w trakcie
    kickoffu, resetu po golu i powtorki. Dzieki temu bota mozna wlaczyc w
    dowolnym momencie meczu.
    """
    from prism_sdk.adapters._rlgym_shim import resolve_self_player_index

    deadline = time.time() + timeout
    announced = False
    while time.time() < deadline:
        world.refresh()
        pi = resolve_self_player_index(world)
        if pi is not None:
            return pi
        if not announced:
            print("[*] Czekam, az Twoje auto pojawi sie na boisku "
                  "(menu/spectator/respawn) - Ctrl+C przerywa...")
            announced = True
        time.sleep(0.25)
    return -1


# ======================================================================
# Tryby bez gry
# ======================================================================

def cmd_list_bots() -> int:
    from prism_sdk.agent import registry
    registry.discover()
    print("Boty zarejestrowane w bots/:")
    print(registry.describe())
    print(f"\nDomyslny: {DEFAULT_BOT}")
    return 0


def cmd_check(bot_name: str, dump_path: str) -> int:
    """Przebieg bez gry i bez sterownika.

    Sprawdza to, co najczesciej sypie sie dopiero w meczu: brak wag, brak
    pakietu (torch ciagnie sporo zaleznosci), zla nazwa bota, zepsuty dump.
    """
    ok = apply_dump(dump_path)
    print()

    from prism_sdk.agent import registry
    registry.discover()
    try:
        agent = registry.create(bot_name)
    except Exception as e:
        print(f"[!] nie moge zbudowac bota '{bot_name}': {type(e).__name__}: {e}")
        return 1

    timing = "tak" if hasattr(agent, "TICK_SKIP") else "NIE"
    print(f"[+] bot '{bot_name}' -> {agent.name}")
    print(f"    taktowanie jak w RLBocie: {timing}")
    print(f"    decision_hz: {getattr(agent, 'decision_hz', None)} "
          f"(None = decyzja co tick, okno tikow liczy mixin)")
    if hasattr(agent, "hardcoded_kickoffs"):
        print(f"    skryptowany kickoff: {agent.hardcoded_kickoffs}")
    return 0 if ok else 1


# ======================================================================
# main
# ======================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="prism_sdk runner (bez RLBota)")
    p.add_argument("--run", action="store_true",
                   help="uruchom petle sterowania (bez tego: tylko opis stanu gry)")
    p.add_argument("--bot", default=DEFAULT_BOT, help=f"nazwa bota (domyslnie: {DEFAULT_BOT})")
    p.add_argument("--hz", type=float, default=DEFAULT_HZ, help="tempo wysylki inputu")
    p.add_argument("--send", default="pc", choices=("pc", "kernel", "patch", "print"),
                   help="kanal sterowania (domyslnie pc - jedyny dzialajacy "
                        "na sterowniku z samym READ/WRITE)")
    p.add_argument("--pc-mode", default="override", choices=("override", "direct", "both"))
    p.add_argument("--no-pc", action="store_true",
                   help="dla kernel/patch: nie pisz rownolegle do PlayerControllera")
    p.add_argument("--no-gate", action="store_true",
                   help="nie pauzuj w menu/replayu/odliczaniu (do debugowania petli)")
    p.add_argument("--car-index", type=int, default=-1,
                   help="wymus indeks auta zamiast auto-detekcji")
    p.add_argument("--dump", default="", help="sciezka do rl_dump_full.txt")
    p.add_argument("--apply-setvi", action="store_true",
                   help="podmien takze SETVI_RVA wartoscia z dumpa (patrz ostrzezenie)")
    p.add_argument("--list-bots", action="store_true")
    p.add_argument("--check", action="store_true",
                   help="sprawdz dump, wagi i konstrukcje bota - bez gry i sterownika")
    p.add_argument("--release", action="store_true",
                   help="wyzeruj input i oddaj sterowanie (po ubitym procesie bota)")
    args = p.parse_args(argv)

    if args.list_bots:
        return cmd_list_bots()
    if args.check:
        return cmd_check(args.bot, args.dump)

    # Stale z dumpa MUSZA byc na miejscu przed attachem - patrz apply_dump().
    apply_dump(args.dump, apply_setvi=args.apply_setvi)
    print()

    from prism_sdk.world import World

    print("[*] Podpinam sie do Rocket League...")
    world = World()
    if not world.attach():
        print("[!] Attach nieudany. Sterownik PrismKernel zaladowany? RL uruchomiony?")
        return 1
    if not world.refresh():
        print("[!] refresh() nieudany - jestes w menu? Wejdz do meczu albo freeplay.")
        world.detach()
        return 1
    print("[+] Podpiete.")

    if args.release:
        from prism_sdk.control.player_controller import PlayerControllerChannel
        done = PlayerControllerChannel(world, mode="both").neutralize()
        print("[+] input wyzerowany" if done else "[i] brak lokalnego auta - nie ma czego zerowac")
        world.detach()
        return 0

    print(world.describe())

    if not args.run:
        print("\n[i] Dodaj --run, zeby wystartowac sterowanie.")
        world.detach()
        return 0

    from prism_sdk.agent import Manager, registry
    registry.discover()
    try:
        agent = registry.create(args.bot)
    except Exception as e:
        print(f"[!] {type(e).__name__}: {e}")
        world.detach()
        return 1

    sender = None
    try:
        sender = build_sender(world, args.send, args.pc_mode, args.no_pc)
    except Exception as e:
        print(f"[!] kanal '{args.send}' nie wstal: {type(e).__name__}: {e}")
        world.detach()
        return 1

    car_index = args.car_index
    if car_index < 0:
        car_index = wait_for_local_car(world)
        if car_index < 0:
            print("[!] Nie wykrylem Twojego auta. Jestes NA BOISKU (nie spectator)?")
            shutdown(sender, world)
            return 1
        me = world.players[car_index]
        print(f"[+] lokalne auto: player[{car_index}] '{getattr(me, 'name', '?')}' "
              f"team={me.team_num} (car @ 0x{me.car_address:X})")

        # ===== CONTROLLER PAUSE / RESUME (PS5 Share button) =====
    try:
        import pygame
        pygame.init()
        pygame.joystick.init()
    except ImportError:
        print("[!] Missing library 'pygame'. Install it with: pip install pygame")
        pygame = None

    paused = False
    joystick = None
    prev_button = False

    if pygame is not None and pygame.joystick.get_count() > 0:
        joystick = pygame.joystick.Joystick(0)
        joystick.init()
        print(f"[+] Controller detected: {joystick.get_name()}")
        print("[+] Share / Create button = pause / resume bot")
    else:
        print("[!] No controller found – pause with controller disabled")

    def is_paused() -> bool:
        nonlocal paused, prev_button

        if joystick is None:
            return paused

        pygame.event.pump()                     # keep controller state updated

        # Button 4 = Share / Create on PS5 DualSense / Edge
        button_now = joystick.get_button(4)

        # Rising edge detection (only trigger once when pressed)
        if button_now and not prev_button:
            paused = not paused
            if paused:
                print("\n[PAUSE] Bot paused – you have full control")
            else:
                print("\n[RESUME] Bot resumed")

        prev_button = button_now
        return paused
    # ========================================================

    mgr = Manager(
        world,
        sender=sender,
        gate_on_play_state=not args.no_gate,
        pause_source=is_paused
    )
    mgr.bind(agent, car_index=car_index)

    print(f"\n[*] {agent.name} @ {args.hz:g} Hz, kanal '{args.send}', car[{car_index}]")
    print("    Ctrl+C = stop completely | Share button = pause/resume")
    try:
        mgr.run(hz=args.hz)
    finally:
        if pygame is not None:
            pygame.quit()
        shutdown(sender, world)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
