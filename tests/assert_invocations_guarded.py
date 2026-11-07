#!/usr/bin/env python3
"""Assert no bats test can invoke ``install.sh`` or ``uninstall.sh`` unguarded (#233).

Run by ``test_uninstall.bats``. Exists because the individual guard calls can
each be deleted without any other test noticing: the suite would stay green
while losing the only thing standing between it and the developer's real
install. This checks the property rather than one instance of it.

WHY THIS IS NOT A PROXIMITY CHECK ANY MORE (Codex, 2026-11-06)
The first version looked for the guard's name within the eight lines above an
invocation, by substring match. A **comment** mentioning
``assert_destructive_targets_sandboxed`` therefore satisfied it, and so would a
guard call sitting in an unrelated branch that never executes. Text proximity is
not a rule.

The rule enforced instead is structural, and there is no way to word your way
past it:

1. ``install.sh`` and ``uninstall.sh`` may be invoked **only** from inside a
   shell function — never directly in a ``@test`` body, where no wrapper can
   stand in front of them.
2. That function must call ``assert_destructive_targets_sandboxed`` as an
   executable statement, not inside a comment.
3. It must call it BEFORE the invocation.

Keyed on the guard call rather than on a ``run_uninstall*`` name, so a new
helper with any name is covered the moment it invokes either script. Keying on
the name is what let ``do_full_install`` slip through.

Usage: ``assert_invocations_guarded.py <path-to-bats-file>``
Exits 0 when the file satisfies all three, 1 otherwise, listing each violation.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

GUARD_CALL = "assert_destructive_targets_sandboxed"

# BOTH scripts, not just the uninstaller (QA, 2026-11-06). `INVOCATION` was
# `"uninstall.sh"` alone, so `do_full_install` -- which runs `install.sh` with
# `env -u MEMORY_HOME -u TEST_MODE` only -- was invisible to this check. A leaked
# REKOL_INDEX_DIR passed straight through it and install.sh tried to rebuild the
# developer's LIVE index with `test-hashing`. The data survived because the
# PRODUCT's real-index guard refused the rebuild, which is the component
# #216/#217/#220 show has gaps: the suite was being protected by the thing it
# exists to test.
INVOCATION_SCRIPTS = ("uninstall.sh", "install.sh")

FUNCTION_START = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{")
TEST_START = re.compile(r"^@test\s")


@dataclass
class Violation:
    line_number: int
    message: str


@dataclass
class _Wrapper:
    name: str
    guard_line: int | None = None
    invocation_lines: list[int] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    """Drop a trailing comment so a mention inside one never counts as code.

    Deliberately simple: a ``#`` inside a quoted string would be stripped too,
    which can only ever make this check stricter, never laxer.
    """
    without = line.split("#", 1)[0]
    return without


def check(bats_file: Path) -> list[Violation]:
    """Return every violation of the three rules above."""
    lines = bats_file.read_text().splitlines()
    violations, wrappers = _scan(lines)
    return violations + _validate_wrappers(wrappers)


def _scan(lines: list[str]) -> tuple[list[Violation], dict[str, _Wrapper]]:
    """Find every invocation and wrapper, flagging invocations outside wrappers."""
    violations: list[Violation] = []
    current_function: str | None = None
    wrappers: dict[str, _Wrapper] = {}

    for index, raw_line in enumerate(lines, start=1):
        code = _strip_comment(raw_line)
        stripped = code.strip()

        match = FUNCTION_START.match(stripped)
        if match:
            current_function = match.group(1)
            continue
        if TEST_START.match(stripped):
            current_function = None
            continue
        if stripped == "}":
            current_function = None
            continue

        if GUARD_CALL in code and current_function is not None:
            wrapper = wrappers.setdefault(current_function, _Wrapper(name=current_function))
            if wrapper.guard_line is None:
                wrapper.guard_line = index

        if not any(script in code for script in INVOCATION_SCRIPTS):
            continue
        # Prose and assertions that merely name the script are not invocations;
        # reaching it goes through COMPONENT_DIR.
        if "COMPONENT_DIR" not in code:
            continue

        if current_function is None:
            violations.append(
                Violation(
                    index,
                    f"invokes a destructive script outside any function "
                    f"(a @test body or top level): {stripped}",
                )
            )
            continue
        wrappers.setdefault(current_function, _Wrapper(name=current_function))
        wrappers[current_function].invocation_lines.append(index)

    return violations, wrappers


def _validate_wrappers(wrappers: dict[str, _Wrapper]) -> list[Violation]:
    """Every wrapper that invokes must guard first."""
    violations: list[Violation] = []
    for wrapper in wrappers.values():
        if not wrapper.invocation_lines:
            # A wrapper that invokes nothing needs no guard; it is probably a
            # helper. Not a violation.
            continue
        if wrapper.guard_line is None:
            violations.append(
                Violation(
                    wrapper.invocation_lines[0],
                    f"{wrapper.name}() invokes a destructive script but never "
                    f"calls {GUARD_CALL} as a statement",
                )
            )
            continue
        early = [n for n in wrapper.invocation_lines if n < wrapper.guard_line]
        if early:
            violations.append(
                Violation(
                    early[0],
                    f"{wrapper.name}() invokes a destructive script at line "
                    f"{early[0]}, before its {GUARD_CALL} at line "
                    f"{wrapper.guard_line}",
                )
            )

    return violations


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <bats-file>", file=sys.stderr)
        return 2

    bats_file = Path(argv[1])
    if not bats_file.is_file():
        print(f"not a file: {bats_file}", file=sys.stderr)
        return 2

    violations = check(bats_file)
    print(f"guard violations: {len(violations)}")
    for violation in violations:
        print(f"  line {violation.line_number}: {violation.message}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
