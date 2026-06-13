"""Scheduled deletion of temporary user images.

Privacy rule: originals and outputs are deleted after
DELETE_FILES_AFTER_HOURS (default 24h). The purge function is synchronous
and unit-testable; the async loop wraps it for the bot process.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from src.utils.logging import get_logger

logger = get_logger(__name__)


def purge_old_files(base_dir: Path, max_age_hours: float, now: float | None = None) -> int:
    """Delete files under base_dir older than max_age_hours. Returns count."""
    if not base_dir.exists():
        return 0
    now = now if now is not None else time.time()
    cutoff = now - max_age_hours * 3600
    deleted = 0
    for path in sorted(base_dir.rglob("*"), reverse=True):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            # File may vanish mid-scan (concurrent job); skip quietly.
            continue
    if deleted:
        logger.info("cleanup removed %d expired files", deleted)
    return deleted


async def cleanup_loop(
    base_dir: Path,
    max_age_hours: float,
    interval_s: float = 3600.0,
) -> None:
    """Run purge_old_files forever on a fixed interval."""
    while True:
        try:
            await asyncio.to_thread(purge_old_files, base_dir, max_age_hours)
        except Exception:  # never let the janitor kill the bot
            logger.exception("cleanup pass failed")
        await asyncio.sleep(interval_s)
