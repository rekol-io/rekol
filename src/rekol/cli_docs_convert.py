"""memory-docs-convert: turn a text-file tree into synthetic Claude Code JSONL.

Writes one .jsonl per immediate-child folder of SOURCE_DIR into
``<claude_projects_dir>/<prefix>/`` so the existing claude-session-index
ingester surfaces the content in memory-search. By default it then chains
``claude-session-index --incremental`` to ingest immediately.

    memory-docs-convert ~/path/to/sessions --prefix backstage-ai-archive
    memory-docs-convert ~/path/to/sessions --no-index      # write only
    memory-docs-convert ~/path/to/sessions --dry-run        # report, write nothing
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import click

from rekol.config import load_config
from rekol.docs_convert import convert_tree

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
    help="After writing, chain `claude-session-index --incremental` to ingest. "
    "--incremental (not --full) so the existing ~1000 transcripts are not "
    "needlessly re-walked.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be written without writing anything.",
)
def main(source_dir: Path, prefix: str, max_bytes: int, index: bool, dry_run: bool) -> None:
    """Convert SOURCE_DIR (a tree of text files) into synthetic transcripts."""
    cfg = load_config()
    target_dir = cfg.claude_projects_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    stats = convert_tree(
        source_dir=source_dir,
        target_dir=target_dir,
        prefix=prefix,
        max_bytes=max_bytes,
        dry_run=dry_run,
    )
    click.echo(stats.as_line())

    if dry_run:
        click.echo(f"(dry-run) would write under {target_dir / prefix}", err=True)
        return

    if not index:
        return

    # Chain the existing ingester (incremental: skip unchanged transcripts).
    if shutil.which("claude-session-index") is None:
        click.echo(
            "claude-session-index not on PATH; JSONL written but not indexed. "
            "Open a fresh shell or run `claude-session-index --incremental` manually.",
            err=True,
        )
        sys.exit(3)
    click.echo("running claude-session-index --incremental ...", err=True)
    completed = subprocess.run(["claude-session-index", "--incremental"], check=False)
    if completed.returncode != 0:
        click.echo(
            f"claude-session-index exited {completed.returncode}; index may be partial.",
            err=True,
        )
        sys.exit(completed.returncode)


if __name__ == "__main__":
    sys.exit(main())
