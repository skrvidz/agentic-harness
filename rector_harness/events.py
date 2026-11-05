from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field


class HarnessEvent(BaseModel):
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    kind: str
    run_id: str | None = None
    turn: int = 0
    role: str | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


Listener = Callable[[HarnessEvent], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def publish(self, event: HarnessEvent) -> None:
        for listener in tuple(self._listeners):
            result = listener(event)
            if inspect.isawaitable(result):
                await result
