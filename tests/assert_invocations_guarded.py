#!/usr/bin/env python3
"""Assert every ``uninstall.sh`` invocation in a bats file is guarded (#223).

Run by ``test_uninstall.bats``. Exists because the individual
``assert_destructive_targets_sandboxed`` calls can each be deleted without any
other test noticing: the suite would stay green while losing the only thing
standing between it and the developer's real install. This checks the property
rather than one instance of it.

Usage: ``assert_invocations_guarded.py <path-to-bats-file>``
Exits 0 when every invocation is guarded, 1 otherwise, listing the offenders.
"""

from __future__ import annotations

import sys
from pathlib import Path

GUARD_CALL = "assert_destructive_targets_sandboxed"

# A window rather than the immediately-preceding line. A guarded invocation
# legitimately looks like:
#
#     assert_destructive_targets_sandboxed
#     run env -u MEMORY_HOME -u TEST_MODE \
#         REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
#         "${COMPONENT_DIR}/uninstall.sh" --yes
#
# so the assertion can sit several continuation lines above the invocation.
PRECEDING_LINES_ALLOWED = 8


def unguarded_invocations(bats_file: Path) -> list[tuple[int, str]]:
    """Return ``(line_number, text)`` for each unguarded invocation line."""
    lines: list[str] = bats_file.read_text().splitlines()
    offenders: list[tuple[int, str]] = []

    for index, line in enumerate(lines):
        if "uninstall.sh" not in line:
            continue
        if line.lstrip().startswith("#"):
            continue
        # Only real invocations reach the script through COMPONENT_DIR; prose and
        # assertions that merely mention the name are not invocations.
        if "COMPONENT_DIR" not in line:
            continue
        window_start = max(0, index - PRECEDING_LINES_ALLOWED)
        if any(GUARD_CALL in lines[j] for j in range(window_start, index)):
            continue
        offenders.append((index + 1, line.strip()))

    return offenders


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <bats-file>", file=sys.stderr)
        return 2

    bats_file = Path(argv[1])
    if not bats_file.is_file():
        print(f"not a file: {bats_file}", file=sys.stderr)
        return 2

    offenders = unguarded_invocations(bats_file)
    print(f"unguarded invocations: {len(offenders)}")
    for line_number, text in offenders:
        print(f"  line {line_number}: {text}")
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
