"""End-to-end glove pipeline: load → detect → mask → edit → quality gate.

The pipeline is a glove-inpainting sniper. When anything is uncertain it
fails safely and preserves the original image instead of returning soup.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from src.image_pipeline import compositor, mask_builder, quality_gate
from src.image_pipeline.prompt_builder import build_prompt, stricter
from src.image_pipeline.providers import ImageEditError, ImageEditProvider, create_provider
from src.utils import image_io
from src.utils.config import Settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Failure reason codes surfaced in ProcessResult.debug["reason"].
REASON_NO_HANDS = "no_hands"
REASON_BAD_MASK = "bad_mask"
REASON_PROVIDER_ERROR = "provider_error"
REASON_QUALITY_REJECTED = "quality_rejected"
REASON_IO_ERROR = "io_error"


@dataclass
class ProcessResult:
    """Outcome of one glove-edit attempt."""

    success: bool
    output_path: str | None = None
    mask_path: str | None = None
    detected_hands: int = 0
    confidence: float = 0.0
    error_message: str | None = None
    debug: dict = field(default_factory=dict)


def process_image_for_gloves(
    input_path: str,
    mode: str = "balanced",
    *,
    settings: Settings | None = None,
    provider: ImageEditProvider | None = None,
    detector: object | None = None,
) -> ProcessResult:
    """Add medical light-blue gloves to every visible hand in the image.

    Args:
        input_path: path to the source image on disk.
        mode: prompt mode — "balanced", "soft", "hard", or "strict".
        settings: runtime settings (built from env when omitted).
        provider: image edit backend (resolved from settings when omitted).
        detector: hand detector; injectable for tests. Must expose
            ``detect_with_fallback(image_bgr) -> list[DetectedHand]``.
    """
    started = time.monotonic()
    settings = settings or Settings.from_env()
    debug: dict = {"mode": mode}

    # 1-3. Load, EXIF-normalize, bounded resize.
    try:
        pil_image = image_io.load_image_rgb(input_path)
        pil_image = image_io.resize_max_side(pil_image, settings.output_max_side)
        work_dir = Path(input_path).parent
        normalized_path = work_dir / f"{Path(input_path).stem}_normalized.png"
        image_io.save_image_rgb(pil_image, normalized_path)
        original_bgr = image_io.pil_to_bgr(pil_image)
    except image_io.ImageIOError as exc:
        return _failure(REASON_IO_ERROR, str(exc), debug, started)

    height, width = original_bgr.shape[:2]
    debug["image_size"] = (width, height)

    # 4. Detect hands (MediaPipe + conservative fallbacks).
    if detector is None:
        from src.image_pipeline.hand_detector import HandDetector

        detector = HandDetector(
            max_num_hands=settings.hand_max_num_hands,
            min_detection_confidence=settings.hand_min_detection_confidence,
            min_tracking_confidence=settings.hand_min_detection_confidence,
        )
    hands = detector.detect_with_fallback(original_bgr)
    if not hands:
        return _failure(
            REASON_NO_HANDS,
            "No hands detected in the image.",
            debug,
            started,
        )

    confidence = float(np.mean([hand.confidence for hand in hands]))
    debug["hand_confidences"] = [round(h.confidence, 3) for h in hands]

    # 5-6. Per-hand masks merged into one.
    params = mask_builder.params_for_image(width, height)
    masks = [
        mask_builder.build_hand_mask(hand.landmarks_px, (width, height), params)
        for hand in hands
    ]
    merged_mask = mask_builder.merge_masks(masks)

    # 7. Coverage sanity check — fail safely rather than wreck the image.
    coverage_ok, coverage_error = mask_builder.validate_coverage(merged_mask)
    debug["mask_coverage"] = round(mask_builder.mask_coverage(merged_mask), 5)
    if not coverage_ok:
        result = _failure(REASON_BAD_MASK, coverage_error or "Bad mask", debug, started)
        result.detected_hands = len(hands)
        result.confidence = confidence
        return result

    mask_path = work_dir / f"{Path(input_path).stem}_mask.png"
    image_io.save_mask(merged_mask, mask_path)

    # 8-9. Edit via provider, verify, retry once with the strict prompt.
    provider = provider or create_provider(settings)
    bundle = build_prompt(mode)
    report: quality_gate.QualityReport | None = None
    output_path: str | None = None

    # A fixed seed (when the provider supports one) makes the same image +
    # prompt reproduce the same gloves run to run.
    edit_config: dict = {"mode": bundle.mode}
    if settings.image_provider_seed is not None:
        edit_config["seed"] = settings.image_provider_seed

    for attempt, current in enumerate((bundle, stricter(bundle)), start=1):
        try:
            candidate = provider.edit_image(
                str(normalized_path),
                str(mask_path),
                current.prompt,
                current.negative_prompt,
                config={**edit_config, "mode": current.mode},
            )
        except ImageEditError as exc:
            result = _failure(REASON_PROVIDER_ERROR, str(exc), debug, started)
            result.detected_hands = len(hands)
            result.confidence = confidence
            result.mask_path = str(mask_path)
            return result

        # Hard-mask composite: discard any provider drift outside the hand mask
        # so the result is the EXACT original everywhere but the gloves, then
        # persist it so the delivered file matches what we verify.
        edited_bgr = compositor.composite_in_mask(
            original_bgr, image_io.load_bgr(candidate), merged_mask
        )
        image_io.save_bgr(edited_bgr, candidate)
        report = quality_gate.evaluate(original_bgr, edited_bgr, merged_mask)
        debug[f"quality_attempt_{attempt}"] = {
            "passed": report.passed,
            "reasons": report.reasons,
            "outside_delta": round(report.outside_mask_delta, 3),
            "inside_delta": round(report.inside_mask_delta, 3),
            "outside_ssim": round(report.outside_ssim, 4),
        }
        if report.passed:
            output_path = candidate
            break
        logger.info(
            "quality gate rejected attempt %d (%s); %s",
            attempt,
            ",".join(report.reasons),
            "retrying with strict prompt" if attempt == 1 else "giving up",
        )

    # 10. Final verdict + debug artifacts.
    if output_path is None:
        result = _failure(
            REASON_QUALITY_REJECTED,
            "Edited image failed the quality gate twice: "
            + ",".join(report.reasons if report else []),
            debug,
            started,
        )
        result.detected_hands = len(hands)
        result.confidence = confidence
        result.mask_path = str(mask_path)
        return result

    heatmap = quality_gate.make_diff_heatmap(original_bgr, image_io.load_bgr(output_path))
    heatmap_path = work_dir / f"{Path(input_path).stem}_diff.png"
    image_io.save_mask(heatmap, heatmap_path)
    debug["heatmap_path"] = str(heatmap_path)
    debug["duration_s"] = round(time.monotonic() - started, 2)

    return ProcessResult(
        success=True,
        output_path=output_path,
        mask_path=str(mask_path),
        detected_hands=len(hands),
        confidence=confidence,
        debug=debug,
    )


def _failure(reason: str, message: str, debug: dict, started: float) -> ProcessResult:
    debug = {**debug, "reason": reason, "duration_s": round(time.monotonic() - started, 2)}
    return ProcessResult(success=False, error_message=message, debug=debug)


def _main() -> None:  # pragma: no cover - manual testing CLI
    """CLI for local testing: python -m src.image_pipeline.pipeline IMG [--mode m]"""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Glove an image from the command line.")
    parser.add_argument("image", help="Path to the input image")
    parser.add_argument("--mode", default="balanced", choices=["balanced", "soft", "hard", "strict"])
    args = parser.parse_args()

    result = process_image_for_gloves(args.image, args.mode)
    print(json.dumps(result.__dict__, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    _main()
