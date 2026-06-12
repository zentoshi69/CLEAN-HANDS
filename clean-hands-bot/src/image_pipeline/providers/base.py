"""Provider interface for masked image editing (inpainting)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ImageEditError(RuntimeError):
    """Raised when a provider fails to produce an edited image."""


@dataclass(frozen=True)
class ProviderConfig:
    """Connection settings for a provider instance. Keys come from .env."""

    api_key: str = ""
    endpoint: str = ""
    timeout_s: float = 120.0
    max_retries: int = 3
    extra: dict[str, Any] = field(default_factory=dict)


class ImageEditProvider(ABC):
    """A backend that applies a prompt-guided edit inside a mask.

    Contract: pixels outside the mask should be preserved; the pipeline's
    quality gate independently verifies this and rejects sloppy output.
    """

    name: str = "base"

    @abstractmethod
    def edit_image(
        self,
        input_image_path: str,
        mask_path: str,
        prompt: str,
        negative_prompt: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        """Edit `input_image_path` inside `mask_path`; return output path.

        Args:
            input_image_path: source image on local disk.
            mask_path: single-channel PNG; white = editable, black = frozen.
            prompt: positive edit instruction.
            negative_prompt: what must not change.
            config: optional per-request overrides (mode, seed, ...).

        Raises:
            ImageEditError: when the edit cannot be produced.
        """


class FutureProvider(ImageEditProvider):
    """Placeholder slot for the next provider integration."""

    name = "future"

    def edit_image(
        self,
        input_image_path: str,
        mask_path: str,
        prompt: str,
        negative_prompt: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        raise ImageEditError(
            "The 'future' provider is a placeholder. Set IMAGE_PROVIDER to "
            "'mock' or 'generic_http' in your .env."
        )
