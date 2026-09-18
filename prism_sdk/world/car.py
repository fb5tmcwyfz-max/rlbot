"""
Car - fizyka jednego auta w RL. Bez metadatych gracza (te w Player).

Physics + flagi. Odczyt z Vehicle_TA (baza dla Car_TA/Car_Freeplay_TA/etc).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from prism_sdk.low.driver import Driver
from prism_sdk.low        import offsets as O
from prism_sdk.world._common import Vec3, Quat, read_rbstate, quat_to_matrix


@dataclass
class CarPhysics:
    position:         Vec3 = field(default_factory=lambda: np.zeros(3))
    quaternion:       Quat = field(default_factory=lambda: np.array([0., 0., 0., 1.]))
    linear_velocity:  Vec3 = field(default_factory=lambda: np.zeros(3))
    angular_velocity: Vec3 = field(default_factory=lambda: np.zeros(3))

    @property
    def speed(self) -> float:
        return float(np.linalg.norm(self.linear_velocity))

    @property
    def rotation_matrix(self) -> np.ndarray:
        """3x3 macierz obrotu. Kolumny: forward, right, up."""
        return quat_to_matrix(self.quaternion)

    @property
    def forward(self) -> Vec3:
        return self.rotation_matrix[:, 0]

    @property
    def right(self) -> Vec3:
        return self.rotation_matrix[:, 1]

    @property
    def up(self) -> Vec3:
        return self.rotation_matrix[:, 2]


@dataclass
class Car:
    """Snapshot auta - fizyka + flagi + boost + jump/dodge components."""

    #: Adres UCar_TA (podklasa Vehicle_TA) w pamieci
    address: int = 0

    #: Fizyka - pozycja, orientacja, predkosci
    physics: CarPhysics = field(default_factory=CarPhysics)

    #: Boost 0.0-1.0
    boost:   float = 0.0

    #: Flagi z Vehicle_TA.bDriving/bJumped/bOnGround/...
    is_driving:      bool = False
    is_supersonic:   bool = False
    is_on_ground:    bool = True
    has_jumped:      bool = False
    has_double_jumped: bool = False

    #: True gdy auto moze wykonac flip. Bardzej precyzyjne niz not has_double_jumped:
    #: uwzglednia dodge component (dodge zuzywa flipa nawet bez double jump).
    has_flip: bool = True

    #: Czas kiedy ostatnio wykonano jump (game world time, sec).
    #: 0 gdy jeszcze nie skakano w tej rundzie.
    last_jump_time:  float = 0.0
    #: Czas kiedy ostatnio wykonano dodge (flip). 0 gdy nie bylo dodgea.
    last_dodge_time: float = 0.0
    #: Aktualnie w trakcie wykonywania dodgea? (dodge component active)
    is_dodging: bool = False
    #: Aktualnie w trakcie skoku (jump component active - trzyma jump)?
    is_jumping: bool = False

    #: Ile milisekund pozostalo na wykonanie double jump/flip po jump'ie.
    #: 0 gdy juz nie mozna albo nie skakano.
    flip_time_remaining: float = 0.0

    #: Aktualny licznik double jumpow (Car_TA.DoubleJumpsCtr)
    double_jumps_used: int = 0

    #: PID gracza sterujacego (adres PRI_TA lub 0 dla bota bez PRI)
    pri_addr:    int = 0
    #: Player controller ptr - !=0 znaczy human gra tym autem
    is_human_controlled: bool = False

    #: Adres ostatniego attackera (kto zdemolowal) - dla event detection
    attacker_pri_addr: int = 0

    @classmethod
    def read(cls, drv: Driver, car_addr: int, world_time: float = 0.0) -> "Car":
        if car_addr == 0:
            return cls()

        pos, quat, lv, av = read_rbstate(drv, car_addr)

        flags = drv.read_u32(car_addr + O.Vehicle.FLAGS_BYTE)
        driving        = bool(flags & O.Vehicle.FLAG_DRIVING)
        jumped         = bool(flags & O.Vehicle.FLAG_JUMPED)
        double_jumped  = bool(flags & O.Vehicle.FLAG_DOUBLE_JUMPED)
        on_ground      = bool(flags & O.Vehicle.FLAG_ON_GROUND)
        supersonic     = bool(flags & O.Vehicle.FLAG_SUPERSONIC)

        # Boost
        boost = 0.0
        boost_ptr = drv.read_ptr(car_addr + O.Vehicle.BOOST_COMPONENT)
        if boost_ptr != 0:
            raw = drv.read_f32(boost_ptr + O.BoostComponent.CURRENT_AMOUNT)
            boost = max(0.0, min(1.0, raw))

        # Jump component - kiedy ostatnio skakano
        last_jump_time = 0.0
        is_jumping = False
        jump_ptr = drv.read_ptr(car_addr + O.Vehicle.JUMP_COMPONENT)
        if jump_ptr != 0:
            last_jump_time = drv.read_f32(jump_ptr + O.JumpComponent.LAST_JUMP_TIME)
            jump_active = drv.read_u32(jump_ptr + O.JumpComponent.ACTIVE_FLAGS)
            is_jumping = bool(jump_active & O.JumpComponent.FLAG_ACTIVE)

        # Dodge component
        last_dodge_time = 0.0
        is_dodging = False
        dodge_ptr = drv.read_ptr(car_addr + O.Vehicle.DODGE_COMPONENT)
        if dodge_ptr != 0:
            last_dodge_time = drv.read_f32(dodge_ptr + O.DodgeComponent.LAST_DODGE_TIME)
            dodge_active = drv.read_u32(dodge_ptr + O.DodgeComponent.ACTIVE_FLAGS)
            is_dodging = bool(dodge_active & O.DodgeComponent.FLAG_ACTIVE)

        # Precyzyjny has_flip: nie mial dodgea ani double jump, i jest w ~1.5s od jumpa
        # (albo jeszcze nie skakal - is_on_ground)
        has_flip = True
        if on_ground:
            has_flip = True   # na ziemi zawsze ma flip
        elif double_jumped or is_dodging:
            has_flip = False
        elif last_jump_time > 0 and world_time > 0:
            since_jump = world_time - last_jump_time
            has_flip = 0 <= since_jump <= 1.5   # 1.5s window na double jump/dodge

        flip_time_remaining = 0.0
        if last_jump_time > 0 and world_time > 0 and not on_ground:
            since_jump = world_time - last_jump_time
            flip_time_remaining = max(0.0, 1.5 - since_jump)

        # Double jumps counter
        double_jumps_used = drv.read_i32(car_addr + O.Car.DOUBLE_JUMPS_CTR)

        pri_ptr = drv.read_ptr(car_addr + O.Vehicle.PRI)
        pc_ptr  = drv.read_ptr(car_addr + O.Vehicle.PLAYER_CONTROLLER)
        attacker_ptr = drv.read_ptr(car_addr + O.Car.ATTACKER_PRI)

        return cls(
            address=car_addr,
            physics=CarPhysics(
                position=pos, quaternion=quat,
                linear_velocity=lv, angular_velocity=av,
            ),
            boost=boost,
            is_driving=driving,
            is_supersonic=supersonic,
            is_on_ground=on_ground,
            has_jumped=jumped,
            has_double_jumped=double_jumped,
            has_flip=has_flip,
            last_jump_time=last_jump_time,
            last_dodge_time=last_dodge_time,
            is_dodging=is_dodging,
            is_jumping=is_jumping,
            flip_time_remaining=flip_time_remaining,
            double_jumps_used=double_jumps_used,
            pri_addr=pri_ptr,
            is_human_controlled=(pc_ptr != 0),
            attacker_pri_addr=attacker_ptr,
        )
