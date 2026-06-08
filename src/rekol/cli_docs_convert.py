"""rekol import: turn a text-file tree into synthetic Claude Code JSONL.

DEPRECATED (T8 #63): ``import`` writes a ONE-TIME snapshot that drifts the moment
the source tree changes. It is superseded by INCLUDE-SCOPE — add a directory to
``include_dirs`` in ``rekol.config.yaml`` once and every ``session-index`` run
re-indexes its new files automatically, governed by the deny-list (see
``rekol.include_indexer``). ``import`` is KEPT WORKING (existing flows like
``backstage-ai-archive`` must not break) but prints a notice steering to
include-scope.

Writes one .jsonl per immediate-child folder of SOURCE_DIR into
``<claude_projects_dir>/<prefix>/`` so the existing rekol session-index
ingester surfaces the content in rekol search. By default it then chains
``rekol session-index --incremental`` to ingest immediately.

    rekol import ~/path/to/sessions --prefix backstage-ai-archive
    rekol import ~/path/to/sessions --no-index      # write only
    rekol import ~/path/to/sessions --dry-run        # report, write nothing
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from rekol.config import load_config
from rekol.docs_convert import TEXT_EXTENSIONS, convert_tree

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB; backstop against one huge file


@click.command()
@click.argument(
    "source_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--prefix",
    default="backstage-ai-archive",
    show_default=True,
    help="Subdirectory under claude_projects_dir to write the synthetic "
    "transcripts into. Also salts the uuid/sessionId hashes so different "
    "archives never collide.",
)
@click.option(
    "--max-bytes",
    default=_DEFAULT_MAX_BYTES,
    show_default=True,
    type=int,
    help="Skip any single file larger than this (backstop against one huge file "
    "becoming one giant low-precision search hit).",
)
@click.option(
    "--index/--no-index",
    default=True,
    show_default=True,
    help="After writing, chain `rekol session-index --incremental` to ingest. "
    "--incremental (not --full) so the existing ~1000 transcripts are not "
    "needlessly re-walked.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be written without writing anything.",
)
@click.option(
    "--include",
    default="",
    help="Comma-separated extra extensions to treat as text (no dots), e.g. "
    "'html,rst'. Added on top of the built-in allowlist.",
)
@click.option(
    "--exclude",
    default="",
    help="Comma-separated extensions to drop from the allowlist (no dots), e.g. 'json,csv'. "
    "Exclude takes precedence over --include if the same extension appears in both.",
)
def main(
    source_dir: Path,
    prefix: str,
    max_bytes: int,
    index: bool,
    dry_run: bool,
    include: str,
    exclude: str,
) -> None:
    """Convert SOURCE_DIR (a tree of text files) into synthetic transcripts."""
    # DEPRECATION (T8 #63): `import` is a ONE-TIME snapshot — edit/move/add a file
    # under SOURCE_DIR after this and the index drifts. Include-scope replaces it
    # with an ONGOING relationship: add the dir to `include_dirs` once and every
    # `session-index` run picks up its new files automatically (deny-list
    # governed). We keep `import` WORKING (so existing flows like
    # backstage-ai-archive don't break) but steer users to the durable path. The
    # notice goes to stderr so it never pollutes the machine-readable stats line on
    # stdout.
    click.echo(
        "note: `rekol import` is DEPRECATED — it writes a one-time snapshot that "
        "goes stale when the source changes. Prefer include-scope: add this "
        "directory to `include_dirs` in rekol.config.yaml (or via onboarding) so "
        "`rekol session-index` keeps it indexed on every run, governed by your "
        "deny-list. `import` still works for now.",
        err=True,
    )
    cfg = load_config()
    target_dir = cfg.claude_projects_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    def _split(raw: str) -> set[str]:
        return {e.strip().lstrip(".").lower() for e in raw.split(",") if e.strip()}

    text_extensions = (TEXT_EXTENSIONS | _split(include)) - _split(exclude)

    stats = convert_tree(
        source_dir=source_dir,
        target_dir=target_dir,
        prefix=prefix,
        max_bytes=max_bytes,
        dry_run=dry_run,
        text_extensions=text_extensions,
    )
    click.echo(stats.as_line())

    if dry_run:
        click.echo(f"(dry-run) would write under {target_dir / prefix}", err=True)
        return

    if not index:
        return

    # Ingest the just-written transcripts by invoking the session-index
    # subcommand in-process. Lazy import to avoid the cli -> cli_docs_convert
    # -> cli import cycle. standalone_mode=False makes Click return/raise
    # instead of sys.exit, so a failure here doesn't kill the whole convert.
    from rekol.cli import main as rekol_cli

    click.echo("ingesting new transcripts (session-index --incremental) ...", err=True)
    # We only translate SystemExit here; cli_session_index uses sys.exit on every
    # error path. ClickException/Abort (none today) are intentionally allowed to propagate.
    try:
        rekol_cli(["session-index", "--incremental"], standalone_mode=False)
    except SystemExit as exc:  # some leaf commands still sys.exit on error paths
        if exc.code not in (0, None):
            click.echo(f"session-index exited {exc.code}; index may be partial.", err=True)
            sys.exit(exc.code)


if __name__ == "__main__":
    sys.exit(main())
