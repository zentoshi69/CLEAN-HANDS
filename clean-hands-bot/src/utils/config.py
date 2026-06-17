"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

try:  # optional: .env support for local development
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _parse_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _parse_optional_int(env: Mapping[str, str], key: str) -> int | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _parse_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _parse_id_set(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"ADMIN_TELEGRAM_IDS must be comma-separated integers, got {chunk!r}"
            ) from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for the bot and image pipeline."""

    telegram_bot_token: str = ""
    image_provider: str = "mock"
    image_provider_api_key: str = ""
    image_provider_endpoint: str = ""
    image_provider_seed: int | None = None
    admin_telegram_ids: frozenset[int] = field(default_factory=frozenset)
    max_image_size_mb: int = 15
    output_max_side: int = 1536
    delete_files_after_hours: int = 24
    temp_dir: Path = Path("/tmp/clean-hands-bot")
    # Hand detection: tighter defaults reduce phantom "invented" hands.
    hand_max_num_hands: int = 4
    hand_min_detection_confidence: float = 0.6

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_token: bool = False,
    ) -> "Settings":
        """Build settings from the process environment (and .env if present)."""
        if env is None:
            if load_dotenv is not None:
                load_dotenv()
            env = os.environ

        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if require_token and not token:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather "
                "and put the token in your .env file."
            )

        return cls(
            telegram_bot_token=token,
            image_provider=env.get("IMAGE_PROVIDER", "mock").strip().lower() or "mock",
            image_provider_api_key=env.get("IMAGE_PROVIDER_API_KEY", "").strip(),
            image_provider_endpoint=env.get("IMAGE_PROVIDER_ENDPOINT", "").strip(),
            image_provider_seed=_parse_optional_int(env, "IMAGE_PROVIDER_SEED"),
            admin_telegram_ids=_parse_id_set(env.get("ADMIN_TELEGRAM_IDS", "")),
            max_image_size_mb=_parse_int(env, "MAX_IMAGE_SIZE_MB", 15),
            output_max_side=_parse_int(env, "OUTPUT_MAX_SIDE", 1536),
            delete_files_after_hours=_parse_int(env, "DELETE_FILES_AFTER_HOURS", 24),
            temp_dir=Path(env.get("TEMP_DIR", "/tmp/clean-hands-bot")),
            hand_max_num_hands=_parse_int(env, "HAND_MAX_NUM_HANDS", 4),
            hand_min_detection_confidence=_parse_float(
                env, "HAND_MIN_DETECTION_CONFIDENCE", 0.6
            ),
        )

    @property
    def max_image_size_bytes(self) -> int:
        return self.max_image_size_mb * 1024 * 1024

    def is_admin(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.admin_telegram_ids
