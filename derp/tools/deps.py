from dataclasses import dataclass

from aiogram.types import Message


@dataclass
class AgentDeps:
    """Dependencies for the chat memory tool."""

    message: Message
