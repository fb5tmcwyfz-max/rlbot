"""
Fizyczne + geometryczne stale Rocket League.

Wszystkie wartosci w unreal units (uu). Nie ruszaj bez fizycznej weryfikacji.
Zrodlo: RLBot wiki + eksperymentalnie potwierdzone.
"""

# ==================================================================
# ARENA - Standard Map (DFH Stadium)
# ==================================================================
# Uklad wspolrzednych RL:
#   X: -4096 (west) .. +4096 (east) - szerokosc pola
#   Y: -5120 (blue goal side) .. +5120 (orange goal side) - dlugosc
#   Z: 0 (podloga) .. 2044 (sufit)
#
# Blue druzyna zaczyna od Y-negative, broni bramki blue na y = -5120.
# Orange broni bramki na y = +5120.
# ==================================================================

FIELD_LENGTH   = 10240.0
FIELD_WIDTH    = 8192.0
FIELD_HEIGHT   = 2044.0
FIELD_HALF_LENGTH = FIELD_LENGTH / 2   # = 5120
FIELD_HALF_WIDTH  = FIELD_WIDTH  / 2   # = 4096

# --- Podloga / sufit ---
FLOOR_Z        = 0.0
CEILING_Z      = FIELD_HEIGHT           # = 2044

# --- Sciany (surface planes) ---
SIDE_WALL_X       = FIELD_HALF_WIDTH    # = 4096, sciana boczna x = ±4096
BACK_WALL_Y       = FIELD_HALF_LENGTH   # = 5120, sciana tylna y = ±5120

# --- Corner ramps (zaokrąglenia w rogach) ---
# Corner to trojkatna scianka o kacie 45° biegnaca od (x=4096, y=3968) do (x=3968, y=5120)
# W rzeczywistosci to lagodne zaokraglenie ale approximujemy plaszczyzna.
CORNER_WALL_MIN_XY = 1152.0    # od tego X i Y zaczyna sie corner (od srodka areny)
# Rownanie corner walls: |x| + |y| = CORNER_WALL_SUM
CORNER_WALL_SUM    = 8064.0    # x + y (kazde absolutne) - suma na krawedzi corner
# Tzn: dla dowolnego (x, y) w rogu, warunek: |x| >= 3968-y (proporcja 1:1)

# --- Goal (unified for both goals) ---
GOAL_HEIGHT    = 642.775
GOAL_WIDTH     = 1786.0                 # cala szerokosc bramki
GOAL_DEPTH     = 880.0                  # od goal-line w tyl (do siatki)
GOAL_HALF_WIDTH = GOAL_WIDTH / 2        # = 893

# Blue goal @ y = -5120
BLUE_GOAL_Y    = -FIELD_HALF_LENGTH     # = -5120
ORANGE_GOAL_Y  =  FIELD_HALF_LENGTH     # = +5120

# Goal centers (na goal-line, srodek wysokosci)
BLUE_GOAL_CENTER   = ( 0.0, BLUE_GOAL_Y,   GOAL_HEIGHT / 2)
ORANGE_GOAL_CENTER = ( 0.0, ORANGE_GOAL_Y, GOAL_HEIGHT / 2)

# Goal opening bounds (na goal-line)
GOAL_LINE_X_MIN = -GOAL_HALF_WIDTH      # -893
GOAL_LINE_X_MAX =  GOAL_HALF_WIDTH      # +893
GOAL_LINE_Z_MIN =  0.0
GOAL_LINE_Z_MAX =  GOAL_HEIGHT          # 642.775

# --- Kickoff spots (5 pozycji, gracz zawsze zaczyna na jednej) ---
# Uzywane w standardowym Soccar (1v1, 2v2, 3v3).
# X positive = right od blue perspektywy.
KICKOFF_SPOTS_BLUE = [
    (-2048.0, -2560.0, 17.0),    # 3v3 left
    ( 2048.0, -2560.0, 17.0),    # 3v3 right
    ( -256.0, -3840.0, 17.0),    # 3v3 back-left / 2v2 left
    (  256.0, -3840.0, 17.0),    # 3v3 back-right / 2v2 right
    (    0.0, -4608.0, 17.0),    # 1v1 middle / 3v3 center
]
# Orange mirror (X i Y negated)
KICKOFF_SPOTS_ORANGE = [(-x, -y, z) for (x, y, z) in KICKOFF_SPOTS_BLUE]

# --- Wall / ceiling thresholds (do wykrywania czy auto jest na powierzchni) ---
# 20 uu tolerancji na wheels touch
GROUND_TOUCH_Z_MAX   = 25.0
CEILING_TOUCH_Z_MIN  = CEILING_Z - 25.0
WALL_TOUCH_TOL       = 25.0

# ==================================================================
# BALL
# ==================================================================

BALL_RADIUS         = 91.25         # standard ball
BALL_MASS           = 30.0          # kg (dla obliczen)
BALL_MAX_SPEED      = 6000.0        # uu/s (cap silnika)
BALL_DRAG           = 0.030305      # per second (multiplikatywny)
BALL_FRICTION       = 2.0
BALL_RESTITUTION    = 0.6           # bounce factor (0..1)
BALL_ROLL_SPEED     = 200.0         # threshold below which ball rolls

# Grawitacja (uu/s^2)
GRAVITY             = -650.0        # aplikowana na Z pilki i auta

# ==================================================================
# CAR
# ==================================================================

CAR_MAX_SPEED           = 2300.0    # przy boost
CAR_SUPERSONIC_SPEED    = 2200.0    # threshold flag
CAR_MAX_SPEED_NO_BOOST  = 1410.0    # bez boosta na plaskim
CAR_MAX_ANGULAR_SPEED   = 5.5       # rad/s

# Boost
CAR_BOOST_MAX           = 100.0
CAR_BOOST_CONSUMPTION   = 33.3      # per second at full throttle
CAR_BOOST_ACCELERATION  = 991.6667  # uu/s^2 gdy boost trzymany

# Jump
JUMP_INIT_SPEED    = 292.0          # instant Z boost przy jump
JUMP_MIN_HOLD      = 0.025          # sec - minimum wciskania
JUMP_MAX_HOLD      = 0.2            # sec - max hold dla full jump
JUMP_HOLD_ACCEL    = 1458.0         # uu/s^2 gdy trzymamy jump

DOUBLE_JUMP_SPEED  = 292.0
DODGE_IMPULSE      = 500.0          # uu/s XY impulse
DODGE_TORQUE       = 260.0          # rotation impulse
DODGE_COOLDOWN     = 1.25           # sec od dodge do refresh
FLIP_TIME          = 1.5            # sec - od jump do koniec dodge-timera

# Boost pad respawn
BIG_PAD_RESPAWN     = 10.0          # sec
SMALL_PAD_RESPAWN   = 4.0           # sec
BIG_PAD_BOOST       = 100.0
SMALL_PAD_BOOST     = 12.0

# ==================================================================
# CAR HITBOXY (approximated by body type)
# ==================================================================

class HitboxType:
    OCTANE     = "octane"       # 118 x 84 x 36
    DOMINUS    = "dominus"      # 127 x 83 x 32
    PLANK      = "plank"        # 129 x 84 x 30   (Batmobile, Breakout)
    BREAKOUT   = "breakout"     # = plank alias
    HYBRID     = "hybrid"       # 127 x 83 x 34   (Fennec, ...)
    MERC       = "merc"         # 121 x 84 x 39
    BONE       = "bone"         # 130 x 82 x 32   (Endo, Twinzer)

HITBOX_DIMS = {
    HitboxType.OCTANE:   (118.007, 84.200, 36.159),
    HitboxType.DOMINUS:  (127.929, 83.280, 31.300),
    HitboxType.PLANK:    (128.821, 84.670, 29.394),
    HitboxType.BREAKOUT: (128.821, 84.670, 29.394),
    HitboxType.HYBRID:   (127.019, 82.188, 34.159),
    HitboxType.MERC:     (120.720, 76.712, 41.659),
    HitboxType.BONE:     (129.518, 82.100, 32.115),
}

# offset od pivot auta do centrum hitboxa (X = forward)
HITBOX_OFFSETS = {
    HitboxType.OCTANE:   (13.876, 0.0, 20.755),
    HitboxType.DOMINUS:  (9.008,  0.0, 15.750),
    HitboxType.PLANK:    (9.008,  0.0, 12.096),
    HitboxType.BREAKOUT: (9.008,  0.0, 12.096),
    HitboxType.HYBRID:   (13.865, 0.0, 20.755),
    HitboxType.MERC:     (11.377, 0.0, 21.336),
    HitboxType.BONE:     (12.500, 0.0, 15.750),
}

# ==================================================================
# TIMINGS
# ==================================================================

KICKOFF_COUNTDOWN     = 3.0         # sec countdown przed rundzie
GOAL_REPLAY_TIME      = 5.0         # sec zamrozenia po golu
DEMOLITION_RESPAWN    = 3.0         # sec

# ==================================================================
# HELPERS - geometry
# ==================================================================

def is_in_goal(position, team_num: int, tolerance: float = 5.0) -> bool:
    """True jesli punkt (x, y, z) jest w bramce team_num (0=blue, 1=orange)."""
    x, y, z = position[0], position[1], position[2]
    if z < 0 or z > GOAL_HEIGHT + tolerance:
        return False
    if abs(x) > GOAL_HALF_WIDTH + tolerance:
        return False
    if team_num == 0:
        return y <= -FIELD_HALF_LENGTH + tolerance
    else:
        return y >=  FIELD_HALF_LENGTH - tolerance


def side_of_field(y: float) -> int:
    """0 = blue half, 1 = orange half."""
    return 0 if y < 0 else 1
