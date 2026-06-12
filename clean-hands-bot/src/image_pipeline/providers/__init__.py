"""Provider registry: resolve IMAGE_PROVIDER from settings to an adapter."""

from __future__ import annotations

from src.image_pipeline.providers.base import (
    FutureProvider,
    ImageEditError,
    ImageEditProvider,
    ProviderConfig,
)
from src.image_pipeline.providers.generic_http import GenericHTTPProvider
from src.image_pipeline.providers.mock import MockProvider
from src.utils.config import Settings

__all__ = [
    "ImageEditError",
    "ImageEditProvider",
    "ProviderConfig",
    "create_provider",
]


def create_provider(settings: Settings) -> ImageEditProvider:
    """Instantiate the provider named by IMAGE_PROVIDER."""
    name = settings.image_provider
    if name == "mock":
        return MockProvider()
    if name == "generic_http":
        return GenericHTTPProvider(
            ProviderConfig(
                api_key=settings.image_provider_api_key,
                endpoint=settings.image_provider_endpoint,
            )
        )
    if name == "future":
        return FutureProvider()
    raise ImageEditError(
        f"Unknown IMAGE_PROVIDER {name!r}. Supported: mock, generic_http, future."
    )
