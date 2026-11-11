"""Every writer command reports an index refusal as an error, not a traceback.

QA, 2026-11-11: on a model switch `rekol capture` refused correctly and then
died with about twenty lines of Python traceback, with the two remedies below
the stack. The refusal is new in #216 — before it, those commands could not
refuse at all — and the release notes advertise it, so the message is part of
the feature rather than a detail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from rekol.cli_common import index_refusals_reported
from rekol.safety import RealIndexClobberError
from rekol.store import IndexModelMismatchError

REAL_MODEL = "BAAI/bge-small-en-v1.5"
OTHER_REAL = "sentence-transformers/all-MiniLM-L6-v2"


def _refusals() -> list[Exception]:
    return [
        IndexModelMismatchError(Path("/x/index.db"), REAL_MODEL, 384, OTHER_REAL, 384),
        RealIndexClobberError(Path("/x/index.db"), REAL_MODEL, "test-hashing"),
    ]


@pytest.mark.parametrize("error", _refusals(), ids=["model-mismatch", "clobber"])
def test_the_helper_reports_and_exits_without_a_traceback(error: Exception) -> None:
    """Both refusal types, caught, printed, exit 1."""
    runner = CliRunner()

    import click

    @click.command()
    def victim() -> None:
        with index_refusals_reported():
            raise error

    result = runner.invoke(victim, [])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), (
        f"a {type(result.exception).__name__} escaped to the user"
    )
    assert "error:" in result.output
    assert str(error).splitlines()[0][:40] in result.output


def test_the_helper_can_add_a_line_without_losing_the_remedy() -> None:
    """`capture`'s extra line must not displace the remedies."""
    runner = CliRunner()

    import click

    error = IndexModelMismatchError(Path("/x/index.db"), REAL_MODEL, 384, OTHER_REAL, 384)

    @click.command()
    def victim() -> None:
        with index_refusals_reported("Your memory was saved to /m/n.md but not indexed."):
            raise error

    result = runner.invoke(victim, [])
    assert result.exit_code == 1
    assert "Your memory was saved to /m/n.md but not indexed." in result.output
    assert "rekol index rebuild" in result.output, "the remedy was pushed out"
    assert f"set embedding_model back to {REAL_MODEL!r}" in result.output


def test_every_writer_command_uses_the_helper() -> None:
    """NO WRITER MAY FORGET IT, which is why this is a scan and not a habit.

    Same shape as the `IndexStore` construction enumeration: the commands that
    reach a guarded write are the ones that can refuse, so each must report it.

    `bootstrap` and `propose` are deliberately NOT here, and that was measured
    rather than assumed: they construct a store and read through it (recall,
    dedupe) without calling a guarded write, so a handler there would describe
    a path that does not exist. `index update` has its own pre-flight
    `check_model_identity` with an explicit exit, which predates the helper.
    """
    source_dir = Path(__file__).resolve().parent.parent / "src" / "rekol"
    writers = {
        "cli_capture.py": "capture",
        "cli_confidence.py": "confirm / flag-suspect",
        "cli_invalidate.py": "invalidate",
    }
    missing = []
    for filename, label in writers.items():
        text = (source_dir / filename).read_text(encoding="utf-8")
        if "index_refusals_reported" not in text:
            missing.append(f"{filename} ({label})")
    assert not missing, (
        "these writer commands can refuse a write but do not report it as an error:\n  "
        + "\n  ".join(missing)
    )

    # And the ones that call `.update()` must do it INSIDE the context manager,
    # not merely import it.
    for filename in writers:
        text = (source_dir / filename).read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            if ".update()" in line and "def " not in line:
                window = "\n".join(text.splitlines()[max(0, line_number - 8) : line_number])
                assert "index_refusals_reported" in window, (
                    f"{filename}:{line_number} writes outside the refusal reporter:\n{line}"
                )


def test_capture_says_the_memory_was_saved_when_indexing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA's case, end to end: the file is written BEFORE the index write.

    So "refused" must not read as "lost". `capture` writes the memory, the
    indexing is refused, and the message has to say which happened.
    """
    from rekol.cli_capture import main as capture_main

    home = tmp_path / "home"
    (home / "topics").mkdir(parents=True)
    (home / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")
    monkeypatch.setenv("REKOL_HOME", str(home))
    for var in ("MEMORY_HOME", "REKOL_INDEX_DIR", "REKOL_ARCHIVE_DIR", "REKOL_TOOLS_HOME"):
        monkeypatch.delenv(var, raising=False)

    # Refuse at the chokepoint, the way a model switch does.
    from rekol.store import IndexStore

    def refuse(self: IndexStore) -> None:
        raise IndexModelMismatchError(self.db_path, REAL_MODEL, 384, "test-hashing", 384)

    monkeypatch.setattr(IndexStore, "_assert_write_allowed", refuse)

    result = CliRunner().invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "note.md",
            "--name",
            "note",
            "--description",
            "a note",
            "--body",
            "some body text",
        ],
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), (
        f"a {type(result.exception).__name__} escaped out of capture"
    )
    assert "was saved to" in result.output, result.output
    assert "not indexed" in result.output, result.output
    assert "rekol index rebuild" in result.output, result.output
    written = list((home / "topics").glob("*.md"))
    assert written, "capture refused AND lost the memory — the message would be a lie"
