"""Nothing in the memory home is written non-atomically (#238).

``Path.write_text`` and ``open(path, "w")`` truncate before writing, so every
in-place edit destroyed the user's file first and wrote the replacement second.
A fault in between left the memory truncated or empty, and the truncated copy
was the only one. ``capture`` had the same shape with a different consequence: a
0-byte or half-written ``.md`` under a real name, which the indexer reads as a
memory.

The rule is one line and these tests are keyed on it rather than on the five
call sites that happened to break it, because the next one added would not be in
that list.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from rekol.atomic_write import AtomicWriteError, write_text_atomically

# Modules that write files inside the memory home. The rule applies to these;
# writes into the index dir or the config are a different question (those are
# rebuildable, and tasks.py/bootstrap_state.py already stage + replace).
MEMORY_WRITING_MODULES = (
    "cli_capture.py",
    "cli_confidence.py",
    "cli_invalidate.py",
    "cli_review.py",
    "cli_propose.py",
)

SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "rekol"


def _truncating_writes(tree: ast.AST) -> list[tuple[int, str]]:
    """Every call in ``tree`` that opens a path for writing by truncation."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("write_text", "write_bytes"):
            found.append((node.lineno, f".{func.attr}(...)"))
            continue
        if isinstance(func, ast.Name) and func.id == "open":
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = str(keyword.value.value)
            if "w" in mode or "a" in mode:
                found.append((node.lineno, f"open(..., {mode!r})"))
    return found


@pytest.mark.parametrize("module_name", MEMORY_WRITING_MODULES)
def test_no_module_writes_a_memory_by_truncation(module_name: str) -> None:
    """THE RULE, as a property of the source rather than a list of call sites.

    A new command that writes a memory with ``write_text`` fails here even
    though nobody thought to add a test for it — which is the point, because the
    five sites this fixed were each added by someone who did not know the rule
    existed.
    """
    source_file = SOURCE_DIR / module_name
    assert source_file.is_file(), f"{module_name} has moved; update MEMORY_WRITING_MODULES"
    offenders = _truncating_writes(ast.parse(source_file.read_text()))
    assert not offenders, (
        f"{module_name} writes a file by truncation, which destroys the existing "
        f"content before the replacement is written:\n"
        + "\n".join(f"  line {line}: {what}" for line, what in offenders)
        + "\n  Use rekol.atomic_write.write_text_atomically instead."
    )


def test_the_rule_check_actually_catches_a_violation(tmp_path: Path) -> None:
    """The control for the test above, which would otherwise pass on any file.

    A property test that cannot fail is the defect this project keeps shipping,
    so the detector is pointed at a module containing exactly one new
    ``write_text`` and required to find it.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from pathlib import Path\ndef edit(p: Path, text: str) -> None:\n    p.write_text(text)\n"
    )
    offenders = _truncating_writes(ast.parse(probe.read_text()))
    assert offenders, "the detector does not see a plain write_text"
    assert offenders[0][1] == ".write_text(...)"

    handle_probe = tmp_path / "probe2.py"
    handle_probe.write_text('def edit(p, t):\n    with open(p, "w") as h:\n        h.write(t)\n')
    assert _truncating_writes(ast.parse(handle_probe.read_text())), (
        "the detector does not see open(..., 'w')"
    )


def test_an_existing_memory_survives_a_failed_write(tmp_path: Path) -> None:
    """The whole point: the old content is still there after a failure.

    Simulated by making the staged write fail — the target must not have been
    opened at all, let alone truncated.
    """
    target = tmp_path / "topics" / "sailing.md"
    target.parent.mkdir(parents=True)
    original = "---\nname: Sailing\n---\n\nthe original body\n"
    target.write_text(original)

    real_fsync = os.fsync

    def _fail(fd: int) -> None:
        raise OSError("simulated ENOSPC at fsync")

    os.fsync = _fail  # type: ignore[assignment]
    try:
        with pytest.raises(AtomicWriteError) as caught:
            write_text_atomically(target, "the replacement that never lands")
    finally:
        os.fsync = real_fsync  # type: ignore[assignment]

    assert target.read_text() == original, "the existing memory was damaged"
    assert "UNCHANGED" in str(caught.value), str(caught.value)
    # And no debris was left where the indexer could find it.
    assert sorted(p.name for p in target.parent.iterdir()) == ["sailing.md"]


def test_a_failure_leaves_no_zero_byte_memory(tmp_path: Path) -> None:
    """capture's shape: a NEW file, interrupted. The name must not exist at all.

    A 0-byte `.md` under a real name is the wrong-memory hazard — the indexer
    walks `**/*.md` and a partial file with valid frontmatter reads as content.
    """
    target = tmp_path / "knowledge" / "new.md"
    target.parent.mkdir(parents=True)

    real_fsync = os.fsync

    def _fail(fd: int) -> None:
        raise OSError("simulated failure")

    os.fsync = _fail  # type: ignore[assignment]
    try:
        with pytest.raises(AtomicWriteError):
            write_text_atomically(target, "---\nname: New\n---\n\nbody\n")
    finally:
        os.fsync = real_fsync  # type: ignore[assignment]

    assert not target.exists(), "an interrupted capture left a file behind"
    assert list(target.parent.iterdir()) == [], "staging debris was left behind"


def test_the_retry_after_a_failure_works(tmp_path: Path) -> None:
    """A failure must not leave state that makes the next attempt fail too.

    The first version of the archive fix left a claimed name behind, so the
    retry chose a different one and the user ended up with two files.
    """
    target = tmp_path / "topics" / "retry.md"
    target.parent.mkdir(parents=True)
    real_fsync = os.fsync

    def _fail(fd: int) -> None:
        raise OSError("simulated failure")

    os.fsync = _fail  # type: ignore[assignment]
    try:
        with pytest.raises(AtomicWriteError):
            write_text_atomically(target, "first attempt")
    finally:
        os.fsync = real_fsync  # type: ignore[assignment]

    write_text_atomically(target, "second attempt")
    assert target.read_text() == "second attempt"
    assert sorted(p.name for p in target.parent.iterdir()) == ["retry.md"]


def test_the_staged_temporary_is_invisible_to_the_indexer_walk(tmp_path: Path) -> None:
    """A leading dot is NOT enough, which is measurable and surprising.

    ``Path.glob("**/*.md")`` matches dotfiles, and that glob IS the indexer's
    walk — so a staged file named ``.partial-x.md`` would be indexed as a
    memory. The ``.tmp`` suffix is what keeps it out.
    """
    home = tmp_path / "home"
    (home / "topics").mkdir(parents=True)
    # Prove the premise rather than asserting it: a dotted .md IS matched.
    (home / "topics" / ".dotted.md").write_text("x")
    assert list(home.glob("**/*.md")), "the premise is wrong; revisit the suffix choice"

    captured: list[str] = []
    real_replace = os.replace

    def _capture(src, dst, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(Path(src).name)
        return real_replace(src, dst, **kwargs)

    os.replace = _capture  # type: ignore[assignment]
    try:
        write_text_atomically(home / "topics" / "real.md", "---\nname: R\n---\n\nbody\n")
    finally:
        os.replace = real_replace  # type: ignore[assignment]

    assert captured, "nothing was published through os.replace"
    staged_name = captured[0]
    assert staged_name.startswith(".partial-"), staged_name
    assert not staged_name.endswith(".md"), staged_name
    # The staged name, had it survived, must not be matched by the walk.
    probe = home / "topics" / staged_name
    probe.write_text("x")
    matched = {p.name for p in home.glob("**/*.md")}
    assert staged_name not in matched, f"{staged_name} is visible to the indexer walk"
    probe.unlink()


def test_the_mode_of_an_existing_memory_is_preserved(tmp_path: Path) -> None:
    """mkstemp creates 0600, so a naive replace silently tightens permissions.

    On a memory home shared over a network mount or between accounts that is a
    real change, and an invisible one.
    """
    target = tmp_path / "topics" / "modes.md"
    target.parent.mkdir(parents=True)
    target.write_text("original")
    target.chmod(0o644)

    write_text_atomically(target, "replacement")
    assert target.stat().st_mode & 0o777 == 0o644, oct(target.stat().st_mode)
