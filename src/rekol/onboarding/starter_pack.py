"""Starter-pack seeding for ``rekol init`` — pure, no prompts, idempotent.

The bundled ``template/`` directory (REKOL.md + ``always``/``when``/``topics``/
``knowledge`` ``*.example`` files) is the starter pack a fresh install begins
from. ``install.sh`` already seeds it on a *first* install; this module is the
Python equivalent ``rekol init`` calls so the starter-pack step is re-runnable
from inside the tool (Path B always-on, Path A opt-in) without shelling out.

Seeding is COPY-IF-ABSENT: an existing home file is never overwritten, so a
re-run is a safe no-op and a user's hand-edits survive. The ``.example`` suffix
is stripped on copy (same convention ``install.sh`` uses), so what lands on disk
is the real ``identity.md`` / ``REKOL.md`` the runtime reads. T4 (#42) owns the
*content* of the template; T1 only owns wiring the copy into the init flow.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def find_template_dir() -> Path | None:
    """Locate the bundled ``template/`` directory, or ``None`` if not found.

    The template ships at the repository root (``<root>/template``), one level
    above ``src/`` — i.e. two parents up from this package. We resolve relative
    to this module rather than the cwd so init works from any working directory.
    Returns ``None`` (rather than raising) when the directory is absent — e.g. a
    packaging layout that did not vendor it — so the caller degrades gracefully
    instead of crashing onboarding.
    """
    # rekol/onboarding/starter_pack.py -> rekol/onboarding -> rekol -> src -> root
    repo_root = Path(__file__).resolve().parents[3]
    template = repo_root / "template"
    return template if template.is_dir() else None


def _stripped_relative_dest(rel: Path) -> Path:
    """Map a template-relative path to its on-disk name (drops a .example suffix)."""
    if rel.suffix == ".example":
        return rel.with_suffix("")  # foo.md.example -> foo.md
    return rel


def seed_starter_pack(template_dir: Path, memory_home: Path) -> list[Path]:
    """Copy every template file missing from ``memory_home``; return what was created.

    Walks ``template_dir`` recursively. For each file, computes its destination
    under ``memory_home`` with any ``.example`` suffix stripped, and copies it
    ONLY when the destination does not already exist — an existing file (template
    seed or user-authored) is left untouched. Parent directories are created as
    needed.

    Returns the list of destination paths actually created, so the caller can
    report "seeded N files" or "nothing to do" (idempotent re-run).
    """
    created: list[Path] = []
    for src in sorted(template_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(template_dir)
        dest = memory_home / _stripped_relative_dest(rel)
        if dest.exists():
            # Never clobber existing content — makes the step re-runnable and
            # preserves both prior seeds and the user's own edits.
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        created.append(dest)
    return created
