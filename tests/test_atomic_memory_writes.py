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
import stat
from pathlib import Path

import pytest

from rekol.atomic_write import AtomicWriteError, write_text_atomically

# EVERY module under src/rekol is scanned, with an explicit reviewed exemption
# below (product, 2026-11-07). The first version listed five modules and the PR
# text claimed a new command would be caught automatically — a claim wider than
# the check, which is the habit this project is trying to break.
#
# A module absent from both the clean set and the exemptions FAILS, so a new one
# has to be looked at before it passes. That is the whole mechanism: the list is
# a record of decisions, not a way to get green.
#
# WHAT THIS STILL CANNOT SEE, stated because nothing should claim more than it
# checks: a mode held in a variable (`open(p, mode)`), `os.open`, `os.fdopen`,
# `shutil.copy` onto a memory path, a write through a helper this test does not
# know about, and anything in a dependency. It reads the source, so it also
# cannot see a path computed at runtime — a `write_text` on a path that only
# sometimes lands in the memory home is reported either way.
EXEMPT_MODULES: dict[str, str] = {
    # Already stage-then-replace, which is the rule this test enforces; they
    # just do it inline rather than through the helper.
    "bootstrap.py": "stages a temporary and os.replaces it",
    "bootstrap_state.py": "stages a temporary and os.replaces it",
    "cli_resume.py": "settings.json is staged and replaced; the marker is in the cache",
    # Writes into the local-only index/cache directory. Rebuildable by
    # definition: losing one costs a reindex, not a memory.
    "include_indexer.py": "marker file in the index cache",
    "indexer.py": "INDEX.md and the skip manifest, both in the index cache",
    "release.py": "release-check state in the index cache",
    "cli_hooks.py": "per-session scratch files in the index cache",
    # Append-only journals. `open(..., "a")` does not truncate, so the hazard
    # this test exists for does not apply.
    "resume.py": "append-only freeze journal",
    # The transcript archive, which is rekol's own durable copy of session
    # JSONL rather than curated memory. Worth the same treatment eventually;
    # not this change.
    "sessions/archive.py": "transcript archive, not curated memory",
    # Writes the file the user asked for, at a path they named.
    "docs_convert/writer.py": "writes its explicit output path",
    # PENDING, NOT ABSOLVED. Both of these really do call `write_text` on this
    # branch, and the reasons that used to sit here — "prose in docstrings, not
    # calls" — were simply false (Codex, 2026-11-07). An exemption whose reason
    # is wrong is worse than no exemption: it tells the next reader to stop
    # looking, which is exactly what this test exists to prevent.
    #
    # `archive.py` writes the `.MIGRATED` marker and `migrator.py` writes the
    # imported memory, both by truncation, and both are what #224 replaces with
    # staged-and-replaced publication. #224 merges before this PR, so these come
    # out at the rebase rather than being fixed here — and not on anyone
    # remembering: `test_every_exemption_still_describes_a_real_module` fails on
    # a dead exemption, so the rebase cannot pass with them still listed.
    # Verified against #224's head (`387f514`): neither module has a truncating
    # write there, so both exemptions will go.
    "migrate/archive.py": "PENDING: write_text at archive.py:101 (the .MIGRATED marker), "
    "removed by #224 and due out of this list at the rebase",
    "migrate/migrator.py": "PENDING: write_text at migrator.py:218 (the imported memory), "
    "removed by #224 and due out of this list at the rebase",
}


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
        # `Path.open("w")` as well as the builtin, and `r+` as well as `w`:
        # both can shorten or rewrite an existing file in place. Added because
        # the control for this scan caught `p.open("w")` going unseen.
        is_builtin = isinstance(func, ast.Name) and func.id == "open"
        is_path_open = isinstance(func, ast.Attribute) and func.attr == "open"
        if is_builtin or is_path_open:
            mode = ""
            mode_arg = 1 if is_builtin else 0
            if len(node.args) > mode_arg and isinstance(node.args[mode_arg], ast.Constant):
                mode = str(node.args[mode_arg].value)
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = str(keyword.value.value)
            if any(flag in mode for flag in ("w", "a", "+")):
                label = "open" if is_builtin else "Path.open"
                found.append((node.lineno, f"{label}(..., {mode!r})"))
    return found


def _modules_with_truncating_writes() -> dict[str, list[tuple[int, str]]]:
    """Every module under ``src/rekol`` that opens a path by truncation."""
    found: dict[str, list[tuple[int, str]]] = {}
    for source_file in sorted(SOURCE_DIR.rglob("*.py")):
        offenders = _truncating_writes(ast.parse(source_file.read_text()))
        if offenders:
            found[str(source_file.relative_to(SOURCE_DIR))] = offenders
    return found


def test_no_unreviewed_module_writes_a_file_by_truncation() -> None:
    """THE RULE, over the whole package rather than a list of five modules.

    A module that truncates has to be either fixed or added to
    ``EXEMPT_MODULES`` with a reason — so a new command that writes a memory the
    old way fails here even though nobody thought to test it, which is the point:
    each of the sites this fixed was added by someone who did not know the rule
    existed.
    """
    offending = _modules_with_truncating_writes()
    unreviewed = {m: hits for m, hits in offending.items() if m not in EXEMPT_MODULES}
    assert not unreviewed, (
        "these modules write a file by truncation, which destroys the existing "
        "content before the replacement is written:\n"
        + "\n".join(
            f"  {module}: " + ", ".join(f"line {line} {what}" for line, what in hits)
            for module, hits in sorted(unreviewed.items())
        )
        + "\n  Use rekol.atomic_write.write_text_atomically, or add the module to "
        "EXEMPT_MODULES with the reason it is safe."
    )


def test_every_exemption_still_describes_a_real_module() -> None:
    """An exemption for a module that no longer truncates is a stale decision.

    Left in place it silently licenses a future write, which is how an allowlist
    turns into a hole.
    """
    offending = _modules_with_truncating_writes()
    for module, reason in EXEMPT_MODULES.items():
        assert (SOURCE_DIR / module).is_file(), f"{module} has moved; EXEMPT_MODULES is stale"
        assert reason.strip(), f"{module} is exempt with no reason given"
    stale = sorted(set(EXEMPT_MODULES) - set(offending))
    assert not stale, (
        "these modules no longer truncate anything, so their exemption is dead "
        f"and should be removed: {stale}"
    )


def test_the_commands_that_write_memories_are_not_exempt() -> None:
    """The five that #238 fixed must be clean, not excused.

    Without this, someone could make the suite pass by exempting them.
    """
    for module in (
        "cli_capture.py",
        "cli_confidence.py",
        "cli_invalidate.py",
        "cli_review.py",
        "cli_propose.py",
    ):
        assert module not in EXEMPT_MODULES, f"{module} must not be exempt"
        assert not _truncating_writes(ast.parse((SOURCE_DIR / module).read_text())), (
            f"{module} truncates again"
        )


def test_a_new_module_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the scan: a module nobody has reviewed must FAIL.

    Points the scan at a throwaway package containing one new module with one
    `write_text`, and requires it to be reported. Without this the scan could be
    passing because it walks nothing.
    """
    fake = tmp_path / "rekol"
    (fake / "sub").mkdir(parents=True)
    (fake / "brand_new_command.py").write_text(
        "from pathlib import Path\ndef run(p: Path) -> None:\n    p.write_text('x')\n"
    )
    (fake / "sub" / "nested.py").write_text(
        'def run(p):\n    with p.open("w") as h:\n        h.write("x")\n'
    )
    monkeypatch.setattr("tests.test_atomic_memory_writes.SOURCE_DIR", fake, raising=False)
    import tests.test_atomic_memory_writes as module_under_test

    monkeypatch.setattr(module_under_test, "SOURCE_DIR", fake)
    found = module_under_test._modules_with_truncating_writes()
    assert "brand_new_command.py" in found, found
    assert "sub/nested.py" in found, "a module in a subpackage was not walked"


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


def test_a_symlinked_memory_is_edited_through_the_link(tmp_path: Path) -> None:
    """os.replace acts on the NAME, so this had to be made explicit.

    Measured by product: replacing a symlinked memory deleted the link, left a
    regular file in the home holding the new text, and left the REAL file with
    the old text. A silent fork — the user's edit in a different place from the
    file they believe they edited. `Path.write_text` follows the link, so that
    worked before this module existed.
    """
    home = tmp_path / "home" / "topics"
    home.mkdir(parents=True)
    external = tmp_path / "elsewhere"
    external.mkdir()
    real = external / "real.md"
    real.write_text("the original body")
    link = home / "m.md"
    link.symlink_to(real)

    write_text_atomically(link, "the new body")

    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert real.read_text() == "the new body", "the real file was left stale"
    assert link.read_text() == "the new body"
    # No debris beside either file.
    assert sorted(p.name for p in home.iterdir()) == ["m.md"]
    assert sorted(p.name for p in external.iterdir()) == ["real.md"]


def test_a_symlink_inside_the_home_is_edited_through_the_link(tmp_path: Path) -> None:
    """The contained case, which is the ordinary one and must behave the same."""
    home = tmp_path / "home"
    (home / "topics").mkdir(parents=True)
    (home / "knowledge").mkdir()
    real = home / "knowledge" / "real.md"
    real.write_text("original")
    link = home / "topics" / "alias.md"
    link.symlink_to(real)

    write_text_atomically(link, "updated")

    assert link.is_symlink()
    assert real.read_text() == "updated"


def test_a_dangling_symlink_creates_its_target(tmp_path: Path) -> None:
    """Matching write_text, which creates the target rather than failing."""
    home = tmp_path / "home" / "topics"
    home.mkdir(parents=True)
    target = tmp_path / "elsewhere" / "missing.md"
    target.parent.mkdir(parents=True)
    link = home / "dangling.md"
    link.symlink_to(target)
    assert not target.exists()

    write_text_atomically(link, "---\nname: D\n---\n\nbody\n")

    assert target.read_text().endswith("body\n")
    assert link.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o644


def test_a_dangling_symlink_into_a_missing_directory_fails_cleanly(tmp_path: Path) -> None:
    """There is nowhere to stage, so it must be the one-sentence failure."""
    home = tmp_path / "home" / "topics"
    home.mkdir(parents=True)
    link = home / "nowhere.md"
    link.symlink_to(tmp_path / "no" / "such" / "dir" / "x.md")

    with pytest.raises(AtomicWriteError) as caught:
        write_text_atomically(link, "body")
    assert "UNCHANGED" in str(caught.value), str(caught.value)
    assert sorted(p.name for p in home.iterdir()) == ["nowhere.md"]


def test_the_mode_of_a_symlink_target_is_preserved(tmp_path: Path) -> None:
    """The mode that survives is the target's, not the link's."""
    home = tmp_path / "home" / "topics"
    home.mkdir(parents=True)
    real = tmp_path / "elsewhere" / "real.md"
    real.parent.mkdir(parents=True)
    real.write_text("original")
    real.chmod(0o640)
    (home / "m.md").symlink_to(real)

    write_text_atomically(home / "m.md", "updated")
    assert real.stat().st_mode & 0o777 == 0o640, oct(real.stat().st_mode)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this test relies on")
def test_a_read_only_directory_fails_with_the_message_not_a_traceback(tmp_path: Path) -> None:
    """`mkstemp` sat OUTSIDE the try, so this escaped as a raw OSError.

    That breaks the one promise this module makes: a failure says in one
    sentence whether the file survived. A traceback does not.
    """
    directory = tmp_path / "locked"
    directory.mkdir()
    target = directory / "m.md"
    target.write_text("original")
    directory.chmod(0o500)  # readable and traversable, not writable
    try:
        with pytest.raises(AtomicWriteError) as caught:
            write_text_atomically(target, "replacement")
    finally:
        directory.chmod(0o755)

    assert "UNCHANGED" in str(caught.value), str(caught.value)
    assert target.read_text() == "original"


@pytest.mark.parametrize(("umask_value", "expected"), [(0o077, 0o600), (0o022, 0o644)])
def test_a_new_file_follows_the_users_umask(
    tmp_path: Path, umask_value: int, expected: int
) -> None:
    """Forcing 0644 on a new memory was a security regression.

    Under umask 077 `Path.write_text` produces 0600, so `capture` kept a new
    memory private to the user. Staging with `mkstemp` and then `chmod 0644`
    made every new memory readable by every local account on the machine
    (product, 2026-11-07). The mode is the kernel's business now: the stage is
    created with 0666 and the umask is applied to it.

    Three shapes, because all three create a file that did not exist: a new
    memory (`capture`), a new proposal, and a dangling symlink's target. They
    are tested here rather than through each CLI because the scan above proves
    those commands have no other way to write — if one grows a second path, that
    test fails, not this one.
    """
    home = tmp_path / "home" / "topics"
    home.mkdir(parents=True)
    external = tmp_path / "elsewhere"
    external.mkdir()
    dangling_target = external / "linked.md"
    (home / "link.md").symlink_to(dangling_target)

    previous = os.umask(umask_value)
    try:
        write_text_atomically(home / "new-memory.md", "---\nname: N\n---\n\nbody\n")
        write_text_atomically(tmp_path / "proposal.md", "- [ ] a candidate\n")
        write_text_atomically(home / "link.md", "---\nname: L\n---\n\nbody\n")
    finally:
        os.umask(previous)

    for created in (home / "new-memory.md", tmp_path / "proposal.md", dangling_target):
        assert created.exists(), created
        assert created.stat().st_mode & 0o777 == expected, (
            f"{created.name} is {oct(created.stat().st_mode & 0o777)}, "
            f"expected {oct(expected)} under umask {oct(umask_value)}"
        )


@pytest.mark.parametrize("umask_value", [0o077, 0o022])
def test_an_existing_file_keeps_its_mode_whatever_the_umask(
    tmp_path: Path, umask_value: int
) -> None:
    """The counterpart: the umask must not silently re-permission an edit.

    Without this, following the umask everywhere would quietly tighten or
    loosen an existing memory the user had deliberately chmod'ed.
    """
    target = tmp_path / "existing.md"
    target.write_text("original")
    target.chmod(0o640)

    previous = os.umask(umask_value)
    try:
        write_text_atomically(target, "replacement")
    finally:
        os.umask(previous)

    assert target.stat().st_mode & 0o777 == 0o640, oct(target.stat().st_mode & 0o777)
    assert target.read_text() == "replacement"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this relies on")
@pytest.mark.parametrize("umask_value", [0o022, 0o077])
def test_the_stage_is_never_more_readable_than_the_file_it_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask_value: int
) -> None:
    """THE STAGE'S MODE IS CHECKED BEFORE ITS FIRST BYTE (Codex P1, product).

    A regression this module introduced: the stage was created `0666 & ~umask`
    and the target's mode copied on afterwards. Under umask 022 that wrote the
    whole content of a private `0600` memory into a **world-readable 0644**
    temporary, and a kill before the chmod left that file in the memory home.

    The probe is on the descriptor, which is the only way to ask the question
    product actually asked: `os.fstat` at the moment `write_text_atomically` is
    about to write, before any content exists. Checking the published file's
    mode would pass whatever the stage had been.
    """
    from rekol import atomic_write

    target = tmp_path / "private.md"
    target.write_text("---\nname: P\n---\n\nsecret\n")
    target.chmod(0o600)

    observed: dict[str, int] = {}
    real_fdopen = atomic_write.os.fdopen

    def observe_then_open(fd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        observed["stage"] = stat.S_IMODE(os.fstat(fd).st_mode)
        observed["size"] = os.fstat(fd).st_size
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(atomic_write.os, "fdopen", observe_then_open)
    previous = os.umask(umask_value)
    try:
        write_text_atomically(target, "---\nname: P\n---\n\nstill secret\n")
    finally:
        os.umask(previous)
        monkeypatch.undo()

    assert observed, "the probe never fired; this test proved nothing"
    assert observed["size"] == 0, "the probe ran after bytes were already written"
    # THE CLAIM: not one permission bit beyond what the final file will have.
    assert observed["stage"] & ~0o600 == 0, (
        f"the staged file was {oct(observed['stage'])} while holding the content "
        f"of a 0600 file under umask {oct(umask_value)}"
    )
    # And the published file keeps its exact mode, so a tight umask has not
    # quietly tightened it either.
    assert target.stat().st_mode & 0o777 == 0o600, oct(target.stat().st_mode & 0o777)
    assert target.read_text() == "---\nname: P\n---\n\nstill secret\n"


def test_persisting_include_scope_does_not_truncate_the_config(tmp_path, monkeypatch) -> None:
    """`rekol.config.yaml` is read, mutated and written back WHOLE.

    So a fault between the truncation and the write emptied it and lost keys
    that have nothing to do with include-scope — the user's embedding model,
    their archive settings, whatever else they had set. It is config rather than
    memory, which is why it was exempt at first; product ruled that a destroyed
    config is still a destroyed user file.
    """
    from rekol.config import persist_include_scope

    home = tmp_path / "home"
    home.mkdir()
    config_file = home / "rekol.config.yaml"
    config_file.write_text(
        "embedding_model: test-hashing\narchive_enabled: false\nupdate_check: false\n"
    )

    real_fsync = os.fsync

    def _fail(fd: int) -> None:
        raise OSError("simulated ENOSPC")

    monkeypatch.setattr(os, "fsync", _fail)
    with pytest.raises(AtomicWriteError):
        persist_include_scope(home, ["/some/dir"], [])
    monkeypatch.setattr(os, "fsync", real_fsync)

    text = config_file.read_text()
    assert "embedding_model: test-hashing" in text, "the user's other keys were lost"
    assert "archive_enabled: false" in text
    assert "update_check: false" in text

    # And the ordinary path still works, keeping those keys.
    persist_include_scope(home, ["/some/dir"], [])
    text = config_file.read_text()
    assert "embedding_model: test-hashing" in text
    assert "/some/dir" in text


def test_a_symlinked_config_is_written_through_and_the_link_survives(tmp_path: Path) -> None:
    """A dotfile-managed config keeps working, and that is a decision.

    `rekol.config.yaml -> ~/dotfiles/rekol.yaml` is what someone using a
    dotfile manager has. `os.replace` acts on the NAME, so publishing without
    following the link would leave a regular file in the home holding the new
    keys while the dotfile repo kept the old ones — the config silently detached
    from the one the user edits and version-controls.

    Codex asked whether this should be refused, as product ruled for MEMORY
    files reached through a link out of the home. Product answered no
    (2026-11-07): a link leaving the home is an anomaly for a memory and the
    normal case for a config, and refusing it would break a working setup.
    """
    from rekol.config import persist_include_scope

    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "dotfiles"
    elsewhere.mkdir()
    real_config = elsewhere / "rekol.yaml"
    real_config.write_text("embedding_model: test-hashing\narchive_enabled: false\n")
    link = home / "rekol.config.yaml"
    link.symlink_to(real_config)

    persist_include_scope(home, ["/some/dir"], ["/some/dir/secret"])

    # The LINK survives as a link, pointing where it did.
    assert link.is_symlink(), "the config was replaced by a regular file"
    assert link.resolve() == real_config.resolve()
    # The TARGET holds the new content, with the user's other keys intact.
    written = real_config.read_text()
    assert "/some/dir" in written, written
    assert "/some/dir/secret" in written, written
    assert "embedding_model: test-hashing" in written, written
    # And nothing was left beside the link in the home.
    assert sorted(p.name for p in home.iterdir()) == ["rekol.config.yaml"], sorted(
        p.name for p in home.iterdir()
    )
