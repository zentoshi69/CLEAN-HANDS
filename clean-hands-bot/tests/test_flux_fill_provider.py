"""Tests for the FLUX.1 Fill [pro] provider (BFL submit → poll → download)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.image_pipeline.providers import flux_fill
from src.image_pipeline.providers.base import ImageEditError, ProviderConfig
from src.image_pipeline.providers.flux_fill import FluxFillProvider


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def images(tmp_path: Path) -> tuple[str, str]:
    img = tmp_path / "in.png"
    mask = tmp_path / "mask.png"
    img.write_bytes(b"\x89PNG-image-bytes")
    mask.write_bytes(b"\x89PNG-mask-bytes")
    return str(img), str(mask)


def _config(**extra) -> ProviderConfig:
    return ProviderConfig(api_key="bfl-test-key", extra={"poll_interval_s": 0, **extra})


class TestConstruction:
    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(ImageEditError, match="IMAGE_PROVIDER_API_KEY"):
            FluxFillProvider(ProviderConfig(api_key=""))

    def test_defaults_to_bfl_endpoint(self) -> None:
        provider = FluxFillProvider(ProviderConfig(api_key="k"))
        assert provider._endpoint == flux_fill.DEFAULT_ENDPOINT


class TestEditImage:
    def test_happy_path_submits_polls_downloads(self, images, monkeypatch) -> None:
        img_path, mask_path = images
        posts: list = []

        def fake_post(url, headers, json, timeout):
            posts.append((url, headers, json))
            return FakeResponse(json_data={"id": "abc", "polling_url": "https://poll/abc"})

        gets = iter(
            [
                FakeResponse(json_data={"status": "Pending"}),
                FakeResponse(json_data={"status": "Ready", "result": {"sample": "https://img/abc.png"}}),
                FakeResponse(content=b"GLOVED-IMAGE-BYTES"),
            ]
        )
        monkeypatch.setattr(flux_fill.requests, "post", fake_post)
        monkeypatch.setattr(flux_fill.requests, "get", lambda *a, **k: next(gets))

        provider = FluxFillProvider(_config())
        out = provider.edit_image(img_path, mask_path, "gloves", "neg", config={"seed": 7})

        assert Path(out).read_bytes() == b"GLOVED-IMAGE-BYTES"
        assert Path(out).name == "in_gloved.png"
        # Auth header, base64 payload, and the seed must reach the API.
        url, headers, payload = posts[0]
        assert headers["x-key"] == "bfl-test-key"
        assert payload["seed"] == 7
        assert payload["image"] and payload["mask"]
        assert payload["output_format"] == "png"

    def test_fatal_status_raises(self, images, monkeypatch) -> None:
        img_path, mask_path = images
        monkeypatch.setattr(
            flux_fill.requests, "post",
            lambda *a, **k: FakeResponse(json_data={"polling_url": "https://poll/x"}),
        )
        monkeypatch.setattr(
            flux_fill.requests, "get",
            lambda *a, **k: FakeResponse(json_data={"status": "Request Moderated"}),
        )
        provider = FluxFillProvider(_config())
        with pytest.raises(ImageEditError, match="Request Moderated"):
            provider.edit_image(img_path, mask_path, "gloves", "neg")

    def test_submit_error_status_raises(self, images, monkeypatch) -> None:
        img_path, mask_path = images
        monkeypatch.setattr(
            flux_fill.requests, "post",
            lambda *a, **k: FakeResponse(status_code=401, text="bad key"),
        )
        provider = FluxFillProvider(ProviderConfig(api_key="k", max_retries=1, extra={"poll_interval_s": 0}))
        with pytest.raises(ImageEditError, match="status 401"):
            provider.edit_image(img_path, mask_path, "gloves", "neg")


def test_derive_result_url() -> None:
    url = flux_fill._derive_result_url("https://api.bfl.ai/v1/flux-pro-1.0-fill", "task123")
    assert url == "https://api.bfl.ai/v1/get_result?id=task123"
