"""Bot command metadata and all user-facing copy."""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand

# Commands that trigger image processing (rate-limited).
PROCESSING_COMMANDS = ("/meme", "/retry", "/soft", "/hard")

MSG_START = (
    "🧤 CLEAN HANDS DIRTY MONEY\n\n"
    "Send me an image with visible hands, then reply to it with /meme "
    "(or send the image with /meme as the caption). I add light blue "
    "transparent medical gloves to every visible hand — and change "
    "absolutely nothing else.\n\n"
    "Commands:\n"
    "/meme — glove the hands\n"
    "/retry — redo the last image with a stricter prompt\n"
    "/soft — more natural transparent glove\n"
    "/hard — more visible glossy medical glove\n\n"
    "Clean hands. Dirty money."
)

MSG_PROCESSING = "Gloving the hands… tiny medical goblins are working."
MSG_SUCCESS_CAPTION = "CLEAN HANDS. DIRTY MONEY."
MSG_NO_HANDS = "No cleanable hands found. Upload an image with visible hands."
MSG_NO_HANDS_GOBLIN = "No cleanable hands found. The glove goblin found nothing to bless."
MSG_FAILURE = (
    "The glove machine got too excited and tried to mutate reality. "
    "Send a clearer image with visible hands."
)
MSG_QUALITY_FAILURE = (
    "The glove machine tried but the AI goblin touched too much. "
    "Try a clearer hand photo."
)
MSG_RATE_LIMIT = "Glove factory overheated. Try again in a minute."
MSG_NO_IMAGE = (
    "I need an image to glove. Send a photo with /meme as the caption, "
    "or reply to a photo with /meme."
)
MSG_NO_RETRY = "Nothing to retry yet. Send an image with /meme first."
MSG_IMAGE_TOO_LARGE = "That image is too large. Max size is {max_mb} MB."
MSG_NOT_ADMIN = "The /debug door is for glove administrators only."
MSG_NO_DEBUG = "No debug data yet. Run /meme on an image first."
MSG_MODE_SET = "Glove mode set to {mode}. Reply to an image with /meme, or send one now."

BOT_COMMANDS = [
    BotCommand(command="start", description="What this bot does"),
    BotCommand(command="meme", description="Add medical gloves to visible hands"),
    BotCommand(command="retry", description="Redo last image with stricter prompt"),
    BotCommand(command="soft", description="More natural transparent glove"),
    BotCommand(command="hard", description="More visible glossy glove"),
]


async def set_bot_commands(bot: Bot) -> None:
    """Register the command menu with Telegram (excludes admin /debug)."""
    await bot.set_my_commands(BOT_COMMANDS)
