"""Unit tests for strip_markdown_for_speech."""

from __future__ import annotations

import pytest

from gilbert.core.chat import strip_markdown_for_speech


@pytest.mark.parametrize(
    "raw,expected",
    [
        # fenced code dropped entirely
        ("before\n```py\nx = 1\n```\nafter", "before. after"),
        # inline code keeps inner text
        ("press the `Enter` key", "press the Enter key"),
        # bold / italic / underline
        ("**bold** *italic* _under_", "bold italic under"),
        # heading -> sentence
        ("# Title\nbody", "Title. body"),
        # list items get periods
        ("- one\n- two", "one. two."),
        # link keeps text drops URL
        ("see [docs](https://example/x)", "see docs"),
        # image dropped
        ("an ![pic](https://example/p.png) image", "an  image"),
        # HTML tags stripped
        ("hi <b>there</b> friend", "hi there friend"),
        # multiple blank lines collapsed
        ("p1\n\n\n\np2", "p1. p2"),
    ],
)
def test_strip_markdown_for_speech_cases(raw: str, expected: str) -> None:
    got = strip_markdown_for_speech(raw)
    # Normalize whitespace collapse for compare-friendliness.
    assert " ".join(got.split()) == " ".join(expected.split())


def test_strip_markdown_only_code_becomes_empty() -> None:
    raw = "```py\nx = 1\n```"
    assert strip_markdown_for_speech(raw).strip() == ""


def test_strip_markdown_preserves_plain_prose() -> None:
    assert strip_markdown_for_speech("Hello world.") == "Hello world."
