"""Build precise inpainting masks around detected hands.

The default "skeleton" style draws thick strokes along the hand bone
topology plus a filled palm polygon. Compared to a plain convex hull this
hugs the fingers and leaves a hole where a held object (bottle, cash,
phone) sits inside the grip — which is exactly what we want: glove the
hand, never repaint the object.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# MediaPipe Hands 21-landmark topology.
WRIST = 0
MIDDLE_MCP = 9
INDEX_MCP = 5
PINKY_MCP = 17
FINGERTIPS = (4, 8, 12, 16, 20)
PALM_POLYGON = (0, 1, 2, 5, 9, 13, 17)
HAND_CONNECTIONS: tuple[tuple[int, int], ...] = (
    (1, 2), (2, 3), (3, 4),          # thumb
    (5, 6), (6, 7), (7, 8),          # index
    (9, 10), (10, 11), (11, 12),     # middle
    (13, 14), (14, 15), (15, 16),    # ring
    (17, 18), (18, 19), (19, 20),    # pinky
    (0, 1), (0, 5), (5, 9), (9, 13), (13, 17), (0, 17),  # palm frame
)


@dataclass(frozen=True)
class MaskParams:
    """Resolution-dependent mask drawing parameters (pixels)."""

    padding_px: int
    feather_px: int


def params_for_image(width: int, height: int) -> MaskParams:
    """Pick padding/feather per the spec's small/medium/large buckets."""
    longest = max(width, height)
    if longest < 640:
        return MaskParams(padding_px=11, feather_px=4)
    if longest < 1280:
        return MaskParams(padding_px=20, feather_px=8)
    return MaskParams(padding_px=32, feather_px=12)


def hand_scale_px(landmarks_px: tuple[tuple[float, float], ...]) -> float:
    """Characteristic hand size: wrist to middle-finger MCP distance."""
    wx, wy = landmarks_px[WRIST]
    mx, my = landmarks_px[MIDDLE_MCP]
    return float(np.hypot(mx - wx, my - wy))


def build_hand_mask(
    landmarks_px: tuple[tuple[float, float], ...],
    image_size: tuple[int, int],
    params: MaskParams | None = None,
    *,
    style: str = "skeleton",
    include_wrist: bool = True,
) -> np.ndarray:
    """Build a feathered uint8 mask (HxW, 0-255) for one hand.

    Args:
        landmarks_px: 21 (x, y) pixel coordinates in MediaPipe order.
        image_size: (width, height) of the target image.
        params: drawing parameters; derived from image size when omitted.
        style: "skeleton" (default, avoids held objects) or "hull".
        include_wrist: extend the mask a short way down the wrist so the
            glove cuff lands naturally.
    """
    if len(landmarks_px) != 21:
        raise ValueError(f"Expected 21 hand landmarks, got {len(landmarks_px)}")
    if style not in ("skeleton", "hull"):
        raise ValueError(f"Unknown mask style {style!r}")

    width, height = image_size
    if params is None:
        params = params_for_image(width, height)

    mask = np.zeros((height, width), dtype=np.uint8)
    pts = np.array(landmarks_px, dtype=np.float32)
    scale = max(hand_scale_px(landmarks_px), 4.0)
    # Finger stroke thickness scales with the hand, clamped to sane bounds.
    thickness = int(np.clip(scale * 0.30, 3, 60))

    if style == "hull":
        hull = cv2.convexHull(pts.astype(np.int32))
        cv2.fillConvexPoly(mask, hull, 255)
    else:
        for a, b in HAND_CONNECTIONS:
            pa = tuple(np.round(pts[a]).astype(int))
            pb = tuple(np.round(pts[b]).astype(int))
            cv2.line(mask, pa, pb, 255, thickness=thickness, lineType=cv2.LINE_AA)
        palm = pts[list(PALM_POLYGON)].astype(np.int32)
        cv2.fillConvexPoly(mask, cv2.convexHull(palm), 255)
        for tip in FINGERTIPS:
            center = tuple(np.round(pts[tip]).astype(int))
            cv2.circle(mask, center, max(2, thickness * 2 // 3), 255, -1)

    if include_wrist:
        _extend_wrist(mask, pts, scale)

    # Padding via gentle dilation — never aggressive.
    kernel_size = max(3, params.padding_px | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    mask = cv2.dilate(mask, kernel, iterations=1)

    return feather_mask(mask, params.feather_px)


def _extend_wrist(
    mask: np.ndarray, pts: np.ndarray, scale: float
) -> None:
    """Extend the mask from the wrist landmark away from the palm."""
    wrist = pts[WRIST]
    direction = wrist - pts[MIDDLE_MCP]
    norm = float(np.linalg.norm(direction))
    if norm < 1e-3:
        return
    direction /= norm
    cuff_end = wrist + direction * scale * 0.35
    palm_width = float(np.linalg.norm(pts[INDEX_MCP] - pts[PINKY_MCP]))
    thickness = int(np.clip(palm_width * 0.9, 4, 120))
    cv2.line(
        mask,
        tuple(np.round(wrist).astype(int)),
        tuple(np.round(cuff_end).astype(int)),
        255,
        thickness=thickness,
        lineType=cv2.LINE_AA,
    )


def feather_mask(mask: np.ndarray, feather_px: int) -> np.ndarray:
    """Soften mask edges with a Gaussian falloff."""
    if feather_px <= 0:
        return mask
    ksize = feather_px * 2 + 1
    return cv2.GaussianBlur(mask, (ksize, ksize), feather_px / 2.0)


def merge_masks(masks: list[np.ndarray]) -> np.ndarray:
    """Combine per-hand masks into one (pixel-wise maximum)."""
    if not masks:
        raise ValueError("merge_masks requires at least one mask")
    merged = masks[0]
    for mask in masks[1:]:
        if mask.shape != merged.shape:
            raise ValueError("All masks must share the same shape")
        merged = np.maximum(merged, mask)
    return merged


def mask_coverage(mask: np.ndarray) -> float:
    """Fraction of the image covered by the mask (0.0 - 1.0)."""
    return float(np.count_nonzero(mask > 32)) / mask.size


def validate_coverage(
    mask: np.ndarray,
    min_fraction: float = 0.0005,
    max_fraction: float = 0.45,
) -> tuple[bool, str | None]:
    """Fail safely when the mask is suspiciously tiny or huge."""
    coverage = mask_coverage(mask)
    if coverage < min_fraction:
        return False, (
            f"Mask coverage {coverage:.4%} is below the safe minimum — "
            "hands are too small or detection was unreliable."
        )
    if coverage > max_fraction:
        return False, (
            f"Mask coverage {coverage:.1%} is above the safe maximum — "
            "refusing to repaint that much of the image."
        )
    return True, None
