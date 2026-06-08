"""``rekol include`` — govern the external knowledge dirs in indexing scope.

The thin CLI surface over T8's :mod:`rekol.include_scope` engine — the T1↔T8
seam the assistant-led onboarding (and a human at the terminal) drives to answer
the spec's "Include = scope, not snapshot-import" step:

  * ``rekol include discover <root>`` — list candidate knowledge dirs under a
    root, ranked by ``.md`` count (the coverage signal the onboarding UI ranks
    on), auto-junk-filtered. Read-only; ``--json`` for the skill to consume.
  * ``rekol include show`` — print the currently-persisted include-scope.
    Read-only.
  * ``rekol include add <dir>...`` — add directories to ``include_dirs`` so a dir
    included once auto-indexes its new files forever (governed by the deny-list).
  * ``rekol include remove <dir>...`` — drop directories from ``include_dirs``.
  * ``rekol include deny <glob>...`` — add deny globs ("exclude a folder ->
    everything under it out"). The Custom exclude-list persisted as a deny-list.
  * ``rekol include allow <glob>...`` — remove deny globs.

This command only *governs scope* — it never converts or indexes. The conversion
into searchable transcripts happens on the next ``rekol session-index`` run
(``rekol.include_indexer``), so a dir included once stays indexed on its own.
Writes round-trip through :func:`rekol.config.persist_include_scope`, which
preserves every other config key.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from rekol.config import load_config, persist_include_scope
from rekol.include_scope import discover_knowledge_dirs


def _resolve_dir_arg(raw: str) -> str:
    """Normalise a user-supplied dir path to the absolute string we persist.

    ~ is expanded and the path is made absolute so what lands in the config is
    stable regardless of the process's cwd — matching ``resolved_include_dirs``'
    own expansion so ``add`` then ``show`` round-trip to the same string.
    """
    return str(Path(raw).expanduser().resolve())


@click.group(name="include")
def main() -> None:
    """Govern which external knowledge dirs are in REKOL's indexing scope."""


@main.command(name="discover")
@click.argument("root", type=click.Path(file_okay=False))
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the discovered dirs as a JSON array (path/md_count/text_count) so "
    "the onboarding assistant can rank and present them programmatically.",
)
@click.option(
    "--max-files",
    type=int,
    default=None,
    help="Cap how many files the discovery walk inspects (default: a bounded scan "
    "so a huge tree never hangs the first-run flow).",
)
def discover(root: str, as_json: bool, max_files: int | None) -> None:
    """List candidate knowledge dirs under ROOT, ranked by .md count (junk-filtered).

    Read-only: surfaces what *could* be included so the user (or the onboarding
    assistant) can pick. Silent-skip per the spec — an empty/missing root prints a
    calm "no knowledge files found", never "Found 0 dirs, include them?".
    """
    root_path = Path(root).expanduser()
    kwargs = {} if max_files is None else {"max_files": max_files}
    discovered = discover_knowledge_dirs(root_path, **kwargs)

    if as_json:
        click.echo(
            json.dumps(
                [
                    {"path": str(d.path), "md_count": d.md_count, "text_count": d.text_count}
                    for d in discovered
                ]
            )
        )
        return

    if not discovered:
        # Silent-skip: a calm note, not a "Found 0 …?" prompt.
        click.echo(f"No knowledge files found under {root_path}.")
        return

    click.echo(f"Knowledge dirs under {root_path} (ranked by markdown count):")
    for d in discovered:
        # The .md count is the headline coverage signal; show text_count too so a
        # dir of .txt/.py notes (md_count 0) still reads as having content.
        click.echo(f"  {d.path}  ({d.md_count} md, {d.text_count} files)")
    click.echo("(you can change this anytime — add with `rekol include add <dir>`)")


@main.command(name="show")
def show() -> None:
    """Print the currently-persisted include-scope (dirs + deny globs)."""
    cfg = load_config()
    if not cfg.include_dirs and not cfg.include_deny:
        click.echo(
            "No include-scope configured yet — add a knowledge dir with "
            "`rekol include add <dir>` (you can change this anytime)."
        )
        return
    if cfg.include_dirs:
        click.echo("Included dirs:")
        for raw in cfg.include_dirs:
            click.echo(f"  {raw}")
    else:
        click.echo("Included dirs: (none)")
    if cfg.include_deny:
        click.echo("Deny globs (excluded from scope):")
        for glob in cfg.include_deny:
            click.echo(f"  {glob}")


@main.command(name="add")
@click.argument("dirs", nargs=-1, required=True, type=click.Path(exists=True, file_okay=False))
def add(dirs: tuple[str, ...]) -> None:
    """Add one or more DIRS to the include-scope (persisted; idempotent).

    Click's ``exists=True`` guards against persisting a bad scope. Adding an
    already-included dir is a no-op (de-duped) so re-running is safe.
    """
    cfg = load_config()
    current = list(cfg.include_dirs)
    added: list[str] = []
    for raw in dirs:
        resolved = _resolve_dir_arg(raw)
        # De-dupe against the resolved form of every existing entry so a ~ vs
        # absolute spelling of the same dir is not stored twice.
        existing_resolved = {_resolve_dir_arg(d) for d in current}
        if resolved in existing_resolved:
            continue
        current.append(resolved)
        added.append(resolved)
    persist_include_scope(cfg.memory_home, current, list(cfg.include_deny))
    if added:
        for path in added:
            click.echo(f"Included {path}")
        click.echo(
            "Its new files auto-index on the next `rekol session-index` run "
            "(you can change this anytime)."
        )
    else:
        click.echo("Already in scope — nothing to add.")


@main.command(name="remove")
@click.argument("dirs", nargs=-1, required=True, type=click.Path(file_okay=False))
def remove(dirs: tuple[str, ...]) -> None:
    """Remove one or more DIRS from the include-scope (calm no-op if absent)."""
    cfg = load_config()
    # Compare on the resolved form so a ~ vs absolute spelling still matches what
    # was persisted. A dir that isn't in scope is silently skipped (no crash).
    to_drop = {_resolve_dir_arg(d) for d in dirs}
    kept: list[str] = []
    removed: list[str] = []
    for raw in cfg.include_dirs:
        if _resolve_dir_arg(raw) in to_drop:
            removed.append(raw)
        else:
            kept.append(raw)
    persist_include_scope(cfg.memory_home, kept, list(cfg.include_deny))
    if removed:
        for path in removed:
            click.echo(f"Removed {path} from scope")
    else:
        click.echo("Not in scope — nothing to remove.")


@main.command(name="deny")
@click.argument("globs", nargs=-1, required=True)
def deny(globs: tuple[str, ...]) -> None:
    """Add deny GLOBS to the scope's deny-list (idempotent).

    A bare segment like ``vendored`` excludes the whole subtree under any included
    dir ("exclude a folder -> everything under it out"), matching rekol's exclude
    semantics (:func:`rekol.config.path_is_excluded`).
    """
    cfg = load_config()
    current = list(cfg.include_deny)
    added: list[str] = []
    for glob in globs:
        if glob in current:
            continue
        current.append(glob)
        added.append(glob)
    persist_include_scope(cfg.memory_home, list(cfg.include_dirs), current)
    if added:
        for glob in added:
            click.echo(f"Denied {glob}")
        click.echo("(you can change this anytime — re-allow with `rekol include allow <glob>`)")
    else:
        click.echo("Already denied — nothing to add.")


@main.command(name="allow")
@click.argument("globs", nargs=-1, required=True)
def allow(globs: tuple[str, ...]) -> None:
    """Remove deny GLOBS from the deny-list (calm no-op if not denied)."""
    cfg = load_config()
    to_drop = set(globs)
    kept = [g for g in cfg.include_deny if g not in to_drop]
    removed = [g for g in cfg.include_deny if g in to_drop]
    persist_include_scope(cfg.memory_home, list(cfg.include_dirs), kept)
    if removed:
        for glob in removed:
            click.echo(f"Allowed {glob} (removed from deny-list)")
    else:
        click.echo("Not in the deny-list — nothing to remove.")


if __name__ == "__main__":
    main()
