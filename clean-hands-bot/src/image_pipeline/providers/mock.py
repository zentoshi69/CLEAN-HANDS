"""Local mock provider: composites a glove tint with no external API.

Useful for development, tests, and demos. It alpha-blends the brand
light-blue (#9ED8FF) over the masked hand region, adds a mild gloss boost
on already-bright pixels, and leaves everything outside the mask
byte-identical — so it also exercises the quality gate honestly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.image_pipeline.providers.base import ImageEditError, ImageEditProvider

# Brand glove color #9ED8FF in BGR order.
GLOVE_BGR = np.array([255.0, 216.0, 158.0], dtype=np.float32)


class MockProvider(ImageEditProvider):
    """Deterministic offline glove compositor."""

    name = "mock"

    def __init__(self, glove_alpha: float = 0.45, gloss_strength: float = 18.0) -> None:
        self._glove_alpha = glove_alpha
        self._gloss_strength = gloss_strength

    def edit_image(
        self,
        input_image_path: str,
        mask_path: str,
        prompt: str,
        negative_prompt: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        config = config or {}
        image = cv2.imread(input_image_path, cv2.IMREAD_COLOR)
        mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ImageEditError(f"Mock provider cannot read {Path(input_image_path).name}")
        if mask is None:
            raise ImageEditError(f"Mock provider cannot read mask {Path(mask_path).name}")
        if mask.shape != image.shape[:2]:
            raise ImageEditError("Mock provider: mask and image sizes differ")

        alpha = self._glove_alpha
        mode = str(config.get("mode", "balanced"))
        if mode == "soft":
            alpha = 0.32
        elif mode == "hard":
            alpha = 0.58

        blend = (mask.astype(np.float32) / 255.0) * alpha
        blend = blend[:, :, None]

        img_f = image.astype(np.float32)
        tinted = img_f * (1.0 - blend) + GLOVE_BGR[None, None, :] * blend

        # Gloss: lift highlights inside the mask where the source is bright.
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        gloss = (gray**2)[:, :, None] * blend * self._gloss_strength
        tinted = np.clip(tinted + gloss, 0, 255).astype(np.uint8)

        output_path = _output_path_for(input_image_path)
        if not cv2.imwrite(str(output_path), tinted):
            raise ImageEditError("Mock provider failed to write output image")
        return str(output_path)


def _output_path_for(input_image_path: str) -> Path:
    src = Path(input_image_path)
    return src.with_name(f"{src.stem}_gloved{src.suffix or '.png'}")
