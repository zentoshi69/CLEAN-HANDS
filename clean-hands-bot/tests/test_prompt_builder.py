"""Tests for prompt construction."""

from __future__ import annotations

import pytest

from src.image_pipeline.prompt_builder import (
    MODES,
    NEGATIVE_PROMPT,
    build_prompt,
    stricter,
)


class TestBuildPrompt:
    def test_balanced_is_default(self) -> None:
        bundle = build_prompt()
        assert bundle.mode == "balanced"
        assert "light blue transparent medical nitrile gloves" in bundle.prompt
        assert "ONLY" in bundle.prompt

    def test_soft_mode(self) -> None:
        bundle = build_prompt("soft")
        assert "semi-transparent pale blue medical gloves" in bundle.prompt
        assert "subtle" in bundle.prompt

    def test_hard_mode(self) -> None:
        bundle = build_prompt("hard")
        assert "glossy baby-blue medical nitrile gloves" in bundle.prompt

    def test_strict_mode_extends_balanced(self) -> None:
        balanced = build_prompt("balanced")
        strict = build_prompt("strict")
        assert strict.prompt.startswith(balanced.prompt)
        assert "CRITICAL" in strict.prompt
        assert len(strict.prompt) > len(balanced.prompt)

    def test_all_modes_share_negative_prompt(self) -> None:
        for mode in MODES:
            assert build_prompt(mode).negative_prompt == NEGATIVE_PROMPT

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="cursed"):
            build_prompt("cursed")


class TestNegativePrompt:
    def test_protects_critical_regions(self) -> None:
        for clause in (
            "Do not change the face.",
            "Do not change background.",
            "Do not add text.",
            "Do not add money.",
            "Do not create extra fingers.",
            "Do not create extra hands.",
            "Do not change the object being held.",
            "Do not repaint the whole image.",
        ):
            assert clause in NEGATIVE_PROMPT


class TestStricter:
    def test_stricter_escalates_any_mode(self) -> None:
        for mode in ("balanced", "soft", "hard"):
            escalated = stricter(build_prompt(mode))
            assert escalated.mode == "strict"

    def test_stricter_is_idempotent(self) -> None:
        strict = build_prompt("strict")
        assert stricter(strict) == strict
