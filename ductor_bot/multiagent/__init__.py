"""Multi-agent architecture: supervisor, bus, and inter-agent communication."""

from ductor_bot.multiagent.bus import InterAgentBus
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.models import SubAgentConfig
from ductor_bot.multiagent.supervisor import AgentSupervisor

__all__ = ["AgentHealth", "AgentSupervisor", "InterAgentBus", "SubAgentConfig"]
