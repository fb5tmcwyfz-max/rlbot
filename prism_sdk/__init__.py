"""
Opti SDK - Python API do Rocket League przez kernel driver.

Warstwy:
  low/     - odczyt/zapis pamieci (PrismKernel driver)
  world/   - obraz gry (Ball, Cars, Players, Match, BoostPads)
  predict/ - fizyka / trajektorie
  control/ - wysylanie inputow do gry
  agent/   - framework botow (Bot base + Manager dla wielu agentow)

Podstawowe uzycie:
    from prism_sdk import World, Manager
    from prism_sdk.agent import SimpleDriveToBallAgent

    world = World()
    world.attach()

    mgr = Manager(world)
    mgr.add_agent(SimpleDriveToBallAgent(), car_index=0)
    mgr.run(hz=140)
"""

__version__ = "0.1.0"

# High-level re-exports (dodawane w miare implementacji warstw)
