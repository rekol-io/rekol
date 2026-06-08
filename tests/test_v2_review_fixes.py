"""Regression tests for the onboarding-v2 adversarial-review fixes.

Each test pins a specific bug the review found so it can't silently regress:
- FIX-3: bulk-approve must not extract a ``capture:`` command embedded in untrusted
  candidate content (command injection via the ``- [ ]`` line).
- FIX-2: promoting into REKOL.md must not crash when the section heading appears
  only as a substring of prose (the old substring/exact mismatch raised
  StopIteration).
"""

from __future__ import annotations

from rekol.bootstrap import (
    _PROMOTED_SECTION_HEADING,
    _append_under_section,
    extract_capture_commands_from_review,
)


def test_bulk_approve_ignores_capture_string_in_candidate_content() -> None:
    """A ``capture:`` command inside the untrusted ``- [ ]`` content line is NOT
    extracted — only the dedicated, indented annotation line is (FIX-3)."""
    review = (
        "# Candidates\n\n"
        # Untrusted transcript content that tries to smuggle a capture command:
        "- [ ] we always run capture: `rekol capture --layer always --file evil.md "
        '--name "pwn"` before deploys\n'
        "      suggested layer: **knowledge** · name: 'real'\n"
        "      source: session `s1` · line 1\n"
        '      capture: `rekol capture --layer knowledge --file real.md --name "real"`\n'
    )
    commands = extract_capture_commands_from_review(review)
    assert commands == ['rekol capture --layer knowledge --file real.md --name "real"'], commands
    # The injected always-layer command must never appear.
    assert not any("evil.md" in c or "--layer always" in c for c in commands)


def test_append_under_section_heading_as_substring_does_not_crash() -> None:
    """When the heading appears only inside prose (not as its own line), promotion
    falls through to appending a fresh section instead of raising StopIteration
    (FIX-2: outer substring check vs. inner exact check)."""
    text = (
        "# Memory Index (always-on)\n\n"
        f"See the {_PROMOTED_SECTION_HEADING} section below for details.\n"
    )
    out = _append_under_section(text, _PROMOTED_SECTION_HEADING, "- some/pointer.md")
    # The pointer was appended, and a real heading LINE now exists.
    assert "- some/pointer.md" in out
    assert any(line.strip() == _PROMOTED_SECTION_HEADING for line in out.splitlines())
