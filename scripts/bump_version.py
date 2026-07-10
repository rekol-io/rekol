#!/usr/bin/env python3
"""Bump the rekol version, keeping the two version literals in lockstep. See issue #102.

Pre-1.0 convention (``0.x.y``): every PR bumps the **patch** ``y``; a deliberate
**minor** bump ``x`` (a stable release) is set by hand and must NOT get an extra
patch on top. This script is the single source of that logic — run manually today,
and (post-launch, once CI is on stable hosted runners) called from a bump-on-merge
CI step. It keeps ``pyproject.toml`` ``version`` and ``src/rekol/__init__.py``
``__version__`` in sync, and refuses to run if they have already drifted apart.

Usage:
  bump_version.py                    # bump patch y (the common per-PR case)
  bump_version.py --set 0.2.0        # set an explicit version (e.g. a minor release)
  bump_version.py --baseline-ref REF # skip the patch bump if the major/minor already
                                     # changed vs REF — so a minor-release PR that set
                                     # 0.2.0 does not become 0.2.1 on merge
  bump_version.py --check            # print the intended action; write nothing

Exit codes: 0 = bumped, set, or deliberately skipped; 1 = error (drift, parse, etc.).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "rekol" / "__init__.py"

# Anchored at line start so we match ONLY the top-level [project] version, never a
# dependency spec elsewhere in the file. The captured group is the X.Y.Z literal.
_PYPROJECT_RE = re.compile(r'(?m)^version = "(\d+\.\d+\.\d+)"$')
_INIT_RE = re.compile(r'(?m)^__version__ = "(\d+\.\d+\.\d+)"$')


class VersionError(RuntimeError):
    """Raised on parse failure, drift between the two files, or a bad version string."""


@dataclass(frozen=True)
class Version:
    """A parsed ``X.Y.Z`` semantic version."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        """Parse ``X.Y.Z`` into a Version, or raise VersionError."""
        parts = text.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise VersionError(f"not a X.Y.Z version: {text!r}")
        major, minor, patch = (int(p) for p in parts)
        return cls(major, minor, patch)

    def bumped_patch(self) -> Version:
        """Return this version with the patch incremented."""
        return Version(self.major, self.minor, self.patch + 1)

    @property
    def series(self) -> tuple[int, int]:
        """The (major, minor) pair — the part a deliberate release bumps."""
        return (self.major, self.minor)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def _extract(text: str, pattern: re.Pattern[str], label: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise VersionError(f"no version line found in {label}")
    return match.group(1)


def read_current(pyproject: Path = PYPROJECT, init_py: Path = INIT_PY) -> Version:
    """Read both literals, require they agree, and return the shared version.

    A mismatch is an error, not something to silently reconcile: it means a prior
    edit touched one file and not the other, and we must not paper over which is right.
    """
    py_str = _extract(pyproject.read_text(), _PYPROJECT_RE, str(pyproject))
    init_str = _extract(init_py.read_text(), _INIT_RE, str(init_py))
    if py_str != init_str:
        raise VersionError(
            f"version drift: {pyproject.name} has {py_str!r} but "
            f"{init_py.name} has {init_str!r} — reconcile by hand before bumping"
        )
    return Version.parse(py_str)


def write_version(new: Version, pyproject: Path = PYPROJECT, init_py: Path = INIT_PY) -> None:
    """Rewrite the single version line in each file, preserving all other content."""
    for path, pattern, replacement in (
        (pyproject, _PYPROJECT_RE, f'version = "{new}"'),
        (init_py, _INIT_RE, f'__version__ = "{new}"'),
    ):
        text = path.read_text()
        new_text, count = pattern.subn(replacement, text)
        if count != 1:
            raise VersionError(f"expected exactly one version line in {path}, replaced {count}")
        path.write_text(new_text)


def _version_at_ref(ref: str, pyproject: Path = PYPROJECT) -> Version:
    """Read the version from ``pyproject.toml`` as it exists at a git ref."""
    rel = pyproject.relative_to(REPO_ROOT)
    try:
        text = subprocess.run(
            ["git", "show", f"{ref}:{rel.as_posix()}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise VersionError(
            f"cannot read pyproject.toml at ref {ref!r}: {exc.stderr.strip()}"
        ) from exc
    return Version.parse(_extract(text, _PYPROJECT_RE, f"{ref}:{rel}"))


def decide(current: Version, baseline: Version | None) -> Version | None:
    """Return the version to write, or None when the patch bump should be skipped.

    Skip when a baseline is given and the (major, minor) series already changed — that
    means this change deliberately cut a new minor release (e.g. 0.1.x → 0.2.0) and must
    keep its 0.2.0, not roll forward to 0.2.1.
    """
    if baseline is not None and current.series != baseline.series:
        return None
    return current.bumped_patch()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Bump the rekol version (0.x.y).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--set", dest="set_to", metavar="X.Y.Z", help="set an explicit version")
    group.add_argument(
        "--baseline-ref",
        metavar="REF",
        help="skip the patch bump if major/minor already changed vs this git ref",
    )
    group.add_argument(
        "--assert-ahead-of",
        metavar="REF",
        help="exit non-zero unless the current version is strictly greater than the version "
        "at REF (enforce the per-PR bump in CI; writes nothing)",
    )
    parser.add_argument(
        "--check", action="store_true", help="print the intended action; write nothing"
    )
    args = parser.parse_args(argv)

    try:
        # Reference the module globals at call time so tests can redirect them.
        current = read_current(PYPROJECT, INIT_PY)
        if args.assert_ahead_of is not None:
            # #102 enforcement: every PR must bump the version. Fail unless the current
            # version is strictly greater than the base branch's — no files written.
            base = _version_at_ref(args.assert_ahead_of, PYPROJECT)
            if (current.major, current.minor, current.patch) <= (base.major, base.minor, base.patch):
                print(
                    f"error: version not bumped — {current} is not ahead of {base} "
                    f"(at {args.assert_ahead_of}). Run: python scripts/bump_version.py",
                    file=sys.stderr,
                )
                return 1
            print(f"version OK: {current} > {base} (bumped vs {args.assert_ahead_of})")
            return 0
        if args.set_to is not None:
            target: Version | None = Version.parse(args.set_to)
        else:
            baseline = _version_at_ref(args.baseline_ref, PYPROJECT) if args.baseline_ref else None
            target = decide(current, baseline)

        if target is None:
            print(f"skip: {current} already cut a new minor series — no patch bump")
            return 0
        if target == current and args.set_to is None:
            # Only reachable if a future change makes bump a no-op; guard anyway.
            print(f"no change: already at {current}")
            return 0

        if args.check:
            verb = "set" if args.set_to is not None else "bump"
            print(f"would {verb}: {current} -> {target}")
            return 0

        write_version(target, PYPROJECT, INIT_PY)
        print(f"{current} -> {target}")
        return 0
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
