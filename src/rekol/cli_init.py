"""rekol init: interactive first-run onboarding.

Detects what already exists on the machine and offers to ingest it, instead of
starting from an empty store. All prompts default to a safe no-op so pressing
Enter through the flow changes nothing. The detection logic is in
``rekol.onboarding.detect`` (pure, unit-tested); this module is the thin shell.

Sibling subcommands are invoked in-process (not via subprocess), so onboarding
does not depend on `rekol` being on PATH (it isn't until ~/.zshrc is sourced).
"""

from __future__ import annotations

import sys

import click

from rekol.config import load_config
from rekol.onboarding import count_claude_transcripts, detect_cloud_sync_dirs


@click.command(name="init")
@click.option(
    "--yes",
    is_flag=True,
    help="Accept the recommended default for every prompt (non-interactive).",
)
def main(yes: bool) -> None:
    """Interactively onboard a new REKOL install."""
    cfg = load_config()
    click.echo(f"REKOL home: {cfg.memory_home}")

    # 1) Headline: offer to index existing Claude Code transcripts.
    n_transcripts = count_claude_transcripts(cfg.claude_projects_dir)
    if n_transcripts > 0:
        if yes or click.confirm(
            f"Found {n_transcripts} past Claude Code sessions under "
            f"{cfg.claude_projects_dir}. Index them so REKOL can search your history?",
            default=True,
        ):
            _invoke(["session-index", "--incremental"])
    else:
        click.echo("No Claude Code transcripts found — skipping history indexing.")

    # 2) Offer to import an existing notes/docs corpus.
    if not yes and click.confirm(
        "Import an existing notes/docs folder (e.g. an Obsidian vault) now?",
        default=False,
    ):
        corpus = click.prompt("Path to the folder", type=click.Path(exists=True))
        _invoke(["import", corpus])

    # 3) Offer detected cloud-sync folders as a REKOL_HOME location reminder.
    cloud = detect_cloud_sync_dirs()
    if cloud:
        labels = ", ".join(c.label for c in cloud)
        click.echo(
            f"Detected cloud-sync folders ({labels}). REKOL_HOME can live in one so "
            "your markdown syncs across devices — but keep the .index/ directory out "
            "of sync (it is machine-specific and rebuildable)."
        )

    # 4) Opt-in legacy migration (off by default).
    if not yes and click.confirm(
        "Import legacy ~/.claude/projects/*/memory/ content into REKOL now?",
        default=False,
    ):
        _invoke(["migrate", "auto", "--commit", "--no-llm"])

    click.echo("rekol init complete.")


def _invoke(argv: list[str]) -> None:
    """Invoke a rekol subcommand in-process, surfacing failure without aborting init.

    Lazy import of the CLI group avoids the cli -> cli_init -> cli import cycle.
    standalone_mode=False makes Click return/raise instead of calling sys.exit,
    so one failed step does not kill the whole onboarding flow.
    """
    from rekol.cli import main as rekol_cli

    click.echo(f"  rekol {' '.join(argv)}", err=True)
    try:
        rekol_cli(argv, standalone_mode=False)
    except SystemExit as exc:  # some leaf commands still sys.exit on error paths
        if exc.code not in (0, None):
            click.echo(f"  (warning: rekol {argv[0]} exited {exc.code})", err=True)
    except click.Abort:
        click.echo(f"  (warning: rekol {argv[0]} aborted)", err=True)
    except click.ClickException as exc:
        exc.show()


if __name__ == "__main__":
    sys.exit(main())
