"""Thin wrapper around `claude -p` for the migration classifier.

We use Sonnet by default (Leon's preference).  The wrapper:

* Probes whether `claude` is on PATH (``is_claude_available``).
* Invokes ``claude -p --model <model> --output-format json`` with a stdin-fed prompt.
* Parses the JSON response, tolerating a ```json ... ``` code fence.
* Maps any failure (non-zero exit, timeout, unparseable output) to
  :class:`LLMUnavailable`, so the caller can fall back to the knowledge-layer
  default without special-casing subprocess errors.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Dict


CLAUDE_BIN = "claude"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_SEC = 60

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class LLMUnavailable(RuntimeError):
    """Raised when the LLM call fails for any reason the caller can recover from."""


def is_claude_available() -> bool:
    """Return True if the `claude` CLI is on PATH."""
    return shutil.which(CLAUDE_BIN) is not None


def call_claude_classifier(
    prompt: str,
    index_context: str,
    file_body: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, object]:
    """Run the classifier prompt through claude -p; return parsed JSON.

    The model is told to output a JSON object with keys: ``layer``,
    ``filename``, ``tags`` (list[str]), ``aliases`` (list[str]),
    ``rationale`` (str).  See ``classify.build_classifier_prompt`` for the
    prompt text — this function is purely I/O + parsing.

    Args:
        prompt: The classifier prompt (instructions + schema).
        index_context: Current ``$MEMORY_HOME/INDEX.md`` content.
        file_body: Legacy file body (with frontmatter intact if present).
        model: Claude model id.
        timeout_sec: Hard timeout for the subprocess.

    Returns:
        Parsed JSON dict.

    Raises:
        LLMUnavailable: If the subprocess fails, times out, or the response
            cannot be parsed as JSON.
    """
    full_input = (
        f"{prompt}\n\n"
        f"## CURRENT INDEX\n{index_context}\n\n"
        f"## FILE TO CLASSIFY\n{file_body}\n"
    )
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", full_input, "--model", model],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMUnavailable(f"claude -p timed out after {timeout_sec}s") from exc

    if result.returncode != 0:
        raise LLMUnavailable(
            f"claude -p exited {result.returncode}: {result.stderr.strip()[:200]}"
        )

    text = result.stdout.strip()
    # Try fenced JSON first, fall back to raw.
    fence = _JSON_FENCE_RE.search(text)
    payload = fence.group(1) if fence else text
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(
            f"could not parse claude -p output as JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise LLMUnavailable("claude -p returned JSON that was not an object")
    return data
