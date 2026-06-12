"""Temporary file storage and per-user session state.

User images live under the temp dir only (default /tmp/clean-hands-bot)
and are purged by the cleanup job. Session state is in-memory: enough for
/retry and /debug, gone on restart — nothing personal is persisted.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.utils.image_io import ensure_dir


@dataclass
class UserSession:
    """Last-job state for one Telegram user."""

    pending_file_id: str | None = None  # most recent image awaiting /meme
    preferred_mode: str = "balanced"
    last_input_path: str | None = None
    last_mode: str = "balanced"
    last_mask_path: str | None = None
    last_heatmap_path: str | None = None
    last_confidence: float = 0.0
    last_detected_hands: int = 0
    last_debug: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


class LocalStore:
    """Allocates temp paths for downloads/outputs and tracks sessions."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = ensure_dir(base_dir)
        self._sessions: dict[int, UserSession] = {}

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def new_input_path(self, user_id: int, suffix: str = ".jpg") -> Path:
        """Reserve a unique path for an incoming image download."""
        user_dir = ensure_dir(self._base_dir / str(user_id))
        return user_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}"

    def session(self, user_id: int) -> UserSession:
        if user_id not in self._sessions:
            self._sessions[user_id] = UserSession()
        return self._sessions[user_id]

    def remember_job(
        self,
        user_id: int,
        *,
        input_path: str,
        mode: str,
        mask_path: str | None,
        heatmap_path: str | None,
        confidence: float,
        detected_hands: int,
        debug: dict,
    ) -> None:
        session = self.session(user_id)
        session.last_input_path = input_path
        session.last_mode = mode
        session.last_mask_path = mask_path
        session.last_heatmap_path = heatmap_path
        session.last_confidence = confidence
        session.last_detected_hands = detected_hands
        session.last_debug = debug
        session.updated_at = time.time()
