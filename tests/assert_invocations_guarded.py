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

ONE WRAPPER, IN ONE FIXED FORM (Codex, 2026-11-06)
The version before this one accepted any number of wrappers, each needing its
own guard call, and was satisfied by a guard call anywhere above the invocation.
Six wrappers meant six copies of the guard and six places it could be dropped,
and "somewhere above" is not a position. So the rule is now exact:

1. ``install.sh`` and ``uninstall.sh`` may be invoked from **exactly one**
   function in the file, and that function must be named ``REQUIRED_WRAPPER``.
   Not from a ``@test`` body, not from the top level, not from a second helper
   however carefully written.
2. That function's **first executable statement** must be, verbatim,
   ``assert_destructive_targets_sandboxed || return 1`` — not merely a line
   somewhere before the invocation, and not inside a comment.

Two shapes that the previous rule allowed and this one rejects are fixtures in
``tests/test_assert_invocations_guarded.py``: a second guarded wrapper, and a
wrapper whose guard sits after another statement.

Usage: ``assert_invocations_guarded.py <path-to-bats-file>``
Exits 0 when the file satisfies all three, 1 otherwise, listing each violation.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

GUARD_CALL = "assert_destructive_targets_sandboxed"
# The one statement that may open the one wrapper, compared verbatim after
# whitespace collapsing. An exact form is the point: a guard that can be written
# five ways is a guard whose absence is hard to see in a diff.
REQUIRED_FIRST_STATEMENT = f"{GUARD_CALL} || return 1"
REQUIRED_WRAPPER = "rekol_bats_run_destructive"

# BOTH scripts, not just the uninstaller (QA, 2026-11-06). `INVOCATION` was
# `"uninstall.sh"` alone, so `do_full_install` -- which runs `install.sh` with
# `env -u MEMORY_HOME -u TEST_MODE` only -- was invisible to this check. A leaked
# REKOL_INDEX_DIR passed straight through it and install.sh tried to rebuild the
# developer's LIVE index with `test-hashing`. The data survived because the
# PRODUCT's real-index guard refused the rebuild, which is the component
# #216/#217/#220 show has gaps: the suite was being protected by the thing it
# exists to test.
INVOCATION_SCRIPTS = ("uninstall.sh", "install.sh")

# AND the indirect shape, which the literal-name list cannot see. The one
# wrapper runs `"${COMPONENT_DIR}/${script}"`, so a checker keyed on the names
# alone finds ZERO invocations in the file and reports zero violations — which
# looks exactly like being satisfied. That is the defect this project keeps
# producing: a check whose output is identical whether or not the thing held.
# Caught here by the fixtures in tests/test_assert_invocations_guarded.py, which
# failed the moment the wrapper stopped naming its script.
INDIRECT_INVOCATION = re.compile(r"COMPONENT_DIR\}?/\$\{?[A-Za-z_]")

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
    first_statement: str | None = None
    first_statement_line: int | None = None


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

        if current_function is not None and stripped:
            wrapper = wrappers.setdefault(current_function, _Wrapper(name=current_function))
            if wrapper.first_statement is None:
                # The first line inside the function that is code at all. A
                # comment never reaches here: _strip_comment emptied it.
                wrapper.first_statement = " ".join(stripped.split())
                wrapper.first_statement_line = index

        if GUARD_CALL in code and current_function is not None:
            wrapper = wrappers.setdefault(current_function, _Wrapper(name=current_function))
            if wrapper.guard_line is None:
                wrapper.guard_line = index

        names_a_script = any(script in code for script in INVOCATION_SCRIPTS)
        runs_a_variable_path = bool(INDIRECT_INVOCATION.search(code))
        if not (names_a_script or runs_a_variable_path):
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
    """Exactly one wrapper may invoke, under the one required name and form."""
    violations: list[Violation] = []
    invoking = [w for w in wrappers.values() if w.invocation_lines]

    for wrapper in invoking:
        if wrapper.name != REQUIRED_WRAPPER:
            violations.append(
                Violation(
                    wrapper.invocation_lines[0],
                    f"{wrapper.name}() invokes a destructive script, but only "
                    f"{REQUIRED_WRAPPER}() may. Route it through that wrapper "
                    f"instead of guarding a second invocation site",
                )
            )

    if not invoking:
        # ZERO IS A VIOLATION, not a pass (Codex, 2026-11-07). A file that
        # invokes nothing this checker recognises is a file the checker cannot
        # see into, and "guard violations: 0" is the same line it prints when
        # every invocation is correctly guarded. Codex assembled the path across
        # two assignments -- `exe="$COMPONENT_DIR"; exe="$exe/uninstall.sh"` --
        # so no single line carried both halves and the inventory came back
        # empty. This is the second time a refactor blinded it; the first was
        # the indirect `${script}` form.
        violations.append(
            Violation(
                1,
                "no invocation of a destructive script was recognised in this "
                "file at all. Either it does not belong under this check, or the "
                "checker cannot see the shape it uses — teach it the shape "
                "rather than leaving it blind",
            )
        )
        return violations

    required = [w for w in invoking if w.name == REQUIRED_WRAPPER]
    if not required:
        if REQUIRED_WRAPPER in wrappers:
            # The wrapper is DEFINED but nothing in it was recognised as an
            # invocation. Benign-looking and the worst state to be in: every
            # rule below would pass vacuously, and the file would be reported
            # clean while the real invocation went unexamined. This is how the
            # indirect `${script}` form slipped past the name-keyed version.
            violations.append(
                Violation(
                    1,
                    f"{REQUIRED_WRAPPER}() is defined but no invocation was "
                    f"recognised inside it — the checker would pass vacuously. "
                    f"Teach it the shape rather than leaving it blind",
                )
            )
        # Otherwise the file has no invocations at all, which is fine, or they
        # are all flagged above.
        return violations

    wrapper = required[0]
    if wrapper.first_statement != REQUIRED_FIRST_STATEMENT:
        violations.append(
            Violation(
                wrapper.first_statement_line or wrapper.invocation_lines[0],
                f"{REQUIRED_WRAPPER}() must open with exactly "
                f"{REQUIRED_FIRST_STATEMENT!r}; its first statement is "
                f"{wrapper.first_statement!r}",
            )
        )
        return violations

    early = [n for n in wrapper.invocation_lines if n < (wrapper.guard_line or 0)]
    if early:
        violations.append(
            Violation(
                early[0],
                f"{wrapper.name}() invokes a destructive script at line "
                f"{early[0]}, before its {GUARD_CALL} at line {wrapper.guard_line}",
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
