"""Post-edit quality gate.

Rejects provider output that changed anything beyond the gloved hands:
dimension drift, outside-mask pixel changes, low outside-mask SSIM, or a
missing glove effect inside the mask. Face, background, and held-object
preservation are all enforced by the outside-mask checks because the mask
covers hand skin only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

REASON_DIMENSIONS = "dimensions_changed"
REASON_OUTSIDE_DELTA = "outside_mask_changed"
REASON_OUTSIDE_SSIM = "outside_mask_ssim_low"
REASON_NO_GLOVE = "no_glove_effect"


@dataclass(frozen=True)
class Thresholds:
    """Tunable acceptance limits (pixel values are on a 0-255 scale)."""

    max_outside_delta: float = 6.0   # mean abs diff allowed outside the mask
    min_outside_ssim: float = 0.90   # structural similarity outside the mask
    min_inside_delta: float = 3.0    # required mean change inside the mask
    max_aspect_drift: float = 0.01   # allowed aspect-ratio change


@dataclass
class QualityReport:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    outside_mask_delta: float = 0.0
    inside_mask_delta: float = 0.0
    outside_ssim: float = 1.0
    debug: dict = field(default_factory=dict)


def evaluate(
    original_bgr: np.ndarray,
    edited_bgr: np.ndarray,
    mask: np.ndarray,
    thresholds: Thresholds | None = None,
) -> QualityReport:
    """Compare original and edited images against the hand mask."""
    thresholds = thresholds or Thresholds()
    reasons: list[str] = []

    oh, ow = original_bgr.shape[:2]
    eh, ew = edited_bgr.shape[:2]
    if (oh, ow) != (eh, ew):
        original_aspect = ow / oh
        edited_aspect = ew / eh
        drift = abs(edited_aspect - original_aspect) / original_aspect
        if drift > thresholds.max_aspect_drift:
            return QualityReport(
                passed=False,
                reasons=[REASON_DIMENSIONS],
                debug={"original_size": (ow, oh), "edited_size": (ew, eh)},
            )
        # Same aspect: providers sometimes rescale; normalize and continue.
        edited_bgr = cv2.resize(edited_bgr, (ow, oh), interpolation=cv2.INTER_AREA)

    if mask.shape != (oh, ow):
        raise ValueError("Mask shape must match the original image")

    # Strictly-outside region: grow the mask first so the feathered border
    # is not unfairly counted as an outside change.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    grown = cv2.dilate(mask, kernel, iterations=1)
    outside = grown < 8
    inside = mask > 128

    diff = np.abs(
        original_bgr.astype(np.int16) - edited_bgr.astype(np.int16)
    ).mean(axis=2)

    outside_delta = float(diff[outside].mean()) if outside.any() else 0.0
    inside_delta = float(diff[inside].mean()) if inside.any() else 0.0

    gray_orig = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2GRAY).astype(np.float64)
    gray_edit = cv2.cvtColor(edited_bgr, cv2.COLOR_BGR2GRAY).astype(np.float64)
    ssim_map = _ssim_map(gray_orig, gray_edit)
    outside_ssim = float(ssim_map[outside].mean()) if outside.any() else 1.0

    if outside_delta > thresholds.max_outside_delta:
        reasons.append(REASON_OUTSIDE_DELTA)
    if outside_ssim < thresholds.min_outside_ssim:
        reasons.append(REASON_OUTSIDE_SSIM)
    if inside.any() and inside_delta < thresholds.min_inside_delta:
        reasons.append(REASON_NO_GLOVE)

    return QualityReport(
        passed=not reasons,
        reasons=reasons,
        outside_mask_delta=outside_delta,
        inside_mask_delta=inside_delta,
        outside_ssim=outside_ssim,
        debug={
            "outside_pixels": int(outside.sum()),
            "inside_pixels": int(inside.sum()),
            "thresholds": thresholds.__dict__,
        },
    )


def make_diff_heatmap(original_bgr: np.ndarray, edited_bgr: np.ndarray) -> np.ndarray:
    """Render |original - edited| as a JET heatmap for /debug output."""
    oh, ow = original_bgr.shape[:2]
    if edited_bgr.shape[:2] != (oh, ow):
        edited_bgr = cv2.resize(edited_bgr, (ow, oh), interpolation=cv2.INTER_AREA)
    diff = np.abs(
        original_bgr.astype(np.int16) - edited_bgr.astype(np.int16)
    ).mean(axis=2)
    normalized = np.clip(diff * (255.0 / max(diff.max(), 1.0)), 0, 255).astype(np.uint8)
    return cv2.applyColorMap(normalized, cv2.COLORMAP_JET)


def _ssim_map(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Per-pixel SSIM map for two grayscale float64 images (0-255 range)."""
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    blur = lambda img: cv2.GaussianBlur(img, (11, 11), 1.5)  # noqa: E731

    mu_a, mu_b = blur(a), blur(b)
    mu_a2, mu_b2, mu_ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b
    sigma_a2 = blur(a * a) - mu_a2
    sigma_b2 = blur(b * b) - mu_b2
    sigma_ab = blur(a * b) - mu_ab

    numerator = (2 * mu_ab + c1) * (2 * sigma_ab + c2)
    denominator = (mu_a2 + mu_b2 + c1) * (sigma_a2 + sigma_b2 + c2)
    return numerator / np.maximum(denominator, 1e-12)
