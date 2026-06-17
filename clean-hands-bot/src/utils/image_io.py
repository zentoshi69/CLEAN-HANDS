"""Image loading, normalization, and saving helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps


class ImageIOError(RuntimeError):
    """Raised when an image cannot be read or written."""


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_image_rgb(path: str | Path) -> Image.Image:
    """Load an image, applying EXIF orientation, as RGB."""
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB")
    except Exception as exc:  # Pillow raises many concrete types
        raise ImageIOError(f"Cannot read image {Path(path).name}: {exc}") from exc


def pil_to_bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(img, dtype=np.uint8), cv2.COLOR_RGB2BGR)


def bgr_to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


def resize_max_side(img: Image.Image, max_side: int) -> Image.Image:
    """Downscale so the longest side is <= max_side. Never upscales."""
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    scale = max_side / longest
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return img.resize(new_size, Image.LANCZOS)


def save_image_rgb(img: Image.Image, path: str | Path, quality: int = 92) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    suffix = path.suffix.lower()
    try:
        if suffix in (".jpg", ".jpeg"):
            img.save(path, quality=quality, optimize=True)
        else:
            img.save(path)
    except Exception as exc:
        raise ImageIOError(f"Cannot write image {path.name}: {exc}") from exc
    return path


def save_mask(mask: np.ndarray, path: str | Path) -> Path:
    """Save a single-channel uint8 mask as PNG."""
    path = Path(path)
    ensure_dir(path.parent)
    if not cv2.imwrite(str(path), mask):
        raise ImageIOError(f"Cannot write mask {path.name}")
    return path


def save_bgr(arr: np.ndarray, path: str | Path) -> Path:
    """Save a BGR uint8 array as an image file."""
    path = Path(path)
    ensure_dir(path.parent)
    if not cv2.imwrite(str(path), arr):
        raise ImageIOError(f"Cannot write image {path.name}")
    return path


def load_bgr(path: str | Path) -> np.ndarray:
    """Load an image straight to a BGR numpy array (EXIF normalized)."""
    return pil_to_bgr(load_image_rgb(path))
