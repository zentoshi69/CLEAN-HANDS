"""Tests for the hard-mask compositor — the 'nothing else changes' guarantee."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from src.image_pipeline.compositor import composite_in_mask

SIZE = 256


def make_base_image() -> np.ndarray:
    """Smooth gradient image so tests are deterministic and resize-stable."""
    ramp = np.linspace(0, 255, SIZE, dtype=np.float32)
    xx, yy = np.meshgrid(ramp, ramp)
    return np.stack([xx, yy, (xx + yy) / 2], axis=2).astype(np.uint8)


def make_hand_mask(feather: bool = True) -> np.ndarray:
    """Circular 'hand' region in the image center, optionally feathered."""
    mask = np.zeros((SIZE, SIZE), dtype=np.uint8)
    cv2.circle(mask, (SIZE // 2, SIZE // 2), 50, 255, -1)
    if feather:
        mask = cv2.GaussianBlur(mask, (11, 11), 3)
    return mask


class TestCompositeInMask:
    def test_outside_mask_is_byte_identical(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        # Provider "got excited" and brightened the whole frame.
        edited = np.clip(original.astype(np.int16) + 40, 0, 255).astype(np.uint8)

        out = composite_in_mask(original, edited, mask)

        untouched = mask == 0
        assert np.array_equal(out[untouched], original[untouched])

    def test_inside_mask_takes_the_edit(self) -> None:
        original = make_base_image()
        mask = make_hand_mask(feather=False)
        edited = np.full_like(original, 255)

        out = composite_in_mask(original, edited, mask)

        core = mask == 255
        assert np.array_equal(out[core], edited[core])

    def test_unchanged_edit_returns_original_exactly(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        out = composite_in_mask(original, original.copy(), mask)
        assert np.array_equal(out, original)

    def test_feathered_border_blends(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        edited = np.full_like(original, 255)

        out = composite_in_mask(original, edited, mask)

        border = (mask > 0) & (mask < 255)
        assert border.any()
        # Blended pixels sit strictly between the original and the pure edit.
        blended = out[border]
        assert (blended != original[border]).any()
        assert (blended != edited[border]).any()

    def test_resizes_mismatched_edit(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        edited = cv2.resize(
            np.full_like(original, 255), (SIZE * 2, SIZE * 2),
            interpolation=cv2.INTER_NEAREST,
        )
        out = composite_in_mask(original, edited, mask)
        assert out.shape == original.shape
        assert np.array_equal(out[mask == 0], original[mask == 0])

    def test_mask_shape_mismatch_raises(self) -> None:
        original = make_base_image()
        edited = original.copy()
        bad_mask = np.zeros((SIZE // 2, SIZE), dtype=np.uint8)
        with pytest.raises(ValueError, match="Mask shape"):
            composite_in_mask(original, edited, bad_mask)
