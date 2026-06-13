"""Tests for hand mask construction."""

from __future__ import annotations

import numpy as np
import pytest

from src.image_pipeline.mask_builder import (
    MaskParams,
    build_hand_mask,
    feather_mask,
    hand_scale_px,
    mask_coverage,
    merge_masks,
    params_for_image,
    validate_coverage,
)

IMAGE_SIZE = (512, 512)  # (width, height)


def synthetic_hand_landmarks() -> tuple[tuple[float, float], ...]:
    """A plausible upright right hand in a 512x512 frame (MediaPipe order)."""
    return (
        (256, 400),                                        # 0 wrist
        (220, 380), (200, 360), (185, 340), (175, 325),    # thumb
        (230, 300), (228, 270), (226, 245), (224, 225),    # index
        (256, 295), (256, 262), (256, 235), (256, 210),    # middle
        (282, 300), (284, 270), (286, 245), (288, 228),    # ring
        (305, 310), (310, 285), (313, 265), (316, 250),    # pinky
    )


class TestBuildHandMask:
    def test_basic_properties(self) -> None:
        mask = build_hand_mask(synthetic_hand_landmarks(), IMAGE_SIZE)
        assert mask.shape == (512, 512)
        assert mask.dtype == np.uint8
        assert mask.max() == 255

    def test_covers_all_landmarks(self) -> None:
        mask = build_hand_mask(synthetic_hand_landmarks(), IMAGE_SIZE)
        for x, y in synthetic_hand_landmarks():
            assert mask[int(y), int(x)] > 100, f"landmark ({x},{y}) not covered"

    def test_does_not_touch_far_corners(self) -> None:
        mask = build_hand_mask(synthetic_hand_landmarks(), IMAGE_SIZE)
        assert mask[:60, :60].max() == 0
        assert mask[:60, -60:].max() == 0

    def test_coverage_is_reasonable(self) -> None:
        mask = build_hand_mask(synthetic_hand_landmarks(), IMAGE_SIZE)
        coverage = mask_coverage(mask)
        assert 0.005 < coverage < 0.30

    def test_hull_style_covers_all_landmarks(self) -> None:
        landmarks = synthetic_hand_landmarks()
        hull = build_hand_mask(landmarks, IMAGE_SIZE, style="hull")
        for x, y in landmarks:
            assert hull[int(y), int(x)] > 100, f"landmark ({x},{y}) not covered"
        assert 0.005 < mask_coverage(hull) < 0.30

    def test_wrist_extension_adds_area(self) -> None:
        landmarks = synthetic_hand_landmarks()
        with_wrist = build_hand_mask(landmarks, IMAGE_SIZE, include_wrist=True)
        without_wrist = build_hand_mask(landmarks, IMAGE_SIZE, include_wrist=False)
        assert mask_coverage(with_wrist) > mask_coverage(without_wrist)
        # The cuff extends below the wrist landmark (y > 400).
        assert with_wrist[430:460, 240:270].max() > 0

    def test_rejects_wrong_landmark_count(self) -> None:
        with pytest.raises(ValueError, match="21"):
            build_hand_mask(((1.0, 1.0),) * 5, IMAGE_SIZE)

    def test_rejects_unknown_style(self) -> None:
        with pytest.raises(ValueError, match="style"):
            build_hand_mask(synthetic_hand_landmarks(), IMAGE_SIZE, style="blob")


class TestFeather:
    def test_feather_produces_gradient_edges(self) -> None:
        hard = np.zeros((100, 100), dtype=np.uint8)
        hard[40:60, 40:60] = 255
        soft = feather_mask(hard, feather_px=6)
        intermediate = (soft > 0) & (soft < 255)
        assert intermediate.any()

    def test_feather_zero_is_noop(self) -> None:
        mask = np.full((10, 10), 255, dtype=np.uint8)
        assert np.array_equal(feather_mask(mask, 0), mask)


class TestMergeMasks:
    def test_merge_is_pixelwise_max(self) -> None:
        a = np.zeros((10, 10), dtype=np.uint8)
        b = np.zeros((10, 10), dtype=np.uint8)
        a[2, 2] = 200
        b[2, 2] = 100
        b[5, 5] = 255
        merged = merge_masks([a, b])
        assert merged[2, 2] == 200
        assert merged[5, 5] == 255

    def test_merge_rejects_empty_list(self) -> None:
        with pytest.raises(ValueError):
            merge_masks([])

    def test_merge_rejects_shape_mismatch(self) -> None:
        with pytest.raises(ValueError):
            merge_masks([np.zeros((4, 4), np.uint8), np.zeros((5, 5), np.uint8)])


class TestCoverageValidation:
    def test_normal_mask_passes(self) -> None:
        mask = build_hand_mask(synthetic_hand_landmarks(), IMAGE_SIZE)
        ok, error = validate_coverage(mask)
        assert ok and error is None

    def test_tiny_mask_fails_safely(self) -> None:
        mask = np.zeros((512, 512), dtype=np.uint8)
        mask[0, 0] = 255
        ok, error = validate_coverage(mask)
        assert not ok and "minimum" in (error or "")

    def test_huge_mask_fails_safely(self) -> None:
        mask = np.full((512, 512), 255, dtype=np.uint8)
        ok, error = validate_coverage(mask)
        assert not ok and "maximum" in (error or "")


class TestParams:
    def test_size_buckets(self) -> None:
        assert params_for_image(500, 400) == MaskParams(padding_px=11, feather_px=4)
        assert params_for_image(1000, 800) == MaskParams(padding_px=20, feather_px=8)
        assert params_for_image(2000, 1500) == MaskParams(padding_px=32, feather_px=12)

    def test_hand_scale(self) -> None:
        scale = hand_scale_px(synthetic_hand_landmarks())
        assert 100 < scale < 110  # wrist (256,400) to middle MCP (256,295)
