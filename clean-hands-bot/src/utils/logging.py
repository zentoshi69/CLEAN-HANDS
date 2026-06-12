"""Privacy-aware logging.

Only the following fields are ever logged for user activity:
timestamp, telegram user id, image hash, success/failure, detected hand
count, processing time. No file paths of user content, no URLs, no keys.
"""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging once. Safe to call multiple times."""
    global _configured
    if _configured:
        return
    logging.basicConfig(level=level, format=_FORMAT, stream=sys.stdout)
    # Third-party chatter we do not need at INFO.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def log_processing_event(
    logger: logging.Logger,
    *,
    user_id: int,
    image_hash: str,
    success: bool,
    detected_hands: int,
    duration_s: float,
) -> None:
    """Log one image-processing event with the allowed fields only."""
    logger.info(
        "glove_job user=%d image=%s success=%s hands=%d duration=%.2fs",
        user_id,
        image_hash[:16],
        success,
        detected_hands,
        duration_s,
    )
