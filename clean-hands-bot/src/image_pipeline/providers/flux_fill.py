"""FLUX.1 Fill [pro] inpainting provider (Black Forest Labs API).

FLUX.1 Fill is a *mask-native* inpainting model: you hand it the image, a
binary mask (white = repaint, black = keep), and a prompt, and it repaints
only the masked region. That is exactly the contract this pipeline was built
around — the hand mask tells FLUX where the gloves go and nothing else moves.

The BFL API is asynchronous: submit a job, receive a ``polling_url``, poll
until ``status`` is ``Ready``, then download the signed result URL. Endpoint
and key come from ``.env`` — never hardcoded.
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

DEFAULT_ENDPOINT = "https://api.bfl.ai/v1/flux-pro-1.0-fill"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_STATUS_READY = "Ready"
# Terminal non-Ready states BFL can report — fail fast instead of polling forever.
_STATUS_FATAL = {
    "Error",
    "Failed",
    "Content Moderated",
    "Request Moderated",
    "Task not found",
}


class FluxFillProvider(ImageEditProvider):
    """Glove inpainting via Black Forest Labs FLUX.1 Fill [pro]."""

    name = "flux_fill"

    def __init__(self, provider_config: ProviderConfig) -> None:
        if not provider_config.api_key:
            raise ImageEditError(
                "IMAGE_PROVIDER_API_KEY must be set to your Black Forest Labs "
                "key to use the flux_fill provider."
            )
        self._config = provider_config
        self._endpoint = provider_config.endpoint or DEFAULT_ENDPOINT
        extra = provider_config.extra
        self._steps = int(extra.get("steps", 50))
        self._guidance = float(extra.get("guidance", 30.0))
        self._poll_interval_s = float(extra.get("poll_interval_s", 2.0))

    def edit_image(
        self,
        input_image_path: str,
        mask_path: str,
        prompt: str,
        negative_prompt: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        # FLUX Fill has no negative-prompt field; the mask + the pipeline's
        # composite already guarantee nothing outside the hands changes.
        config = config or {}
        payload: dict[str, Any] = {
            "image": _b64_file(input_image_path),
            "mask": _b64_file(mask_path),
            "prompt": prompt,
            "steps": int(config.get("steps", self._steps)),
            "guidance": float(config.get("guidance", self._guidance)),
            "output_format": "png",
            "safety_tolerance": 2,
        }
        seed = config.get("seed")
        if seed is not None:
            payload["seed"] = int(seed)

        headers = {"x-key": self._config.api_key, "Content-Type": "application/json"}

        polling_url = self._submit(headers, payload)
        sample_url = self._poll(headers, polling_url)
        image_bytes = self._download(sample_url)
        return _write_output(input_image_path, image_bytes)

    def _submit(self, headers: dict[str, str], payload: dict[str, Any]) -> str:
        """POST the job; return the polling URL for its result."""
        last_error: Exception | None = None
        for attempt in range(1, self._config.max_retries + 1):
            try:
                response = requests.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self._config.timeout_s,
                )
                if response.status_code in _RETRYABLE_STATUS:
                    raise ImageEditError(
                        f"BFL submit returned retryable status {response.status_code}"
                    )
                if response.status_code not in (200, 201):
                    raise ImageEditError(
                        f"BFL submit failed (status {response.status_code}): "
                        f"{response.text[:200]}"
                    )
                data = response.json()
                polling_url = data.get("polling_url")
                if not polling_url and data.get("id"):
                    polling_url = _derive_result_url(self._endpoint, data["id"])
                if not polling_url:
                    raise ImageEditError("BFL submit response had no polling_url or id")
                return polling_url
            except (requests.RequestException, ImageEditError, ValueError) as exc:
                last_error = exc
                if attempt >= self._config.max_retries:
                    break
                backoff = 2**attempt
                logger.warning(
                    "flux_fill submit attempt %d/%d failed (%s); retrying in %ds",
                    attempt,
                    self._config.max_retries,
                    type(exc).__name__,
                    backoff,
                )
                time.sleep(backoff)
        raise ImageEditError(f"FLUX Fill submit failed after retries: {last_error}")

    def _poll(self, headers: dict[str, str], polling_url: str) -> str:
        """Poll until the job is Ready; return the signed result URL."""
        deadline = time.monotonic() + self._config.timeout_s
        while time.monotonic() < deadline:
            try:
                response = requests.get(polling_url, headers=headers, timeout=30)
            except requests.RequestException as exc:
                logger.warning("flux_fill poll error (%s); retrying", type(exc).__name__)
                time.sleep(self._poll_interval_s)
                continue

            if response.status_code in _RETRYABLE_STATUS:
                time.sleep(self._poll_interval_s)
                continue
            if response.status_code != 200:
                raise ImageEditError(
                    f"BFL poll failed (status {response.status_code}): {response.text[:200]}"
                )

            data = response.json()
            status = data.get("status", "")
            if status == _STATUS_READY:
                sample = (data.get("result") or {}).get("sample")
                if not sample:
                    raise ImageEditError("BFL reported Ready but result had no sample URL")
                return sample
            if status in _STATUS_FATAL:
                raise ImageEditError(f"FLUX Fill job failed with status {status!r}")
            time.sleep(self._poll_interval_s)

        raise ImageEditError(
            f"FLUX Fill job did not finish within {self._config.timeout_s:.0f}s"
        )

    def _download(self, sample_url: str) -> bytes:
        """Fetch the finished image bytes from the signed result URL."""
        try:
            response = requests.get(sample_url, timeout=self._config.timeout_s)
        except requests.RequestException as exc:
            raise ImageEditError(f"Could not download FLUX Fill result: {exc}") from exc
        if response.status_code != 200 or not response.content:
            raise ImageEditError(
                f"FLUX Fill result download failed (status {response.status_code})"
            )
        return response.content


def _b64_file(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _derive_result_url(endpoint: str, task_id: str) -> str:
    """Build a get_result URL from the submit endpoint when no polling_url is given."""
    base = endpoint.split("/v1/", 1)[0]
    return f"{base}/v1/get_result?id={task_id}"


def _write_output(input_image_path: str, image_bytes: bytes) -> str:
    src = Path(input_image_path)
    output_path = src.with_name(f"{src.stem}_gloved.png")
    output_path.write_bytes(image_bytes)
    return str(output_path)
