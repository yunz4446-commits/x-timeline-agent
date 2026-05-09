"""Abstract channel interface."""

from abc import ABC, abstractmethod


class Channel(ABC):
    """A messaging channel that the agent can send messages through."""

    @abstractmethod
    async def send_message(self, content: str, chat_id: str | None = None) -> bool:
        """Send a text/markdown message."""

    @abstractmethod
    async def send_card(self, card: dict, chat_id: str | None = None) -> bool:
        """Send a rich card message."""
