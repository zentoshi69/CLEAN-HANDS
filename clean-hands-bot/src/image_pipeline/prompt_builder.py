"""Prompt construction for the glove inpainting edit.

One job: gloves on, everything else untouched. The "strict" mode is used
automatically when a first attempt fails the quality gate, and by /retry.
"""

from __future__ import annotations

from dataclasses import dataclass

MODES = ("balanced", "soft", "hard", "strict")

NEGATIVE_PROMPT = (
    "Do not change the face. Do not change identity. Do not change expression. "
    "Do not change clothes. Do not change body shape. Do not change background. "
    "Do not add text. Do not add logos. Do not add money. Do not add extra objects. "
    "Do not remove objects. Do not change the object being held. "
    "Do not create extra fingers. Do not create extra hands. Do not deform fingers. "
    "Do not cartoonize a real photo. Do not repaint the whole image. "
    "Do not apply filters. Do not change lighting outside the masked hand areas."
)

_BALANCED = (
    "Edit the image by adding light blue transparent medical nitrile gloves ONLY "
    "to every visible bare human hand. The gloves must fit naturally over the "
    "fingers, palm, knuckles, fingernails, and wrist area, with realistic thin "
    "latex/nitrile wrinkles, subtle glossy highlights, soft blue transparency, "
    "and shadows matching the original lighting. Preserve the exact original "
    "face, pose, clothing, objects, background, camera angle, colors, and "
    "composition. Do not modify anything except the visible hand skin inside "
    "the mask. If the hand is holding an object, keep the object unchanged and "
    "make the glove wrap naturally around the visible fingers."
)

_SOFT = (
    "Add very natural semi-transparent pale blue medical gloves only on the "
    "visible hands. Keep the edit subtle, realistic, thin, clean, and "
    "believable. Preserve the original image exactly outside the masked hand "
    "areas."
)

_HARD = (
    "Add clearly visible glossy baby-blue medical nitrile gloves only to the "
    "visible hands. Make the glove material obvious but realistic, with folds, "
    "stretch marks, highlights, and natural shadows. Preserve everything else "
    "exactly."
)

_STRICT = (
    _BALANCED
    + " CRITICAL: this is a minimal surgical edit. Every pixel outside the "
    "masked hand regions must remain byte-identical to the original image. "
    "Match the original photographic or illustration style exactly. Keep the "
    "exact same finger count, finger positions, and hand pose. The only "
    "change allowed is a thin layer of light blue (#9ED8FF) semi-transparent "
    "medical glove material over the bare hand skin."
)

_PROMPTS: dict[str, str] = {
    "balanced": _BALANCED,
    "soft": _SOFT,
    "hard": _HARD,
    "strict": _STRICT,
}


@dataclass(frozen=True)
class PromptBundle:
    """Prompt pair handed to an image edit provider."""

    mode: str
    prompt: str
    negative_prompt: str


def build_prompt(mode: str = "balanced") -> PromptBundle:
    """Return the prompt bundle for a glove edit mode."""
    if mode not in _PROMPTS:
        raise ValueError(f"Unknown prompt mode {mode!r}; expected one of {MODES}")
    return PromptBundle(mode=mode, prompt=_PROMPTS[mode], negative_prompt=NEGATIVE_PROMPT)


def stricter(bundle: PromptBundle) -> PromptBundle:
    """Escalate any mode to the strict retry prompt."""
    return build_prompt("strict")
