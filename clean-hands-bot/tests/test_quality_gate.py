"""Tests for the post-edit quality gate."""

from __future__ import annotations

import cv2
import numpy as np

from src.image_pipeline.quality_gate import (
    REASON_DIMENSIONS,
    REASON_NO_GLOVE,
    REASON_OUTSIDE_DELTA,
    evaluate,
    make_diff_heatmap,
)

SIZE = 256


def make_base_image() -> np.ndarray:
    """Smooth gradient image so tests are deterministic and resize-stable."""
    ramp = np.linspace(0, 255, SIZE, dtype=np.float32)
    xx, yy = np.meshgrid(ramp, ramp)
    return np.stack([xx, yy, (xx + yy) / 2], axis=2).astype(np.uint8)


def make_hand_mask() -> np.ndarray:
    """Feathered circular 'hand' region in the image center."""
    mask = np.zeros((SIZE, SIZE), dtype=np.uint8)
    cv2.circle(mask, (SIZE // 2, SIZE // 2), 50, 255, -1)
    return cv2.GaussianBlur(mask, (11, 11), 3)


def apply_fake_glove(image: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Shift the masked region toward blue, leave the rest untouched."""
    edited = image.copy()
    inside = mask > 128
    blue = edited[:, :, 0].astype(np.int16)
    blue[inside] = np.clip(blue[inside] + 60, 0, 255)
    edited[:, :, 0] = blue.astype(np.uint8)
    return edited


class TestEvaluate:
    def test_clean_glove_edit_passes(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        edited = apply_fake_glove(original, mask)
        report = evaluate(original, edited, mask)
        assert report.passed, report.reasons
        assert report.outside_mask_delta < 1.0
        assert report.inside_mask_delta > 3.0

    def test_unchanged_image_fails_no_glove_check(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        report = evaluate(original, original.copy(), mask)
        assert not report.passed
        assert REASON_NO_GLOVE in report.reasons

    def test_outside_mask_change_is_rejected(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        # Provider "got excited" and brightened the whole image.
        edited = np.clip(original.astype(np.int16) + 20, 0, 255).astype(np.uint8)
        report = evaluate(original, edited, mask)
        assert not report.passed
        assert REASON_OUTSIDE_DELTA in report.reasons

    def test_aspect_ratio_change_is_rejected(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        squished = cv2.resize(original, (SIZE, SIZE // 2))
        report = evaluate(original, squished, mask)
        assert not report.passed
        assert report.reasons == [REASON_DIMENSIONS]

    def test_same_aspect_rescale_is_normalized(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        edited = apply_fake_glove(original, mask)
        upscaled = cv2.resize(edited, (SIZE * 2, SIZE * 2), interpolation=cv2.INTER_LINEAR)
        report = evaluate(original, upscaled, mask)
        assert report.passed, report.reasons

    def test_report_carries_metrics(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        report = evaluate(original, apply_fake_glove(original, mask), mask)
        assert 0.0 <= report.outside_ssim <= 1.0
        assert report.debug["inside_pixels"] > 0
        assert report.debug["outside_pixels"] > 0


class TestDiffHeatmap:
    def test_heatmap_shape_and_type(self) -> None:
        original = make_base_image()
        edited = apply_fake_glove(original, make_hand_mask())
        heatmap = make_diff_heatmap(original, edited)
        assert heatmap.shape == original.shape
        assert heatmap.dtype == np.uint8

    def test_heatmap_highlights_changed_region(self) -> None:
        original = make_base_image()
        mask = make_hand_mask()
        edited = apply_fake_glove(original, mask)
        heatmap = make_diff_heatmap(original, edited)
        center = heatmap[SIZE // 2 - 10 : SIZE // 2 + 10, SIZE // 2 - 10 : SIZE // 2 + 10]
        corner = heatmap[:20, :20]
        # JET colormap: changed pixels are hot (red channel), unchanged cold.
        assert center[:, :, 2].mean() > corner[:, :, 2].mean()
