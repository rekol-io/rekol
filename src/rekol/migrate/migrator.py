"""Orchestrator: discovery → classification → write → archive → pointer.

Operates on ONE source dir at a time (``migrate_dir``).  The CLI layer wraps
multiple calls for ``auto`` mode.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
import yaml

from rekol.config import resolve_index_dir
from rekol.migrate.archive import (
    ARCHIVE_DIR_NAME,
    archive_file,
    write_retirement_pointer,
)
from rekol.migrate.classify import Classification, classify_file
from rekol.migrate.discover import (
    MIGRATION_MARKER_NAME,
    discover_files_in_dir,
    has_migration_marker,
)


def _nested_md_files(source_dir: Path) -> list[Path]:
    """Every ``.md`` file below ``source_dir`` that discovery will never see.

    Discovery is deliberately non-recursive, so these are neither imported nor
    archived. They are reported rather than migrated: importing them would
    change discovery semantics, which is a design decision and not a fix.
    """
    return [
        path
        for path in sorted(source_dir.rglob("*.md"))
        if path.parent != source_dir and ARCHIVE_DIR_NAME not in path.parts
    ]


def _nested_file_warning(source_dir: Path, nested: list[Path], *, dry_run: bool) -> str:
    """The warning for nested files, with a remedy that actually works.

    The previous wording said only "Move them up one level and re-run". QA
    followed it literally and nothing happened: ``discover_auto_memory_sources``
    filters out retired directories, so a re-run never looks at this one again.
    Documenting a remedy that cannot work is the #166 failure, so the marker is
    named here — that is what makes the directory eligible again.

    ``dry_run`` changes the tense, and it is not cosmetic: a dry run retires
    nothing, so saying "this directory is now retired" would be the same kind of
    false claim this warning exists to prevent. In dry-run the useful advice is
    also different — move the files BEFORE committing and no marker needs
    deleting at all.
    """
    listed = ", ".join(str(path.relative_to(source_dir)) for path in nested)
    opening = (
        f"{len(nested)} file(s) in subdirectories will NOT be imported, will NOT be "
        f"archived, and will NOT be searchable"
        if dry_run
        else f"{len(nested)} file(s) in subdirectories were NOT imported, are NOT in "
        f"the archive, and are NOT searchable"
    )
    if dry_run:
        remedy = (
            f"Nothing has been retired yet, so the simplest fix is to move them up "
            f"into {source_dir} BEFORE running --commit. If you commit first, you "
            f"will also have to delete {MIGRATION_MARKER_NAME} there to make the "
            f"directory eligible again."
        )
    else:
        remedy = (
            f"This directory is now retired, so a later run skips it: to migrate "
            f"them, move them up into {source_dir} AND delete "
            f"{MIGRATION_MARKER_NAME} there, then re-run — or import them by hand."
        )
    return (
        f"{opening} — migration does not recurse: {listed}. They stay where they "
        f"are on disk. {remedy}"
    )


# Map singular layer name (in Classification.layer) to on-disk dir name.
LAYER_DIR_MAP = {
    "always": "always",
    "when": "when",
    "topic": "topics",
    "knowledge": "knowledge",
}


@dataclass
class MigrationReport:
    """Tally of migration outcomes, including per-reason skip counts and errors."""

    migrated: int = 0
    would_migrate: int = 0  # dry-run only
    by_heuristic: int = 0
    by_llm: int = 0
    # Counted separately from by_heuristic (#166): a defaulted file is one we
    # learned nothing about, not one we classified. Folding it into by_heuristic
    # is what let a total failure print as a successful migration.
    by_defaulted: int = 0
    archived: int = 0
    # Where a legacy `MEMORY.md` was moved to, or WOULD be moved in dry-run.
    # It needs its own field because `discover_files_in_dir` skips that name, so
    # it is never one of `files` and never counted in `archived` — which is
    # exactly how its deletion stayed invisible in both the dry-run preview and
    # the final output (QA, 2026-11-06).
    legacy_pointer_archived: Path | None = None
    # Set when a `MEMORY.md` was found and deliberately LEFT ALONE, which is the
    # `migrate repo` contract. Distinct from `legacy_pointer_archived is None`,
    # which only means there was no such file — the CLI has to tell a user that
    # their repo's own MEMORY.md was spared, not merely stay quiet about it.
    legacy_pointer_left_in_place: Path | None = None
    # Every source file a dry run WOULD move into the archive, named so the
    # preview reveals what it is about to touch. QA pointed `migrate repo` at a
    # git work-tree root and --commit moved the repo's tracked README.md and
    # CLAUDE.md; dry-run had reported only "would migrate 2 (defaulted=2)" and
    # named neither, so previewing could not reveal it.
    would_move: list[Path] = field(default_factory=list)
    # Every source file actually MOVED into the archive on a commit run. Needed
    # so a run that was explicitly allowed to touch a repository root can name
    # what it took, rather than leaving the user to read `git status` for it.
    moved: list[Path] = field(default_factory=list)
    skipped_retired: int = 0
    skipped_missing: int = 0
    skipped_duplicate: int = 0  # body-hash matched an already-migrated file
    errors: list[str] = field(default_factory=list)
    # Diagnostics that are NOT failures. `errors` now means "this file was not
    # imported" and is what blocks retirement — so an LLM-unavailable note, which
    # still produced an imported (if poorly described) file, must not live there.
    # Keeping them in one list made the two defaulting paths disagree about the
    # durable outcome: --no-llm retired the directory, LLM-unavailable did not,
    # for identical results on disk.
    warnings: list[str] = field(default_factory=list)


def _body_hash(body: str) -> str:
    """SHA-256 of the body text, whitespace-normalized.

    Used to dedupe legacy files whose bodies are byte-identical but whose
    auto-generated frontmatter (e.g., ``description: Legacy memory from <slug>``)
    differs only because of the source path.  Same body → same hash → migrate
    only once.
    """
    normalized = "\n".join(line.rstrip() for line in body.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _existing_body_hashes(memory_home: Path) -> set[str]:
    """Return the set of body hashes for every file already in ``memory_home``."""
    hashes: set[str] = set()
    for layer in LAYER_DIR_MAP.values():
        layer_dir = memory_home / layer
        if not layer_dir.is_dir():
            continue
        for entry in layer_dir.iterdir():
            if entry.suffix != ".md" or not entry.is_file():
                continue
            try:
                post = frontmatter.load(entry)
                hashes.add(_body_hash(post.content))
            except Exception:  # noqa: BLE001 — best-effort; skip unreadable
                continue
    return hashes


def _serialize_memory_file(classification: Classification) -> str:
    """Render a Classification as a markdown string with YAML frontmatter."""
    fm = yaml.safe_dump(
        classification.frontmatter,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{fm}\n---\n\n{classification.body.strip()}\n"


def _resolve_target_path(
    memory_home: Path,
    classification: Classification,
) -> Path:
    """Return the unique target path.  Appends -N suffix if a file already exists."""
    layer_dir = memory_home / LAYER_DIR_MAP[classification.layer]
    layer_dir.mkdir(parents=True, exist_ok=True)
    base = classification.target_filename
    stem = base[:-3] if base.endswith(".md") else base
    candidate = layer_dir / f"{stem}.md"
    counter = 2
    while candidate.exists():
        candidate = layer_dir / f"{stem}-{counter}.md"
        counter += 1
    return candidate


def migrate_dir(  # noqa: C901  # complex but stable; refactor tracked separately
    source_dir: Path,
    *,
    memory_home: Path,
    dry_run: bool,
    allow_llm: bool,
    archive_legacy_pointer: bool = True,
) -> MigrationReport:
    """Migrate every legacy .md file in ``source_dir`` into ``memory_home``.

    Skips entirely if ``source_dir/MEMORY.md`` is already a retirement pointer
    or if ``source_dir`` does not exist.

    When ``dry_run`` is True, no filesystem writes occur; ``would_migrate`` is
    populated but ``migrated`` stays 0.

    ``archive_legacy_pointer`` False leaves any ``MEMORY.md`` strictly alone and
    records it in ``legacy_pointer_left_in_place``. The default True is the
    auto-memory contract: Claude Code's autoMemory injects
    ``~/.claude/projects/<slug>/memory/MEMORY.md`` into every session, so a
    migrated directory must not leave one there. Outside that tree the file is
    quite possibly a tracked project file — rekol ships one in its own template
    — so archiving it would move tracked content and drop an untracked
    ``old-memory-archive/`` into someone's repository.
    """
    report = MigrationReport()

    if not source_dir.is_dir():
        report.skipped_missing = 1
        return report

    if has_migration_marker(source_dir):
        report.skipped_retired = 1
        return report

    files = discover_files_in_dir(source_dir)
    if not files:
        # Still write the retirement pointer so re-runs skip cleanly. A directory
        # holding ONLY `MEMORY.md` lands here — which is how that file used to be
        # deleted with nothing imported and exit 0.
        #
        # G2 (QA, 2026-11-06): the nested-file warning used to sit further down,
        # below this return, so a directory whose ONLY .md files were nested
        # retired in silence with `migrated 0` and exit 0 — the summary line
        # looked like there had been nothing to do. These are the likeliest
        # shapes to be overlooked, so the warning has to be raised here too.
        nested = _nested_md_files(source_dir)
        if nested:
            report.warnings.append(_nested_file_warning(source_dir, nested, dry_run=dry_run))

        legacy_pointer = source_dir / "MEMORY.md"
        if not archive_legacy_pointer:
            if legacy_pointer.exists():
                report.legacy_pointer_left_in_place = legacy_pointer
        if dry_run:
            if legacy_pointer.exists() and archive_legacy_pointer:
                report.legacy_pointer_archived = source_dir / ARCHIVE_DIR_NAME / legacy_pointer.name
        else:
            outcome = write_retirement_pointer(
                source_dir,
                memory_home=memory_home,
                archive_legacy_pointer=archive_legacy_pointer,
            )
            report.legacy_pointer_archived = outcome.legacy_pointer_archived
            if outcome.error:
                report.errors.append(outcome.error)
        return report

    # INDEX.md now lives in the local-only cache (outside $REKOL_HOME); fall
    # back to the legacy in-tree locations for compatibility with already-
    # deployed setups that haven't been relocated yet.
    index_candidates = [
        resolve_index_dir(memory_home) / "INDEX.md",
        memory_home / ".index" / "INDEX.md",
        memory_home / "INDEX.md",
    ]
    index_context = ""
    for candidate in index_candidates:
        if candidate.exists():
            index_context = candidate.read_text(encoding="utf-8", errors="replace")
            break

    # Snapshot of body hashes already present in memory_home — drives dedup
    # below.  Updated as we migrate so duplicates within this batch also skip.
    seen_hashes = _existing_body_hashes(memory_home)

    for lf in files:
        try:
            c = classify_file(lf, index_context=index_context, allow_llm=allow_llm)
        except Exception as exc:  # noqa: BLE001 — log and continue
            report.errors.append(f"classify failed for {lf.source_path}: {exc}")
            continue

        body_hash = _body_hash(c.body)
        if body_hash in seen_hashes:
            # Same body already migrated — archive the duplicate but don't write.
            report.skipped_duplicate += 1
            if dry_run:
                # A duplicate is archived too, so a dry run would move it.
                report.would_move.append(lf.source_path)
            if not dry_run:
                try:
                    archive_file(lf)
                    report.moved.append(lf.source_path)
                    report.archived += 1
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"archive failed for duplicate {lf.source_path}: {exc}")
            continue

        if dry_run:
            report.would_migrate += 1
            report.would_move.append(lf.source_path)
            if c.method == "heuristic":
                report.by_heuristic += 1
            elif c.method == "defaulted":
                report.by_defaulted += 1
                if c.fallback_reason:
                    report.warnings.append(f"{lf.source_path}: {c.fallback_reason}")
            else:
                report.by_llm += 1
            seen_hashes.add(body_hash)
            continue

        target = _resolve_target_path(memory_home, c)
        try:
            target.write_text(_serialize_memory_file(c))
            archive_file(lf)
            report.moved.append(lf.source_path)
            seen_hashes.add(body_hash)
            report.migrated += 1
            report.archived += 1
            if c.method == "heuristic":
                report.by_heuristic += 1
            elif c.method == "defaulted":
                report.by_defaulted += 1
                if c.fallback_reason:
                    report.warnings.append(f"{lf.source_path}: {c.fallback_reason}")
            else:
                report.by_llm += 1
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"write/archive failed for {lf.source_path}: {exc}")

    # Write the migration marker if anything was processed — even a fully-failed
    # batch.  Without this, a source dir whose every file fails classification
    # (e.g., corrupted YAML on every file) would be retried on every install,
    # accumulating identical errors forever.  Trade-off: the operator must read
    # the install journal / report.errors to discover the failure rather than
    # noticing repeated retry attempts.  Acceptable, because errors counts ARE
    # surfaced in the install output.  The dir is NOT marked when zero files
    # were even attempted (handled by the earlier early-return when files == [])
    # or when dry-running.
    # RETIREMENT INVARIANT (#166, tightened after external review):
    #
    #   A directory may be retired only when EVERY discovered file reached a
    #   terminal, safe state — imported (by any method) or skipped as a duplicate.
    #   A single unimported file blocks retirement for the whole directory.
    #
    # The first attempt at this fix only removed `len(errors) > 0` from the
    # condition, which narrowed the bug from "every file failed" to "at least one
    # file succeeded" — it did not fix it. With `migrated > 0` alone, one good file
    # tombstones the directory and every failed sibling is abandoned permanently,
    # because the next run prints "skipped — already retired" and never retries.
    #
    # Successful originals have already been MOVED to old-memory-archive/, so
    # leaving a mixed-outcome directory unretired means the next run naturally sees
    # only the files that still need work. Re-running is cheap and visible; an
    # unnoticed tombstone is neither.
    #
    # Defaulted files (no frontmatter to go on, and either no LLM or an unavailable
    # one) are treated as IMPORTED, not failed: the body is preserved and
    # searchable, it is merely poorly described. They do not block retirement —
    # blocking on them would mean every `--no-llm` install (the default path)
    # retries forever. What they must NOT do is carry a promise we cannot keep;
    # see the CLI warning, which no longer advertises a reclassification that has
    # no command behind it.
    # `errors` means UNIMPORTED. Diagnostics about imported-but-poorly-described
    # files live in `warnings` and deliberately do not block retirement.
    unimported = len(report.errors) > 0
    # `would_migrate` belongs here (G1, QA 2026-11-06). In dry-run `migrated`
    # stays 0 by design, so `progressed` was False and NOTHING below fired: no
    # MEMORY.md preview and no nested-file warning, in the commonest shape of
    # all (a MEMORY.md beside one topic file). It appeared to work only for a
    # directory with no topic files, which takes the early return above and has
    # its own preview — so the test covering that shape passed while the normal
    # path previewed nothing, and the one destructive step in the run was the
    # one step a user could not see coming.
    progressed = report.migrated > 0 or report.would_migrate > 0 or report.skipped_duplicate > 0
    retirable = progressed and not unimported
    # F7 (QA, 2026-11-06): discovery is deliberately NON-recursive, but retirement
    # is permanent — so a nested file was left on disk, never imported, and no
    # later run looks, while the marker claimed "original files are in
    # old-memory-archive/". That is the abandonment shape #166 set out to end,
    # surviving because the retirement invariant is "every DISCOVERED file" and
    # discovery defines what counts. Retire anyway (refusing would make
    # `migrate auto` retry forever with no way to progress) but name them, so the
    # user can act instead of silently losing track.
    if retirable:
        nested = _nested_md_files(source_dir)
        if nested:
            report.warnings.append(_nested_file_warning(source_dir, nested, dry_run=dry_run))

    legacy_pointer = source_dir / "MEMORY.md"
    if retirable and not archive_legacy_pointer and legacy_pointer.exists():
        report.legacy_pointer_left_in_place = legacy_pointer

    if not dry_run and retirable:
        outcome = write_retirement_pointer(
            source_dir,
            memory_home=memory_home,
            archive_legacy_pointer=archive_legacy_pointer,
        )
        report.legacy_pointer_archived = outcome.legacy_pointer_archived
        if outcome.error:
            # G4: reported as an error, which both blocks the exit-0 claim and
            # — because the marker was never written — leaves the directory
            # eligible for a later run. Previously this escaped as an uncaught
            # PermissionError traceback AFTER the marker had been written, so
            # the directory reported itself migrated and never retried.
            report.errors.append(outcome.error)
    elif dry_run and retirable:
        # Preview it. Dry-run reported "would migrate N" and never mentioned
        # MEMORY.md, so the one destructive step in the run was the one step a
        # user could not preview.
        if legacy_pointer.exists() and archive_legacy_pointer:
            report.legacy_pointer_archived = source_dir / ARCHIVE_DIR_NAME / legacy_pointer.name

    return report
