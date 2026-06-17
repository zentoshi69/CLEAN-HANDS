"""Hard-mask compositing: keep every pixel outside the hand mask identical.

Generative edit providers re-render and re-encode the *whole* frame, so even a
faithful inpaint nudges the face, background, and clothing by a few levels
everywhere — the mask the provider receives is a hint, not a guarantee. This
module throws that drift away: only the masked hand region is taken from the
provider's edit, and everywhere else is the pristine original. The result is
the EXACT same image, just with gloves on the hands — nothing else.

The feathered mask edge gives a seamless glove cuff instead of a hard cutout
line, while pixels fully outside the mask stay byte-identical to the source.
"""

from __future__ import annotations

import cv2
import numpy as np


def composite_in_mask(
    original_bgr: np.ndarray,
    edited_bgr: np.ndarray,
    mask: np.ndarray,
) -> np.ndarray:
    """Blend ``edited_bgr`` over ``original_bgr`` only where ``mask`` is set.

    Args:
        original_bgr: pristine source image (HxWx3, uint8).
        edited_bgr: provider output; resized to the original if it differs.
        mask: single-channel uint8 (0-255). 0 keeps the original exactly,
            255 takes the edit fully, in-between feathers the two.

    Returns:
        A uint8 BGR image that is byte-identical to the original wherever the
        mask is zero, and the gloved edit wherever the mask is set.
    """
    oh, ow = original_bgr.shape[:2]
    if edited_bgr.shape[:2] != (oh, ow):
        edited_bgr = cv2.resize(edited_bgr, (ow, oh), interpolation=cv2.INTER_AREA)
    if mask.shape != (oh, ow):
        raise ValueError("Mask shape must match the original image")

    alpha = (mask.astype(np.float32) / 255.0)[:, :, None]
    blended = (
        original_bgr.astype(np.float32) * (1.0 - alpha)
        + edited_bgr.astype(np.float32) * alpha
    )
    out = np.rint(blended).astype(np.uint8)

    # Float rounding must never nudge an untouched pixel: force every fully
    # masked-out pixel back to the exact original byte value.
    untouched = mask == 0
    out[untouched] = original_bgr[untouched]
    return out
