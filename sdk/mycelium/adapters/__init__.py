"""Framework adapters for all 10 supported frameworks."""

from .autogen import AutoGenContextProtection, AutoGenIntegration
from .cline import ClineContextProtection, ClineIntegration
from .crewai import CrewAIContextProtection, CrewAIIntegration
from .langchain import LangChainContextProtection, LangChainIntegration
from .langgraph import LangGraphContextProtection, LangGraphIntegration
from .livekit import LiveKitContextProtection, LiveKitIntegration
from .openai_agents import OpenAIAgentsContextProtection, OpenAIAgentsIntegration
from .openhands import OpenHandsContextProtection, OpenHandsIntegration
from .smolagents import SmolagentsContextProtection, SmolagentsIntegration
from .stagehand import StagehandContextProtection, StagehandIntegration

__all__ = [
    "LangGraphContextProtection", "LangGraphIntegration",
    "CrewAIContextProtection", "CrewAIIntegration",
    "AutoGenContextProtection", "AutoGenIntegration",
    "OpenAIAgentsContextProtection", "OpenAIAgentsIntegration",
    "SmolagentsContextProtection", "SmolagentsIntegration",
    "LangChainContextProtection", "LangChainIntegration",
    "LiveKitContextProtection", "LiveKitIntegration",
    "OpenHandsContextProtection", "OpenHandsIntegration",
    "ClineContextProtection", "ClineIntegration",
    "StagehandContextProtection", "StagehandIntegration",
]
