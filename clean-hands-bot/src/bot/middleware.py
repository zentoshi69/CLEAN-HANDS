"""Aiogram middleware: per-user rate limiting for processing commands."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from src.bot.commands import MSG_RATE_LIMIT, PROCESSING_COMMANDS


def _is_processing_request(message: Message) -> bool:
    text = (message.text or message.caption or "").strip().lower()
    return any(text.startswith(cmd) for cmd in PROCESSING_COMMANDS)


class RateLimitMiddleware(BaseMiddleware):
    """Allow one glove job per user per cooldown window."""

    def __init__(self, cooldown_s: float = 20.0) -> None:
        self._cooldown_s = cooldown_s
        self._last_request: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and event.from_user is not None
            and _is_processing_request(event)
        ):
            user_id = event.from_user.id
            now = time.monotonic()
            last = self._last_request.get(user_id, 0.0)
            if now - last < self._cooldown_s:
                await event.reply(MSG_RATE_LIMIT)
                return None
            self._last_request[user_id] = now
        return await handler(event, data)
