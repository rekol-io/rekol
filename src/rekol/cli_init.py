"""rekol init: interactive first-run onboarding (forked Path A / Path B).

Detects what already exists on the machine and forks the flow on the headline
signal — how many past Claude Code transcripts there are to mine:

* **Path A — transcripts found.** Three INDEPENDENT opt-in steps:
  1. *index* the sessions so REKOL can search history [default yes];
  2. *memory bootstrap* — distil recurring "always do X / repos live in Y"
     instructions into curated memory now (invokes ``rekol bootstrap`` with the
     shared scope control), or defer to later [default later];
  3. *starter pack* — seed the template scaffold [default off, since a real
     history will populate memory organically].

* **Path B — no/few transcripts.** A "light session": seed the starter pack so
  the home isn't empty, and explain how REKOL grows over time. No bootstrap —
  there's nothing to mine yet.

Every step is opt-in, every prompt defaults to a safe no-op, and every step is
re-runnable (re-running init never clobbers existing files and re-prints how to
run each step manually). Detection + seeding logic is pure and unit-tested in
``rekol.onboarding``; this module is the thin interactive shell.

Sibling subcommands are invoked in-process (not via subprocess), so onboarding
does not depend on `rekol` being on PATH (it isn't until ~/.zshrc is sourced).
"""

from __future__ import annotations

import sys

import click

from rekol.config import Config, load_config
from rekol.onboarding import (
    bootstrap_argv,
    count_claude_transcripts,
    detect_cloud_sync_dirs,
    find_template_dir,
    seed_starter_pack,
)

# Below this many transcripts there is effectively nothing to mine, so init
# takes the Path B "light session" rather than offering to index/bootstrap a
# corpus that would yield no durable signal.
PATH_A_MIN_TRANSCRIPTS = 1

# At or above this many transcripts, indexing is a multi-minute job (embedding
# every message in a large corpus). The magnitude-aware refinement (#59 §6.4)
# warns the user before they kick it off so a long first run isn't a surprise.
# The detector caps its scan at 10_000 (see ``count_claude_transcripts``), so a
# count at the cap reads as "a lot" and trips this warning, which is correct.
HUGE_HISTORY_THRESHOLD = 1_000

# The signature close line — the honest promise REKOL makes on every path.
CLOSE_LINE = "It recalls what it has, faithfully — what it doesn't have, it'll tell you."

# How REKOL grows over time, shown on the empty/light close (and always reachable).
GROWS_HINT = (
    "Grow REKOL over time:\n"
    '  - Capture facts as you work: `rekol capture` (or say "remember this" '
    "and let the rekol skill do it).\n"
    "  - As your Claude Code history builds up, re-run `rekol init` to index it "
    "and `rekol bootstrap` to distil recurring instructions into memory.\n"
    "  - Include external knowledge dirs with `rekol include add <dir>` "
    "(you can change this anytime)."
)


@click.command(name="init")
@click.option(
    "--yes",
    is_flag=True,
    help="Accept the recommended default for every prompt (non-interactive).",
)
@click.option(
    "--seed-only",
    is_flag=True,
    help="Only gap-fill the starter-pack scaffold, then stop — no indexing, "
    "bootstrap, or coverage. The quiet primitive the onboarding skill uses for "
    "its baseline step.",
)
def main(yes: bool, seed_only: bool) -> None:
    """Interactively onboard a new REKOL install (forked on detected history)."""
    cfg = load_config()
    click.echo(f"REKOL home: {cfg.memory_home}")

    if seed_only:
        # Baseline-seed primitive (#59 step 4): gap-fill the scaffold and stop.
        # Deliberately skips indexing / bootstrap / coverage — the onboarding skill
        # calls this for a quiet, safe baseline seed, NOT the full interview (which
        # would otherwise fire a multi-minute session-index pass on a large history).
        _seed_and_report(cfg)
        return

    n_transcripts = count_claude_transcripts(cfg.claude_projects_dir)
    if n_transcripts >= PATH_A_MIN_TRANSCRIPTS:
        indexed_content = _path_a(cfg, n_transcripts, yes=yes)
    else:
        _path_b(cfg, yes=yes)
        indexed_content = False

    # Shared tail: surface detected cloud-sync folders + optional legacy/notes
    # imports. These apply on both paths and are independent, opt-in steps.
    _offer_cloud_sync_hint()
    _offer_notes_import(yes=yes)
    _offer_legacy_migration(yes=yes)

    # Adaptive close (#59 §6.1): WITH content (we just indexed a corpus) print the
    # coverage report; on an empty/light install pivot to "how it grows" instead.
    # Either way, end on the signature honesty line.
    _adaptive_close(indexed_content=indexed_content)

    click.echo(
        "rekol init complete. Re-run `rekol init` any time — it never overwrites your files."
    )


# --- Path A: a real history to mine -------------------------------------------


def _path_a(cfg: Config, n_transcripts: int, *, yes: bool) -> bool:
    """Three independent opt-in steps for an install with past transcripts.

    Returns whether session content was indexed this run, so the adaptive close
    knows whether there is fresh content to print a coverage report for.
    """
    click.echo(f"Found {n_transcripts} past Claude Code sessions under {cfg.claude_projects_dir}.")

    # Step 1 — index (default yes): makes history searchable (recall). Magnitude-
    # aware (#59 §6.4): on a huge history, warn that indexing is a long job BEFORE
    # the prompt, so a multi-minute first run isn't a surprise. The default stays
    # yes — recall is the point — but the user makes an informed choice.
    if n_transcripts >= HUGE_HISTORY_THRESHOLD:
        click.echo(
            f"  That's a large history — indexing all {n_transcripts} sessions may "
            "take a while (it embeds every message). You can index now or skip and "
            "do it later with `rekol session-index` (you can change this anytime)."
        )
    indexed_content = False
    if yes or click.confirm(
        "Index them so REKOL can search your history? (you can change this anytime)",
        default=True,
    ):
        # Only treat content as indexed if the step actually SUCCEEDED, so the
        # adaptive close doesn't run a coverage report for a run that indexed nothing.
        indexed_content = _invoke(["session-index", "--incremental"])

    # Step 2 — memory bootstrap (default: later). Distils recurring instructions
    # into curated, always-on memory (understanding). Independent of step 1 —
    # offered even if indexing was declined. With --yes we defer (bootstrap is a
    # review-gated, assistant-driven flow, not a safe non-interactive default).
    if not yes and click.confirm(
        "Bootstrap curated memory now — mine your history for recurring "
        '"always do X / repos live in Y" instructions to review and keep?',
        default=False,
    ):
        _invoke(_collect_bootstrap_argv())
    else:
        click.echo(
            "  Skipping bootstrap for now. Run it any time with `rekol bootstrap` "
            "(scope it with --scope-days / --scope-project / --all-time / --max-sessions)."
        )

    # Step 3 — starter pack (default off): a real history populates memory
    # organically, so the template scaffold is opt-in here.
    if not yes and click.confirm(
        "Also seed the starter-pack template scaffold (REKOL.md + example layers)?",
        default=False,
    ):
        _seed_and_report(cfg)

    return indexed_content


# --- Path B: a fresh/light install --------------------------------------------


def _path_b(cfg: Config, *, yes: bool) -> None:
    """Light session for an install with no/few transcripts: seed + teach.

    The "how it grows" guidance and the signature close line are emitted by the
    shared :func:`_adaptive_close` after the tail, so this path just seeds the
    pack — keeping the close copy in one place (no duplicate grows hint).
    """
    click.echo(
        "No Claude Code history to mine yet — starting a light session. "
        "Seeding the starter pack so your memory home isn't empty "
        "(you can change this anytime)."
    )
    _seed_and_report(cfg)


# --- adaptive close (#59 §6.1) ------------------------------------------------


def _adaptive_close(*, indexed_content: bool) -> None:
    """End the flow honestly: a coverage report with content, "how it grows" without.

    WITH content (a corpus was just indexed) we run ``rekol coverage`` so the user
    sees the indexed-vs-discoverable signal; on an empty/light install there is
    nothing to report, so we skip it and pivot to how REKOL grows over time.
    Either way we end on the signature close line — the honest promise REKOL makes
    on every path.
    """
    if indexed_content:
        # Coverage is informational (exit 0) and silent-skips when no include-scope
        # is configured, so this is safe to run unconditionally once we've indexed.
        _invoke(["coverage"])
    else:
        click.echo(GROWS_HINT)
    click.echo(CLOSE_LINE)


# --- shared helpers -----------------------------------------------------------


def _collect_bootstrap_argv() -> list[str]:
    """Prompt for the shared scope control and build the bootstrap argv.

    Reuses T3's exact knobs (``--scope-days`` / ``--scope-project`` /
    ``--all-time`` / ``--max-sessions``); each prompt defaults to T3's bounded
    default (blank = leave the flag off). ``--all-time`` is asked last and, when
    chosen, overrides the recency window.
    """
    click.echo(
        "  Scope control (press Enter to accept REKOL's bounded default: a recent "
        "window, capped sessions, all projects):"
    )
    scope_days = _prompt_optional_int("  Limit to the last N days? (blank = default window)")
    projects_raw = click.prompt(
        "  Limit to project slugs (comma-separated, blank = all projects)",
        default="",
        show_default=False,
    )
    scope_projects = tuple(p.strip() for p in projects_raw.split(",") if p.strip())
    max_sessions = _prompt_optional_int(
        "  Cap to the N most-recent sessions? (blank = default cap)"
    )
    all_time = click.confirm(
        "  Widen to your ENTIRE history instead? (overrides the day limit)",
        default=False,
    )
    return bootstrap_argv(
        all_time=all_time,
        scope_days=scope_days,
        scope_projects=scope_projects,
        max_sessions=max_sessions,
    )


def _prompt_optional_int(text: str) -> int | None:
    """Prompt for an optional positive int; blank → None (use the default)."""
    raw = click.prompt(text, default="", show_default=False)
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        click.echo("  (not a number — leaving this scope knob at its default)", err=True)
        return None
    if value <= 0:
        click.echo("  (must be positive — leaving this scope knob at its default)", err=True)
        return None
    return value


def _seed_and_report(cfg: Config) -> None:
    """Seed the starter pack into the home and report what (if anything) landed."""
    template_dir = find_template_dir()
    if template_dir is None:
        click.echo(
            "  (could not locate the bundled starter-pack template — skipping seed)", err=True
        )
        return
    created = seed_starter_pack(template_dir, cfg.memory_home)
    if created:
        click.echo(f"  Seeded {len(created)} starter-pack file(s) into {cfg.memory_home}.")
    else:
        click.echo("  Starter pack already present — nothing to seed.")


def _offer_cloud_sync_hint() -> None:
    """Surface detected cloud-sync folders as a REKOL_HOME location reminder."""
    cloud = detect_cloud_sync_dirs()
    if not cloud:
        return
    labels = ", ".join(c.label for c in cloud)
    click.echo(
        f"Detected cloud-sync folders ({labels}). REKOL_HOME can live in one so "
        "your markdown syncs across devices — the index lives in a local cache "
        "outside your memory folder, so syncing your memory never syncs the index."
    )


def _offer_notes_import(*, yes: bool) -> None:
    """Opt-in import of an existing notes/docs corpus (off by default)."""
    if yes or not click.confirm(
        "Import an existing notes/docs folder (e.g. an Obsidian vault) now?",
        default=False,
    ):
        return
    corpus = click.prompt("Path to the folder", type=click.Path(exists=True))
    _invoke(["import", corpus])


def _offer_legacy_migration(*, yes: bool) -> None:
    """Opt-in legacy migration of ~/.claude/projects/*/memory/ (off by default)."""
    if yes or not click.confirm(
        "Import legacy ~/.claude/projects/*/memory/ content into REKOL now?",
        default=False,
    ):
        return
    _invoke(["migrate", "auto", "--commit", "--no-llm"])


def _invoke(argv: list[str]) -> bool:
    """Invoke a rekol subcommand in-process; return True on success, False on failure.

    Lazy import of the CLI group avoids the cli -> cli_init -> cli import cycle.
    standalone_mode=False makes Click return/raise instead of calling sys.exit,
    so one failed step does not kill the whole onboarding flow. Callers that must
    only proceed when a step really succeeded (e.g. the adaptive close reports
    coverage only when indexing actually happened) check the return; others ignore it.
    """
    from rekol.cli import main as rekol_cli

    click.echo(f"  rekol {' '.join(argv)}", err=True)
    try:
        rekol_cli(argv, standalone_mode=False)
        return True
    except SystemExit as exc:  # some leaf commands still sys.exit on error paths
        if exc.code not in (0, None):
            click.echo(f"  (warning: rekol {argv[0]} exited {exc.code})", err=True)
            return False
        return True
    except click.Abort:
        click.echo(f"  (warning: rekol {argv[0]} aborted)", err=True)
        return False
    except click.ClickException as exc:
        exc.show()
        return False
    except Exception as exc:  # noqa: BLE001 — INTENTIONAL: one failed step must
        # not kill the whole onboarding flow (the docstring's contract). Any
        # unexpected error from a leaf command is surfaced as a warning and init
        # continues to the next step rather than aborting everything.
        click.echo(f"  (warning: rekol {argv[0]} failed: {exc})", err=True)
        return False


if __name__ == "__main__":
    sys.exit(main())
