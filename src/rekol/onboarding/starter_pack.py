"""Starter-pack seeding for ``rekol init`` — pure, no prompts, idempotent.

The bundled ``template/`` directory (REKOL.md + ``always``/``when``/``topics``/
``knowledge`` ``*.example`` files) is the starter pack a fresh install begins
from. ``install.sh`` already seeds it on a *first* install; this module is the
Python equivalent ``rekol init`` calls so the starter-pack step is re-runnable
from inside the tool (Path B always-on, Path A opt-in) without shelling out.

Seeding is COPY-IF-ABSENT, i.e. **gap-fill** (#60): an existing home file is
never overwritten, so a re-run is a safe no-op and a user's hand-edits survive,
and a partially-populated home keeps every real file it already has while only
the *missing* layers receive their directive scaffold. The ``.example`` suffix
is stripped on copy (same convention ``install.sh`` uses), so what lands on disk
is the real ``identity.md`` / ``REKOL.md`` the runtime reads.

The scaffolds themselves are inert, generic *directives* — each layer file tells
the assistant what to learn and record there (#60), carrying no example fact-data
the model could recall as truth. T1 owns wiring the copy into the init flow.
"""

from __future__ import annotations

import importlib.resources
import shutil
from pathlib import Path


def find_template_dir() -> Path | None:
    """Locate the bundled ``template/`` directory, or ``None`` if not found.

    The template ships as **package data** inside the installed ``rekol`` package
    (``rekol/template``), so it resolves for BOTH an editable install and a built
    wheel. The previous repo-root lookup (``parents[3]/template``) only existed in
    an editable/source checkout and vanished off an installed wheel — ``template/``
    was not declared package data (#56). We resolve through ``importlib.resources``
    and cast to a concrete ``Path``: rekol always installs unzipped on disk, so the
    filesystem walk in :func:`seed_starter_pack` keeps working. Returns ``None``
    (rather than raising) when the directory is absent so the caller degrades
    gracefully instead of crashing onboarding.
    """
    try:
        resource = importlib.resources.files("rekol") / "template"
    except (ModuleNotFoundError, TypeError):
        return None
    template = Path(str(resource))
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
