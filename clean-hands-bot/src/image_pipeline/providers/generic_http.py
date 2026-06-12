"""Generic HTTP adapter for external masked-edit (inpainting) APIs.

Works with any endpoint that accepts multipart/form-data with an image,
a mask, and prompt fields, and answers with either raw image bytes or a
JSON body containing base64 image data. Endpoint and key come from .env —
never hardcoded.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import requests

from src.image_pipeline.providers.base import (
    ImageEditError,
    ImageEditProvider,
    ProviderConfig,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_JSON_IMAGE_KEYS = ("image", "image_base64", "b64_json", "output", "data")


class GenericHTTPProvider(ImageEditProvider):
    """POSTs image + mask + prompts to a configurable HTTP endpoint."""

    name = "generic_http"

    def __init__(self, provider_config: ProviderConfig) -> None:
        if not provider_config.endpoint:
            raise ImageEditError(
                "IMAGE_PROVIDER_ENDPOINT must be set to use the generic_http provider."
            )
        self._config = provider_config

    def edit_image(
        self,
        input_image_path: str,
        mask_path: str,
        prompt: str,
        negative_prompt: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        config = config or {}
        headers = {}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        data: dict[str, str] = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
        }
        for key, value in {**self._config.extra, **config}.items():
            data.setdefault(str(key), str(value))

        last_error: Exception | None = None
        for attempt in range(1, self._config.max_retries + 1):
            try:
                with open(input_image_path, "rb") as image_fh, open(mask_path, "rb") as mask_fh:
                    response = requests.post(
                        self._config.endpoint,
                        headers=headers,
                        data=data,
                        files={
                            "image": (Path(input_image_path).name, image_fh, "image/png"),
                            "mask": (Path(mask_path).name, mask_fh, "image/png"),
                        },
                        timeout=self._config.timeout_s,
                    )
                if response.status_code in _RETRYABLE_STATUS:
                    raise ImageEditError(
                        f"Provider returned retryable status {response.status_code}"
                    )
                if response.status_code != 200:
                    raise ImageEditError(
                        f"Provider returned status {response.status_code}: "
                        f"{response.text[:200]}"
                    ) from None
                image_bytes = _extract_image_bytes(response)
                return _write_output(input_image_path, image_bytes)
            except (requests.RequestException, ImageEditError) as exc:
                last_error = exc
                if attempt >= self._config.max_retries:
                    break
                backoff = 2 ** attempt
                logger.warning(
                    "generic_http attempt %d/%d failed (%s); retrying in %ds",
                    attempt,
                    self._config.max_retries,
                    type(exc).__name__,
                    backoff,
                )
                time.sleep(backoff)

        raise ImageEditError(f"Image edit failed after retries: {last_error}")


def _extract_image_bytes(response: requests.Response) -> bytes:
    content_type = response.headers.get("Content-Type", "")
    if content_type.startswith("image/"):
        return response.content

    if "json" in content_type:
        payload = response.json()
        node: Any = payload
        # Common shapes: {"image": b64}, {"data": [{"b64_json": ...}]}, etc.
        if isinstance(node, dict):
            for key in _JSON_IMAGE_KEYS:
                if key in node:
                    node = node[key]
                    break
        if isinstance(node, list) and node:
            node = node[0]
        if isinstance(node, dict):
            for key in _JSON_IMAGE_KEYS:
                if key in node and isinstance(node[key], str):
                    node = node[key]
                    break
        if isinstance(node, str):
            b64 = node.split(",", 1)[-1]  # tolerate data: URLs
            try:
                return base64.b64decode(b64)
            except ValueError as exc:
                raise ImageEditError("Provider JSON contained invalid base64") from exc

    raise ImageEditError(
        f"Provider response not understood (Content-Type: {content_type or 'missing'})"
    )


def _write_output(input_image_path: str, image_bytes: bytes) -> str:
    src = Path(input_image_path)
    output_path = src.with_name(f"{src.stem}_gloved.png")
    output_path.write_bytes(image_bytes)
    return str(output_path)
