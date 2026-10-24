"""The supported-Python range in install.sh, and the candidates it probes (#207).

WHY THIS EXISTS: `install.sh` builds its own venv and picks the interpreter itself.
Its suitability probe required `>=3.11` with **no upper bound**, so it accepted
whatever the newest python on the machine was. When Homebrew's `python3` became
3.15 (2026-10-09) and torch still published nothing past cp314, the installer
chose 3.15 and then handed pip an unsolvable problem — the failure surfaced
thousands of lines later as `ResolutionImpossible: ... torch`, blaming
sentence-transformers rather than the interpreter choice that caused it. The
required `macos-latest` gate was red for four consecutive merges.

No commit caused that, which is why it is worth pinning in a test: the trigger was
the outside world moving, and the same class of breakage returns every time the
ceiling and the probed candidates drift apart. The invariant is textual and
deterministic — no network, no interpreter zoo required — because what went wrong
was a *policy* inconsistency, not behaviour under a particular Python.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"


def _show(version: tuple[int, int]) -> str:
    """`(3, 14)` as `3.14`, so assertion messages read like version numbers."""
    return f"{version[0]}.{version[1]}"


def _bound(name: str) -> tuple[int, int]:
    """The (major, minor) of a `NAME="X.Y"` assignment in install.sh."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(rf'^\s*{name}="(\d+)\.(\d+)"\s*$', text, re.MULTILINE)
    assert match is not None, f"{name} is not set in install.sh — was it renamed?"
    return int(match.group(1)), int(match.group(2))


def _versioned_candidates() -> set[tuple[int, int]]:
    """Every explicit pythonX.Y the interpreter-selection block probes."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index('  PY3=""')
    end = text.index("no suitable Python found")
    return {
        (int(major), int(minor))
        for major, minor in re.findall(r"python(\d+)\.(\d+)", text[start:end])
    }


def test_the_supported_range_is_bounded_at_both_ends() -> None:
    """An open-ended ceiling is the defect itself, not a missing nicety."""
    py_min = _bound("PY_MIN")
    py_max = _bound("PY_MAX_EXCLUSIVE")
    assert py_min < py_max, f"empty supported range: >={py_min} and <{py_max}"


def test_every_probed_candidate_is_inside_the_supported_range() -> None:
    """The ceiling and the candidate list must not drift apart.

    Both directions are real mistakes. Probing a version the ceiling excludes means
    the installer spends time on an interpreter it will then refuse. Raising the
    ceiling without adding candidates means a machine whose only usable python is
    the new version still falls through to the generic `python3` — which is exactly
    the path that picked 3.15.
    """
    py_min = _bound("PY_MIN")
    py_max = _bound("PY_MAX_EXCLUSIVE")
    candidates = _versioned_candidates()
    assert candidates, "found no pythonX.Y candidates — did the block move?"

    outside = sorted(v for v in candidates if not (py_min <= v < py_max))
    assert not outside, (
        f"install.sh probes {[_show(v) for v in outside]} but only accepts "
        f">={_show(py_min)} and <{_show(py_max)}"
    )


def test_every_supported_minor_is_actually_probed() -> None:
    """A supported version nothing looks for is only reachable by luck.

    The generic `python3` fallback would find it only when it happens to be the
    machine's default; otherwise install.sh hard-fails while a perfectly good
    interpreter sits at `pythonX.Y`.
    """
    py_min = _bound("PY_MIN")
    py_max = _bound("PY_MAX_EXCLUSIVE")
    candidates = _versioned_candidates()

    assert py_min[0] == py_max[0] == 3, "this assertion assumes a 3.x range"
    expected = {(3, minor) for minor in range(py_min[1], py_max[1])}
    missing = sorted(expected - candidates)
    assert not missing, (
        f"supported but never probed: {[_show(v) for v in missing]} — add them to "
        "the candidate list or narrow the range"
    )


def test_the_probe_rejects_a_too_new_interpreter_explicitly() -> None:
    """The refusal must name the reason, not just decline.

    `ResolutionImpossible` from pip is what users saw for two weeks; the point of
    the ceiling is a message that says which interpreter was wrong and why.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "is too new" in text, "the probe no longer explains a too-new rejection"
    assert "REKOL_PY_MAX" in text, "the ceiling is no longer passed to the probe"
