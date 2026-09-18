"""
PrismKernel driver wrapper - jednolite Python API do READ/WRITE pamieci gry.

Wszystkie odczyty ida przez `pk_pymem` (Desktop\\nexto\\pk_pymem.py) ktory
rozmawia z naszym driverem przez IOCTL. To pozwala botom nie widziec zadnych
handle'i procesu / OpenProcess - EAC nie widzi zadnego user-mode dostepu.

Podstawowe operacje read_*() zwracaja gotowe Python typy. Do batch odczytow
mamy read_bytes() zeby zminimalizowac ilosc IOCTL calls (jedno IOCTL = kopia
przez MmCopyVirtualMemory, kosztowne).
"""

from __future__ import annotations

import os
import struct
import sys
from typing import Optional, Tuple

# pk_pymem siedzi w Desktop\nexto\; SDK importuje go z tej sciezki. Szukamy
# katalogu 'nexto' obok kazdego przodka tego pliku (projekt bywa przenoszony -
# kiedys Desktop\Bot\, teraz Desktop\VM\Bot\ - sztywne ../../../.. sie rozjezdza),
# a na koncu probujemy %USERPROFILE%\Desktop\nexto.
def _find_nexto_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    d = here
    while True:
        candidates.append(os.path.join(d, "nexto"))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    candidates.append(os.path.join(home, "Desktop", "nexto"))
    for c in candidates:
        if os.path.isfile(os.path.join(c, "pk_pymem.py")):
            return c
    return ""


_NEXTO_DIR = _find_nexto_dir()
if _NEXTO_DIR and _NEXTO_DIR not in sys.path:
    sys.path.insert(0, _NEXTO_DIR)

import pk_pymem  # noqa: E402

PROCESS_NAME = "RocketLeague.exe"

# ==================================================================
# Bariera adresowa - NIE USUWAC
# ==================================================================
#
# 26.07.2026: cztery BSODy pod rzad, dwa ostatnie identyczne:
#   0x50 PAGE_FAULT_IN_NONPAGED_AREA, adres 0xffffffff00000180 / 0xffffffff0000004e
# Czyli male liczby (0x180 = 384, 0x4e = 78) rozszerzone znakiem do wskaznika.
#
# Mechanizm: przy zmianie meczu gra zwalnia GameEvent / PlayerControllera, my
# idziemy po nieaktualnym lancuchu wskaznikow i czytamy smiec. Ten smiec leci
# potem jako ADRES do IOCTL_READ. Sterownik wola MmCopyVirtualMemory z
# PreviousMode = KernelMode, co wylacza walidacje w srodku -> dereferencja
# smiecia w kernelu -> BSOD zamiast bledu.
#
# Wlasciwa poprawka jest w sterowniku (PrismKernel.c, handlery PK_IOCTL_READ
# i PK_IOCTL_WRITE), ale wymaga przebudowy i restartu. Ta bariera dziala od
# razu i kosztuje dwa porownania, wiec zostaje na stale jako druga linia
# obrony - zaden smieciowy wskaznik z gry nie ma prawa dojsc do sterownika.
USER_VA_MIN = 0x10000              # ponizej = null page / male liczby = smiec
USER_VA_MAX = 0x00007FFFFFFF0000   # gorna granica user VA na Win x64


def is_plausible_address(addr: int) -> bool:
    """Czy `addr` moze byc prawdziwym wskaznikiem user-mode w grze?"""
    return USER_VA_MIN <= addr <= USER_VA_MAX


class Driver:
    """Cienki wrapper na pk_pymem.Pymem.

    Uzycie:
        drv = Driver()
        if drv.attach():
            base = drv.module_base
            b = drv.read_u32(base + 0x1000)
            drv.detach()

    Wszystkie odczyty zwracaja domyslne wartosci (0, 0.0, (0,0,0)) przy bledzie
    zamiast rzucac - petla bota nie wywali sie przy jednej niepowodzonej ramce.
    """

    def __init__(self, process_name: str = PROCESS_NAME):
        self.process_name = process_name
        self.pm: Optional[pk_pymem.Pymem] = None
        self.module_base: int = 0
        self.pid: int = 0
        #: Ile smieciowych adresow bariera zatrzymala (diagnostyka - niezerowe
        #: przy zmianie meczu jest normalne, stale rosnace oznacza, ze czytamy
        #: z nieaktualnego lancucha wskaznikow i trzeba to poprawic wyzej)
        self.blocked_reads: int = 0
        self.blocked_writes: int = 0

    # -- lifecycle --

    def attach(self) -> bool:
        try:
            self.pm = pk_pymem.Pymem(self.process_name)
            self.pid = self.pm.process_id
            # NIE lykaj wyjatku - jesli get_image_base sie posypie, module_base
            # bedzie 0, refl.rebuild() cicho zwroci False, a user zobaczy tylko
            # ogolne "Attach failed" bez zadnej diagnozy. Niech propaguje do
            # outer except - dostaniemy prawdziwy komunikat (errno z CreateFile,
            # kod IOCTL, itp.).
            self.module_base = self.pm.get_image_base()
            if self.module_base == 0:
                raise RuntimeError(
                    "get_image_base zwrocilo 0 dla PID %d - PsGetProcessSectionBaseAddress "
                    "nie zna tego procesu (RL zdazyl umrzec?)" % self.pid)
            return True
        except Exception as e:
            print(f"[driver] attach failed ({type(e).__name__}): {e}")
            if self.pm is not None:
                try: self.pm.close()
                except Exception: pass
            self.pm = None
            return False

    def detach(self):
        if self.pm is not None:
            try:
                self.pm.close()
            except Exception:
                pass
            self.pm = None

    @property
    def attached(self) -> bool:
        return self.pm is not None

    # -- raw read primitives --

    def read_bytes(self, addr: int, size: int) -> bytes:
        if self.pm is None or size <= 0 or addr == 0:
            return b""
        if not is_plausible_address(addr):
            # Smieciowy wskaznik (nieaktualny obiekt po zmianie meczu) - odrzuc
            # ZANIM dojdzie do sterownika. Patrz komentarz przy USER_VA_MIN.
            self.blocked_reads += 1
            return b""
        try:
            data = self.pm.read_bytes(addr, size)
            return data if len(data) == size else b""
        except Exception:
            return b""

    def read_u8(self, addr: int) -> int:
        d = self.read_bytes(addr, 1)
        return d[0] if d else 0

    def read_u16(self, addr: int) -> int:
        d = self.read_bytes(addr, 2)
        return struct.unpack("<H", d)[0] if d else 0

    def read_u32(self, addr: int) -> int:
        d = self.read_bytes(addr, 4)
        return struct.unpack("<I", d)[0] if d else 0

    def read_i32(self, addr: int) -> int:
        d = self.read_bytes(addr, 4)
        return struct.unpack("<i", d)[0] if d else 0

    def read_u64(self, addr: int) -> int:
        d = self.read_bytes(addr, 8)
        return struct.unpack("<Q", d)[0] if d else 0

    def read_i64(self, addr: int) -> int:
        d = self.read_bytes(addr, 8)
        return struct.unpack("<q", d)[0] if d else 0

    def read_ptr(self, addr: int) -> int:
        return self.read_u64(addr)

    def read_f32(self, addr: int) -> float:
        d = self.read_bytes(addr, 4)
        return struct.unpack("<f", d)[0] if d else 0.0

    def read_vec3(self, addr: int) -> Tuple[float, float, float]:
        """FVector = 3x float. Uzywane dla Location/Velocity/AngularVelocity."""
        d = self.read_bytes(addr, 12)
        return struct.unpack("<fff", d) if d else (0.0, 0.0, 0.0)

    def read_rotator_i32(self, addr: int) -> Tuple[int, int, int]:
        """FRotator = 3x int32 (unreal rotation units, 65536 = 360deg)."""
        d = self.read_bytes(addr, 12)
        return struct.unpack("<iii", d) if d else (0, 0, 0)

    def read_bit(self, addr: int, bit_mask: int) -> bool:
        return bool(self.read_u32(addr) & bit_mask)

    # -- string reads --

    def read_ascii_from_wide(self, addr: int, max_chars: int = 64) -> Optional[str]:
        """Wide-char (UTF-16) string ograniczony do ASCII printable. Uzywane do
        FNameEntry.Text - nazwy klas UE3 sa ASCII-only."""
        d = self.read_bytes(addr, max_chars * 2)
        if not d:
            return None
        out = []
        for i in range(0, len(d) - 1, 2):
            code = d[i] | (d[i + 1] << 8)
            if code == 0:
                break
            if code < 32 or code > 126:
                return None
            out.append(chr(code))
        return "".join(out) if out else None

    def read_tarray(self, addr: int) -> Tuple[int, int, int]:
        """TArray<T*> = { void* Data; int32 Count; int32 Max } = 16 bajtow.
        Zwraca (data_ptr, count, max)."""
        d = self.read_bytes(addr, 16)
        if not d:
            return 0, 0, 0
        return struct.unpack("<Qii", d)

    def read_ptr_array(self, addr: int, count: int) -> Tuple[int, ...]:
        """Odczyt tablicy `count` 8-bajtowych pointerow z ciaglego adresu."""
        if count <= 0:
            return ()
        d = self.read_bytes(addr, count * 8)
        if not d or len(d) != count * 8:
            return ()
        return struct.unpack(f"<{count}Q", d)

    # -- write primitives (do sterowania kiedy autowrite/hook nie dziala) --

    def write_bytes(self, addr: int, data: bytes) -> bool:
        if self.pm is None or not data or addr == 0:
            return False
        if not is_plausible_address(addr):
            self.blocked_writes += 1
            return False
        try:
            self.pm.write_bytes(addr, data, len(data))
            return True
        except Exception:
            return False
