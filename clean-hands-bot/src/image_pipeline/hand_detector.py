"""Hand detection built on MediaPipe Hands.

MediaPipe is imported lazily so that the mask/prompt/quality modules stay
testable in environments without it. Detection never invents hands: if all
fallback passes find nothing, an empty list is returned.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Landmark indices follow the MediaPipe Hands topology (21 points per hand).
WRIST = 0
MIDDLE_MCP = 9


@dataclass(frozen=True)
class DetectedHand:
    """One detected hand with pixel-space landmarks."""

    landmarks_px: tuple[tuple[float, float], ...]  # 21 (x, y) points
    handedness: str  # "Left" / "Right" / "Unknown"
    confidence: float


class HandDetectorUnavailable(RuntimeError):
    """Raised when MediaPipe is not installed."""


class HandDetector:
    """MediaPipe Hands wrapper with conservative fallback passes."""

    def __init__(
        self,
        max_num_hands: int = 4,
        min_detection_confidence: float = 0.6,
        min_tracking_confidence: float = 0.6,
    ) -> None:
        try:
            import mediapipe as mp
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise HandDetectorUnavailable(
                "mediapipe is required for hand detection. "
                "Install it with: pip install mediapipe"
            ) from exc

        self._mp_hands = mp.solutions.hands
        self._hands = self._mp_hands.Hands(
            static_image_mode=True,
            max_num_hands=max_num_hands,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )

    def close(self) -> None:
        self._hands.close()

    def __enter__(self) -> "HandDetector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def detect(self, image_bgr: np.ndarray) -> list[DetectedHand]:
        """Run a single detection pass on a BGR image."""
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        results = self._hands.process(rgb)
        if not results.multi_hand_landmarks:
            return []

        h, w = image_bgr.shape[:2]
        hands: list[DetectedHand] = []
        handedness_list = results.multi_handedness or []
        for i, hand_landmarks in enumerate(results.multi_hand_landmarks):
            points = tuple(
                (lm.x * w, lm.y * h) for lm in hand_landmarks.landmark
            )
            label, score = "Unknown", 0.0
            if i < len(handedness_list) and handedness_list[i].classification:
                cls = handedness_list[i].classification[0]
                label, score = cls.label, float(cls.score)
            hands.append(
                DetectedHand(landmarks_px=points, handedness=label, confidence=score)
            )
        return hands

    def detect_with_fallback(self, image_bgr: np.ndarray) -> list[DetectedHand]:
        """Detect hands; on zero hits, retry with resize then contrast passes.

        All fallback landmarks are mapped back to original image coordinates.
        Returns an empty list if every pass finds nothing — never hallucinates.
        """
        hands = self.detect(image_bgr)
        if hands:
            return hands

        h, w = image_bgr.shape[:2]
        # Pass 2: resize. Small images are upscaled (detector likes detail),
        # very large ones downscaled (detector likes context).
        longest = max(h, w)
        scale = 2.0 if longest < 600 else (1024 / longest if longest > 1600 else 1.5)
        resized = cv2.resize(
            image_bgr,
            (max(1, round(w * scale)), max(1, round(h * scale))),
            interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA,
        )
        hands = self.detect(resized)
        if hands:
            logger.info("hand_detector fallback=resize scale=%.2f hands=%d", scale, len(hands))
            return [_rescale_hand(hand, 1.0 / scale) for hand in hands]

        # Pass 3: contrast/brightness normalization (CLAHE on luminance).
        lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        normalized = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        hands = self.detect(normalized)
        if hands:
            logger.info("hand_detector fallback=contrast hands=%d", len(hands))
        return hands


def _rescale_hand(hand: DetectedHand, factor: float) -> DetectedHand:
    return DetectedHand(
        landmarks_px=tuple((x * factor, y * factor) for x, y in hand.landmarks_px),
        handedness=hand.handedness,
        confidence=hand.confidence,
    )
