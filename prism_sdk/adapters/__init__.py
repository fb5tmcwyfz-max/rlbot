"""
Adaptery dla istniejacych modeli Opti/Nexto/Necto - przechodza z prism_sdk.World
na format ktorego oczekuja oryginalne (rlgym-based) modele.

Strategia:
  * `_rlgym_shim` udaje rlgym.GameState/PlayerData/PhysicsObject - dzieki temu
    oryginalny CoyoteObs.py (73 KB!) i pretrained_agents/nexto/nexto_v2_obs.py
    dzialaja BEZ MODYFIKACJI.
  * `coyote_action` - lookup table 91 akcji (przepisane z CoyoteParser.py -
    inaczej wymagaloby ActionParser bazowej z rlgym).
  * `opti_agent`, `nexto_agent`, `necto_agent` - Agenty dla Managera ktore
    laduja pretrained torch model, biora World, zwracaja ControllerState.

Uzycie:
    from prism_sdk.adapters import NextoAgent, OptiAgent, CoyoteActionParser

    bot = NextoAgent()          # laduje nexto-model.pt
    manager.bind(bot, car_index=0)
"""

from prism_sdk.adapters.coyote_action import CoyoteActionParser, ACTION_SPACE_SIZE
from prism_sdk.adapters.coyote_obs    import build_obs_from_world

# Agenty ladujemy lazy - torch nie musi byc jak nie uzywasz NN
def NextoAgent(*args, **kwargs):
    """Wytrenowany Nexto - laduje pretrained_agents/nexto/nexto-model.pt."""
    from prism_sdk.adapters.nexto import NextoAgent as _NextoAgent
    return _NextoAgent(*args, **kwargs)


def NectoAgent(*args, **kwargs):
    """Wytrenowany Necto v1 - laduje pretrained_agents/necto/necto-model-30Y.pt."""
    from prism_sdk.adapters.necto import NectoAgent as _NectoAgent
    return _NectoAgent(*args, **kwargs)


def OptiAgent(*args, **kwargs):
    """Wytrenowany Opti model. Wymaga podania model_path (wagi nie sa w repo)."""
    from prism_sdk.adapters.opti import OptiAgent as _OptiAgent
    return _OptiAgent(*args, **kwargs)
