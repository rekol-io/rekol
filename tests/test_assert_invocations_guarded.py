"""The bats guard checker must reject the shapes it used to accept (#233).

The checker is the only thing standing between the bats suites and a delete
against the developer's real install, and it has already been wrong three times:
satisfied by a guard named inside a *comment*, satisfied by any arrangement that
put a guard call somewhere above the invocation, and — caught by these fixtures
— blind to the indirect ``"${COMPONENT_DIR}/${script}"`` invocation, which made
it report zero violations because it could see nothing at all.

The fixtures are the files under ``tests/fixtures/guard/``, shared with the bats
test of the same name so there is one definition of each shape. They are files
rather than inline strings for a concrete reason: the checker scans a file line
by line, so a heredoc body holding a second wrapper read as a second wrapper and
the suite flagged itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assert_invocations_guarded import (  # noqa: E402
    REQUIRED_FIRST_STATEMENT,
    REQUIRED_WRAPPER,
    check,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "guard"


def _fixtures(prefix: str) -> list[Path]:
    found = sorted(FIXTURES.glob(f"{prefix}-*.bats"))
    assert found, f"no {prefix}-* fixtures found in {FIXTURES}"
    return found


@pytest.mark.parametrize("fixture", _fixtures("accept"), ids=lambda p: p.stem)
def test_an_accepted_shape_has_no_violations(fixture: Path) -> None:
    """The control. A checker that rejects everything passes every test below."""
    assert check(fixture) == []


@pytest.mark.parametrize("fixture", _fixtures("reject"), ids=lambda p: p.stem)
def test_a_rejected_shape_is_reported(fixture: Path) -> None:
    """Each of these is a shape some version of the checker accepted."""
    violations = check(fixture)
    assert violations, f"{fixture.name} was accepted"
    assert all(v.message for v in violations), "a violation with no message is unactionable"
    assert all(v.line_number >= 1 for v in violations), "a violation with no line is unlocatable"


def test_every_rejection_names_what_to_do(subtests=None) -> None:
    """A violation that does not say which rule broke cannot be acted on.

    Checked as a property over all the rejection fixtures rather than asserting
    one exact string, so adding a fixture does not need a matching assertion
    here — only a message that mentions the wrapper, the guard, or the scope.
    """
    vocabulary = (REQUIRED_WRAPPER, REQUIRED_FIRST_STATEMENT, "outside any function", "recognised")
    for fixture in _fixtures("reject"):
        messages = [v.message for v in check(fixture)]
        assert any(any(word in m for word in vocabulary) for m in messages), (
            f"{fixture.name} is rejected, but none of its messages names a rule: {messages}"
        )


def test_the_uninstall_suite_satisfies_the_checker() -> None:
    """End to end, on the file the checker is run against in CI.

    ``test_install.bats`` is deliberately NOT held to this rule, and saying so
    here is better than quietly asserting a property it does not have. It
    invokes ``install.sh`` directly from more than forty ``@test`` bodies; what
    protects it is the fail-closed env check called in both ``setup_file`` and
    ``setup``, so no test in it can run with an override pointing outside the
    sandbox. The per-target ``assert_sandboxed`` calls and the one-wrapper rule
    are the uninstaller suite's, because that is the suite whose whole purpose
    is deleting things.
    """
    target = Path(__file__).resolve().parent / "test_uninstall.bats"
    assert check(target) == [], "test_uninstall.bats has guard violations"
