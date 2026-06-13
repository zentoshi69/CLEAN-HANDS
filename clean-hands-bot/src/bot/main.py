"""Bot entrypoint: wire settings, storage, middleware, handlers; poll."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher

from src.bot.commands import set_bot_commands
from src.bot.handlers import router
from src.bot.middleware import RateLimitMiddleware
from src.storage.cleanup import cleanup_loop
from src.storage.local_store import LocalStore
from src.utils.config import Settings
from src.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


async def run() -> None:
    setup_logging()
    settings = Settings.from_env(require_token=True)
    store = LocalStore(settings.temp_dir)

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher(settings=settings, store=store)
    dispatcher.message.middleware(RateLimitMiddleware())
    dispatcher.include_router(router)

    await set_bot_commands(bot)
    janitor = asyncio.create_task(
        cleanup_loop(settings.temp_dir, settings.delete_files_after_hours)
    )

    logger.info(
        "clean-hands bot starting (provider=%s, temp=%s)",
        settings.image_provider,
        settings.temp_dir,
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        janitor.cancel()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
