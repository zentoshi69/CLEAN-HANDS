"""Telegram message handlers for the glove bot."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message, PhotoSize

from src.bot import commands as copy
from src.image_pipeline.pipeline import (
    REASON_NO_HANDS,
    REASON_QUALITY_REJECTED,
    ProcessResult,
    process_image_for_gloves,
)
from src.storage.local_store import LocalStore
from src.utils.config import Settings
from src.utils.image_io import file_sha256
from src.utils.logging import get_logger, log_processing_event

logger = get_logger(__name__)
router = Router(name="clean-hands")

# One pipeline job at a time: MediaPipe graphs are not thread-safe and a
# single worker keeps memory bounded on small hosts.
_pipeline_lock = asyncio.Lock()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(copy.MSG_START)


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_image(message: Message, bot: Bot, settings: Settings, store: LocalStore) -> None:
    """Image received: process now if captioned with a command, else hold it."""
    if message.from_user is None:
        return
    caption = (message.caption or "").strip().lower()
    session = store.session(message.from_user.id)

    mode = session.preferred_mode
    if caption.startswith("/soft"):
        mode = "soft"
    elif caption.startswith("/hard"):
        mode = "hard"

    if any(caption.startswith(cmd) for cmd in copy.PROCESSING_COMMANDS):
        await _run_glove_job(message, bot, settings, store, source_message=message, mode=mode)
        return

    # Any image, no command needed: glove it immediately. The upload itself is
    # auto-deleted inside _run_glove_job the instant it's safely downloaded.
    await _run_glove_job(message, bot, settings, store, source_message=message, mode=mode)


@router.message(Command("meme"))
async def handle_meme(message: Message, bot: Bot, settings: Settings, store: LocalStore) -> None:
    if message.from_user is None:
        return
    mode = store.session(message.from_user.id).preferred_mode
    await _run_glove_job(
        message, bot, settings, store, source_message=message.reply_to_message, mode=mode
    )


@router.message(Command("soft"))
async def handle_soft(message: Message, bot: Bot, settings: Settings, store: LocalStore) -> None:
    await _handle_mode_command(message, bot, settings, store, mode="soft")


@router.message(Command("hard"))
async def handle_hard(message: Message, bot: Bot, settings: Settings, store: LocalStore) -> None:
    await _handle_mode_command(message, bot, settings, store, mode="hard")


@router.message(Command("retry"))
async def handle_retry(message: Message, bot: Bot, settings: Settings, store: LocalStore) -> None:
    """Re-run the user's last image with the strict prompt."""
    if message.from_user is None:
        return
    session = store.session(message.from_user.id)
    if not session.last_input_path or not Path(session.last_input_path).exists():
        await message.reply(copy.MSG_NO_RETRY)
        return
    await _process_and_reply(
        message, settings, store, input_path=Path(session.last_input_path), mode="strict"
    )


@router.message(Command("debug"))
async def handle_debug(message: Message, settings: Settings, store: LocalStore) -> None:
    """Admin-only: return the last mask, diff heatmap, and confidence data."""
    if message.from_user is None:
        return
    if not settings.is_admin(message.from_user.id):
        await message.reply(copy.MSG_NOT_ADMIN)
        return
    session = store.session(message.from_user.id)
    if not session.last_debug:
        await message.reply(copy.MSG_NO_DEBUG)
        return

    summary = (
        f"hands: {session.last_detected_hands}\n"
        f"confidence: {session.last_confidence:.3f}\n"
        f"mode: {session.last_mode}\n"
        f"debug: {json.dumps(session.last_debug, default=str)[:2800]}"
    )
    await message.reply(summary)
    for label, path in (
        ("mask", session.last_mask_path),
        ("diff heatmap", session.last_heatmap_path),
    ):
        if path and Path(path).exists():
            await message.reply_photo(FSInputFile(path), caption=label)


async def _handle_mode_command(
    message: Message, bot: Bot, settings: Settings, store: LocalStore, *, mode: str
) -> None:
    """Set the user's glove mode; process immediately when replying to an image."""
    if message.from_user is None:
        return
    store.session(message.from_user.id).preferred_mode = mode
    source = message.reply_to_message
    if source and _file_id_from(source):
        await _run_glove_job(message, bot, settings, store, source_message=source, mode=mode)
    else:
        await message.reply(copy.MSG_MODE_SET.format(mode=mode))


async def _run_glove_job(
    message: Message,
    bot: Bot,
    settings: Settings,
    store: LocalStore,
    *,
    source_message: Message | None,
    mode: str,
) -> None:
    """Resolve the target image, download it, and run the pipeline."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    session = store.session(user_id)

    file_id, file_size = (None, None)
    if source_message is not None:
        file_id, file_size = _file_ref_from(source_message)
    if file_id is None and session.pending_file_id:
        file_id = session.pending_file_id
    if file_id is None:
        await message.reply(copy.MSG_NO_IMAGE)
        return

    if file_size and file_size > settings.max_image_size_bytes:
        await message.reply(
            copy.MSG_IMAGE_TOO_LARGE.format(max_mb=settings.max_image_size_mb)
        )
        return

    input_path = store.new_input_path(user_id)
    try:
        await bot.download(file_id, destination=str(input_path))
    except Exception:
        logger.exception("download failed for user=%d", user_id)
        await message.reply(copy.MSG_FAILURE)
        return

    # Privacy: the moment the image is safely on disk, delete the user's upload
    # so the original never lingers in the chat. Bots can delete incoming
    # messages in private chats; in groups this needs admin, so it's best-effort.
    if source_message is not None:
        try:
            await source_message.delete()
        except Exception:
            logger.debug("could not delete upload for user=%d", user_id)

    await _process_and_reply(message, settings, store, input_path=input_path, mode=mode)


async def _process_and_reply(
    message: Message,
    settings: Settings,
    store: LocalStore,
    *,
    input_path: Path,
    mode: str,
) -> None:
    """Run the pipeline in a worker thread and deliver the result."""
    assert message.from_user is not None
    user_id = message.from_user.id
    # `answer` (not `reply`): the user's upload was just deleted, so there's no
    # message left to reply to — send fresh into the chat instead.
    status = await message.answer(copy.MSG_PROCESSING)
    started = time.monotonic()

    try:
        async with _pipeline_lock:
            result: ProcessResult = await asyncio.to_thread(
                process_image_for_gloves, str(input_path), mode, settings=settings
            )
    except Exception:
        logger.exception("pipeline crashed for user=%d", user_id)
        await status.edit_text(copy.MSG_FAILURE)
        return

    duration = time.monotonic() - started
    log_processing_event(
        logger,
        user_id=user_id,
        image_hash=file_sha256(input_path),
        success=result.success,
        detected_hands=result.detected_hands,
        duration_s=duration,
    )
    store.remember_job(
        user_id,
        input_path=str(input_path),
        mode=mode,
        mask_path=result.mask_path,
        heatmap_path=result.debug.get("heatmap_path"),
        confidence=result.confidence,
        detected_hands=result.detected_hands,
        debug=result.debug,
    )

    if result.success and result.output_path:
        await message.answer_photo(
            FSInputFile(result.output_path), caption=copy.MSG_SUCCESS_CAPTION
        )
        await status.delete()
        return

    reason = result.debug.get("reason")
    if reason == REASON_NO_HANDS:
        await status.edit_text(copy.MSG_NO_HANDS_GOBLIN)
    elif reason == REASON_QUALITY_REJECTED:
        await status.edit_text(copy.MSG_QUALITY_FAILURE)
    else:
        await status.edit_text(copy.MSG_FAILURE)


def _file_id_from(message: Message) -> str | None:
    file_id, _ = _file_ref_from(message)
    return file_id


def _file_ref_from(message: Message) -> tuple[str | None, int | None]:
    """Extract (file_id, file_size) from a photo or image document."""
    if message.photo:
        largest: PhotoSize = message.photo[-1]
        return largest.file_id, largest.file_size
    document = message.document
    if document and (document.mime_type or "").startswith("image/"):
        return document.file_id, document.file_size
    return None, None
