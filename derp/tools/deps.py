from dataclasses import dataclass


@dataclass
class AgentDeps:
    """Dependencies for the chat memory tool."""

    chat_id: int
