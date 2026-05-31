"""Tests for the claude -p subprocess wrapper used by the classifier."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from rekol.migrate.llm import (
    LLMUnavailable,
    call_claude_classifier,
    is_claude_available,
)


def test_is_claude_available_true_when_on_path() -> None:
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        assert is_claude_available() is True


def test_is_claude_available_false_when_missing() -> None:
    with patch("shutil.which", return_value=None):
        assert is_claude_available() is False


def test_call_claude_classifier_parses_json_response() -> None:
    fake_result = subprocess.CompletedProcess(
        args=["claude"],
        returncode=0,
        stdout='```json\n{"layer": "topic", "filename": "foo.md", "tags": ["a"], "aliases": ["b"], "rationale": "it is a topic"}\n```\n',
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_result):
        out = call_claude_classifier(
            prompt="classify this",
            index_context="# INDEX\n",
            file_body="foo body",
        )
    assert out["layer"] == "topic"
    assert out["filename"] == "foo.md"
    assert out["tags"] == ["a"]
    assert out["aliases"] == ["b"]


def test_call_claude_classifier_accepts_plain_json() -> None:
    """Some Sonnet responses skip the ```json fence; wrapper must still parse."""
    fake_result = subprocess.CompletedProcess(
        args=["claude"],
        returncode=0,
        stdout='{"layer": "when", "filename": "when-x.md", "tags": [], "aliases": []}\n',
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_result):
        out = call_claude_classifier("p", "i", "b")
    assert out["layer"] == "when"


def test_call_claude_classifier_raises_on_nonzero_exit() -> None:
    fake_result = subprocess.CompletedProcess(
        args=["claude"],
        returncode=1,
        stdout="",
        stderr="bedrock denied",
    )
    with patch("subprocess.run", return_value=fake_result):
        with pytest.raises(LLMUnavailable, match="claude -p exited 1"):
            call_claude_classifier("p", "i", "b")


def test_call_claude_classifier_raises_on_unparseable() -> None:
    fake_result = subprocess.CompletedProcess(
        args=["claude"],
        returncode=0,
        stdout="I'm sorry, I can't.",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_result):
        with pytest.raises(LLMUnavailable, match="could not parse"):
            call_claude_classifier("p", "i", "b")


def test_call_claude_classifier_timeout_maps_to_LLMUnavailable() -> None:
    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=60)

    with patch("subprocess.run", side_effect=_raise):
        with pytest.raises(LLMUnavailable, match="timed out"):
            call_claude_classifier("p", "i", "b")
