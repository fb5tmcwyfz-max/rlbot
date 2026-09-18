"""Framework agentow. Boty dziedzicza Agent, Manager je uruchamia.

Podpiecie wlasnego bota: wrzuc plik do `bots/`, oznacz klase `@bot("nazwa")`
i odpal `python launch_sdk.py --run --bot nazwa`. Szczegoly i szablon:
`bots/template.py` oraz docstring `prism_sdk/agent/registry.py`.
"""

from prism_sdk.agent.base     import Agent, AgentContext
from prism_sdk.agent.manager  import Manager
from prism_sdk.agent.registry import (bot, register, discover, create, get,
                                      available, describe, BotEntry)
