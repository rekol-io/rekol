"""``rekol bootstrap`` — resumable orchestration for the memory-bootstrap routine.

This is the harness-agnostic ENGINE the ``rekol-bootstrap`` skill drives. The
skill (the user's own Claude) does the LLM precision work — clustering,
classifying, frontmatter correction, the approve/edit/skip gate, and the actual
``rekol capture`` of approved items. This command does everything that must NOT
depend on an LLM being present:

  1. **Recall** instruction-bearing candidates from the session corpus (reusing
     T2's ``recall_corpus_candidates`` + ``dedupe_against_memory``).
  2. **Scope** them to a bounded slice (recent N sessions / time window / chosen
     projects) — widenable; defaults conservative.
  3. **Batch** per-project and write each batch's review file to
     ``$REKOL_HOME/pending-review/`` INCREMENTALLY, with a pre-classified layer +
     a ready-to-edit ``rekol capture`` line per candidate.
  4. **Checkpoint** the batch plan + progress so a reaped run resumes without
     reprocessing finished batches.

The skill loops over the engine: plan once, then for each ``--next`` batch read
the review file, present approve/edit/skip, capture approved items, update
REKOL.md for always-on ones, then ``--mark-done`` that batch. A SessionEnd reap
mid-run loses at most the in-flight batch; ``--resume`` picks up where it left
off. Review-gated throughout: this command NEVER captures or writes memory
itself.

For a big corpus (T3 v2, #62), the engine adds scalable-review affordances the
skill drives: ``--top N`` (STOP-EARLY — review the strongest N candidates per
batch, defer the rest), ``--bulk-approve <batch>`` (emit every capture command
for a batch so the user can approve the whole batch in one action),
``--promote-always`` (promote a captured always-on item's pointer into REKOL.md),
and ``--refine`` (the end-of-run customize/refine summary). All stay review-gated:
``--bulk-approve`` only PRINTS the capture commands; the skill runs them after the
user approves.

The ``/goal`` wrapper is an OPTIONAL durability accelerant layered on top by the
skill — this command has no dependency on it.
"""

from __future__ import annotations

import datetime as dt
import json
import sys

import click

from rekol.bootstrap import (
    REKOL_MD_FILENAME,
    ScopeFilter,
    apply_scope,
    extract_capture_commands_from_review,
    plan_batches,
    promote_to_rekol_md,
    render_refine_summary,
    write_batch_candidates,
)
from rekol.bootstrap_state import (
    BootstrapState,
    BootstrapStateError,
    load_state,
    save_state,
    state_path_for,
)
from rekol.cli_common import guard_curated_schema
from rekol.config import Config, load_config, load_rekolignore_patterns
from rekol.corpus_propose import dedupe_against_memory, recall_corpus_candidates
from rekol.embeddings import get_embedder
from rekol.sessions.store import SessionStore, SessionStoreDimMismatchError
from rekol.store import IndexStore

# How many candidates to recall before scoping. Generous relative to T2's
# default cap so the scope filter has enough to narrow from; the bounded scope
# (and per-batch review) keeps the volume the assistant sees reasonable.
RECALL_CEILING = 300


def _now_iso() -> str:
    """Current local time as an offset-aware ISO-8601 string (matches capture)."""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _build_scope(
    all_time: bool,
    scope_days: int | None,
    scope_projects: tuple[str, ...],
    max_sessions: int | None,
) -> ScopeFilter:
    """Resolve CLI scope flags into a :class:`ScopeFilter`.

    ``--all-time`` widens the recency window to unbounded (``days=None``). An
    explicit ``--scope-days`` overrides the default window. Project filters and
    a session cap layer on top. With no flags, the conservative bounded default
    applies (recent window, capped sessions, all projects).
    """
    base = ScopeFilter.default()
    days = None if all_time else (scope_days if scope_days is not None else base.days)
    return ScopeFilter(
        projects=list(scope_projects),
        days=days,
        max_sessions=(max_sessions if max_sessions is not None else base.max_sessions),
    )


def _recall_and_dedupe(cfg: Config):
    """Recall corpus candidates and split new vs already-captured (reuses T2).

    Returns the NEW candidates (already-captured ones are intentionally dropped
    here — the bootstrap distils what is missing, and the already-captured bucket
    is T2's job to surface). Returns an empty list when session search is
    disabled or nothing is indexed, so callers report gracefully.
    """
    if not cfg.session_search_enabled:
        return []
    # Combine config excludes with any per-folder .rekolignore at the projects
    # root, so a sensitive project a user excluded is also excluded from recall
    # (matches cli_session_index.py / cli_archive.py).
    exclude_patterns = list(cfg.exclude_paths) + load_rekolignore_patterns(cfg.claude_projects_dir)

    embedder = get_embedder(cfg.embedding_model)
    # Opens live INSIDE the try so a raise in init_schema/guard_curated_schema
    # (or the session-store open) never leaks an already-opened store. ``finally``
    # closes whatever got opened (None-guarded).
    memory_store: IndexStore | None = None
    session_store: SessionStore | None = None
    try:
        memory_store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
        memory_store.init_schema()
        guard_curated_schema(memory_store)
        session_store = SessionStore(db_path=cfg.sessions_db_path, dim=embedder.dim)
        session_store.init_schema()

        try:
            session_store.reconcile_embedding_dim(embedder.dim)
        except SessionStoreDimMismatchError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2) from exc
        candidates = recall_corpus_candidates(
            session_store,
            embedder,
            max_candidates=RECALL_CEILING,
            exclude_patterns=exclude_patterns,
        )
        if not candidates:
            return []
        new_candidates, _already = dedupe_against_memory(candidates, memory_store, embedder)
        return new_candidates
    finally:
        if session_store is not None:
            session_store.close()
        if memory_store is not None:
            memory_store.close()


def _plan_run(
    cfg: Config, scope: ScopeFilter, *, top_n: int | None = None
) -> BootstrapState | None:
    """Recall → scope → batch → write review files → return a persisted checkpoint.

    Persists the checkpoint INCREMENTALLY rather than only after every batch is
    written: a "planning started" checkpoint (the full batch plan,
    ``candidates_written=0``) lands BEFORE the write loop, then it is re-saved
    after each batch's review file is written. So a crash mid-plan no longer
    orphans review files behind a missing checkpoint and forces a full replan —
    the resume path finds the checkpoint plus whatever batch files already landed.
    ``completed`` stays empty here: planning only WRITES the review files; a batch
    is marked done only after the skill fully PROCESSES it (capture + REKOL.md),
    preserving the mark-done-after-processing ordering.

    ``top_n`` (STOP-EARLY) caps how many of each batch's confidence-ranked
    candidates are rendered as ACTIVE review items; the remainder is written into
    a deferred section (not dropped). ``candidates_written`` still counts the full
    batch, since every candidate is on disk and reachable — only the review focus
    is narrowed.

    Returns ``None`` when nothing is in scope (a fresh install / over-narrow
    scope), so the caller can report "no candidates" rather than write an empty
    run. The returned (and on-disk) checkpoint is the final, complete one.
    """
    new_candidates = _recall_and_dedupe(cfg)
    scoped = apply_scope(new_candidates, scope, now_iso=_now_iso())
    batches = plan_batches(scoped)
    if not batches:
        return None

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    pending_dir = cfg.memory_home / "pending-review"
    state_path = state_path_for(cfg.memory_home)

    now = _now_iso()
    state = BootstrapState(
        run_id=run_id,
        embedding_model=cfg.embedding_model,
        scope=scope.as_dict(),
        batches=[b.batch_id for b in batches],
        completed=[],
        created_iso=now,
        updated_iso=now,
        candidates_written=0,
    )
    # "Planning started" checkpoint BEFORE any file write, so a crash on the very
    # first batch still leaves a resumable plan rather than orphaned artifacts.
    save_state(state, state_path)

    for batch in batches:
        write_batch_candidates(batch, pending_dir=pending_dir, run_id=run_id, top_n=top_n)
        state.candidates_written += len(batch.candidates)
        state.updated_iso = _now_iso()
        # Persist progress after each batch's file lands (atomic), so the
        # checkpoint never lags the artifacts on disk.
        save_state(state, state_path)

    return state


def _load_or_fail(cfg: Config) -> BootstrapState | None:
    """Load the checkpoint, surfacing a corrupt one as a hard CLI error.

    A genuinely-absent checkpoint returns ``None`` (no prior run). A corrupt one
    raises through :class:`BootstrapStateError`, which we turn into a non-zero
    exit with the remediation hint — never silently "start fresh" (that would
    reprocess everything).
    """
    try:
        return load_state(state_path_for(cfg.memory_home))
    except BootstrapStateError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(2) from exc


def _emit_status(state: BootstrapState | None) -> None:
    """Print the run status as JSON (the skill's loop primitive)."""
    if state is None:
        click.echo(json.dumps({"batches": [], "pending": [], "completed": [], "run_id": None}))
        return
    click.echo(
        json.dumps(
            {
                "run_id": state.run_id,
                "batches": state.batches,
                "pending": state.pending_batches(),
                "completed": state.completed,
                "candidates_written": state.candidates_written,
                "complete": state.is_complete(),
                "scope": state.scope,
            }
        )
    )


def _handle_status(cfg: Config) -> None:
    _emit_status(_load_or_fail(cfg))


def _handle_next(cfg: Config) -> None:
    """Print the next pending batch's review file path, or nothing if done."""
    state = _load_or_fail(cfg)
    if state is None:
        click.echo("no bootstrap run in progress — run `rekol bootstrap` first", err=True)
        raise SystemExit(1)
    pending = state.pending_batches()
    if not pending:
        click.echo("", nl=False)  # complete: empty stdout so the skill loop ends cleanly
        return
    from rekol.bootstrap import _batch_review_path

    pending_dir = cfg.memory_home / "pending-review"
    click.echo(str(_batch_review_path(pending_dir, state.run_id, pending[0])))


def _handle_mark_done(cfg: Config, batch_id: str) -> None:
    """Advance the checkpoint by recording ``batch_id`` finished."""
    state = _load_or_fail(cfg)
    if state is None:
        click.echo("no bootstrap run in progress — run `rekol bootstrap` first", err=True)
        raise SystemExit(1)
    try:
        state.mark_batch_done(batch_id)
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(2) from exc
    state.updated_iso = _now_iso()
    save_state(state, state_path_for(cfg.memory_home))
    remaining = len(state.pending_batches())
    click.echo(f"marked {batch_id} done ({remaining} batch(es) remaining)")


def _handle_bulk_approve(cfg: Config, batch_id: str) -> None:
    """Print every active ``rekol capture`` command for ``batch_id`` (BULK-APPROVE).

    The scalable-review whole-batch approval primitive: rather than gating each
    candidate individually, the skill calls this to get the full capture plan for a
    batch in one action, presents it for the user's batch-level approval, then runs
    the printed commands. REVIEW-GATED — this NEVER captures itself; it only reads
    the persisted review file (the on-disk source of truth) and emits the active
    (non-deferred) commands, one per line, so the skill can run them verbatim.
    """
    state = _load_or_fail(cfg)
    if state is None:
        click.echo("no bootstrap run in progress — run `rekol bootstrap` first", err=True)
        raise SystemExit(1)
    if batch_id not in state.batches:
        click.echo(
            f"no batch {batch_id!r} in the current run (batches: {', '.join(state.batches)})",
            err=True,
        )
        raise SystemExit(2)
    from rekol.bootstrap import _batch_review_path

    pending_dir = cfg.memory_home / "pending-review"
    review_path = _batch_review_path(pending_dir, state.run_id, batch_id)
    if not review_path.exists():
        click.echo(
            f"review file for {batch_id} is missing ({review_path}); re-run "
            "`rekol bootstrap --reset` to regenerate it.",
            err=True,
        )
        raise SystemExit(2)
    commands = extract_capture_commands_from_review(review_path.read_text(encoding="utf-8"))
    if not commands:
        click.echo(f"no capturable candidates in {batch_id} (nothing to bulk-approve)", err=True)
        return
    for command in commands:
        click.echo(command)


def _handle_promote_always(cfg: Config, *, rel_path: str, name: str, trigger: str) -> None:
    """Promote a captured always-on file's pointer into REKOL.md (the always-on index).

    Capturing an ``always/`` file does not by itself make it ambient — REKOL.md is
    the hand-curated index re-injected every session, so the pointer must land
    there. Idempotent and budget-capped (the always-on cap is honored); an
    over-budget or already-present promotion is reported, never silently dropped.
    """
    result = promote_to_rekol_md(
        cfg.memory_home,
        name=name,
        rel_path=rel_path,
        trigger=trigger,
        budget_bytes=cfg.always_on_budget_bytes,
    )
    if result.promoted:
        click.echo(f"promoted {rel_path} into {REKOL_MD_FILENAME}")
    else:
        click.echo(f"not promoted: {result.reason}")


def _handle_refine(cfg: Config) -> None:
    """Emit the end-of-run customize/refine summary the skill drives (END REFINE PASS).

    Surveys the captured curated-memory files and renders a per-layer summary so
    the skill can offer the user a final pass to rename, re-layer, or edit before
    the bootstrap is declared done. Read-only — it never mutates memory.
    """
    click.echo(render_refine_summary(_captured_memory_items(cfg)))


def _captured_memory_items(cfg: Config) -> list[dict]:
    """List the curated-memory files present, as refine-summary item dicts.

    Walks the memory-layer dirs (``always``/``when``/``topics``/``knowledge``) and
    reports each ``.md`` file as ``{layer, name, rel_path}`` — the engine has no
    per-run capture ledger (the skill captures), so the refine pass summarises the
    on-disk curated memory the run produced. ``topics`` (plural dir) maps back to
    the singular ``topic`` layer the rest of the engine uses.
    """
    dir_to_layer = {"always": "always", "when": "when", "topics": "topic", "knowledge": "knowledge"}
    items: list[dict] = []
    for layer_dir, layer in dir_to_layer.items():
        base = cfg.memory_home / layer_dir
        if not base.exists():
            continue
        for md in sorted(base.rglob("*.md")):
            items.append(
                {
                    "layer": layer,
                    "name": md.stem,
                    "rel_path": str(md.relative_to(cfg.memory_home)),
                }
            )
    return items


def _handle_plan(
    cfg: Config, scope: ScopeFilter, *, resume: bool, reset: bool, top_n: int | None
) -> None:
    """Plan a new run, or resume/refuse against an existing checkpoint.

    With ``--resume`` an existing run is continued using *its original scope* —
    the freshly-parsed ``scope`` is ignored (so ``--resume`` after an
    ``--all-time`` run doesn't silently narrow it). Without ``--resume``, a bare
    re-invocation continues the existing run only when the scope still matches;
    a differing scope is refused so the user makes an explicit choice (resume vs
    reset) rather than silently desyncing the plan from the corpus.

    ``top_n`` (STOP-EARLY) is a render-time focus applied only when planning a
    fresh run; a resume reuses the already-written review files unchanged.
    """
    existing = _load_or_fail(cfg)

    if existing is not None and not reset:
        # --resume: continue the existing run with its original scope, ignoring
        # the flags this invocation parsed. Without --resume, the scope must
        # still match (a bare re-invocation is an implicit resume); a mismatch is
        # refused so the user picks resume-vs-reset explicitly.
        if not resume and not existing.scope_matches(scope.as_dict()):
            click.echo(
                "a bootstrap run is already in progress with a different scope "
                f"({existing.scope}). Re-run `rekol bootstrap --resume` to continue "
                "it with its original scope, or `rekol bootstrap --reset <scope "
                "flags>` to discard it and start a fresh run with the new scope.",
                err=True,
            )
            raise SystemExit(2)
        click.echo(
            f"resuming run {existing.run_id}: "
            f"{len(existing.completed)}/{len(existing.batches)} batches done, "
            f"{len(existing.pending_batches())} remaining."
        )
        _emit_status(existing)
        return

    # Fresh run (no prior checkpoint, or --reset). _plan_run writes artifacts AND
    # persists the checkpoint incrementally (a "planning started" checkpoint up
    # front, re-saved after each batch), so it is already on disk by the time we
    # report. ``--reset`` overwrites any prior checkpoint via that up-front save.
    new_state = _plan_run(cfg, scope, top_n=top_n)
    if new_state is None:
        click.echo(
            "no candidates in scope — nothing to bootstrap (a fresh install with no "
            "indexed sessions, or an over-narrow scope). Try `rekol session-index "
            "--full` first, or widen with --all-time / --scope-days."
        )
        return
    click.echo(
        f"planned run {new_state.run_id}: {len(new_state.batches)} batch(es), "
        f"{new_state.candidates_written} candidate(s) written to "
        f"{cfg.memory_home / 'pending-review'}/"
    )
    _emit_status(new_state)


@click.command()
@click.option("--resume", is_flag=True, help="Continue an existing run, keeping completed batches.")
@click.option("--reset", is_flag=True, help="Discard any in-progress run and re-plan from scratch.")
@click.option("--status", "show_status", is_flag=True, help="Print run plan + progress as JSON.")
@click.option(
    "--next",
    "show_next",
    is_flag=True,
    help="Print the next pending batch's review file path (skill loop primitive).",
)
@click.option(
    "--mark-done",
    "mark_done_batch",
    default=None,
    help="Record a batch id as finished (the skill calls this after capturing approved items).",
)
@click.option(
    "--scope-project",
    "scope_projects",
    multiple=True,
    help="Restrict to one or more project slugs (repeatable). Default: all projects.",
)
@click.option(
    "--scope-days",
    type=int,
    default=None,
    help="Only consider candidates from the last N days. Default: a bounded recent window.",
)
@click.option(
    "--all-time",
    is_flag=True,
    help="Widen the recency window to the entire corpus (overrides --scope-days).",
)
@click.option(
    "--max-sessions",
    type=int,
    default=None,
    help="Cap the corpus to the N most-recent distinct sessions. Default: a bounded cap.",
)
@click.option(
    "--top",
    "top_n",
    type=int,
    default=None,
    help="STOP-EARLY: review only the top-N highest-confidence candidates per batch; "
    "defer the rest (resumable — re-run with a larger --top or --reset to widen).",
)
@click.option(
    "--bulk-approve",
    "bulk_approve_batch",
    default=None,
    help="Print every active `rekol capture` command for a batch (whole-batch approval). "
    "Review-gated: prints only — the skill runs them after the user approves.",
)
@click.option(
    "--promote-always",
    "promote_rel_path",
    default=None,
    help="Promote a captured always-on file (relative path, e.g. always/ruff.md) into "
    "REKOL.md, the always-on index. Idempotent + budget-capped.",
)
@click.option(
    "--promote-name",
    default=None,
    help="Human-readable name for the promoted always-on pointer (with --promote-always).",
)
@click.option(
    "--promote-trigger",
    default="",
    help="Trigger phrasing for the promoted always-on pointer (with --promote-always).",
)
@click.option(
    "--refine",
    "show_refine",
    is_flag=True,
    help="Print the end-of-run customize/refine summary (the final refine pass the skill drives).",
)
def main(
    resume: bool,
    reset: bool,
    show_status: bool,
    show_next: bool,
    mark_done_batch: str | None,
    scope_projects: tuple[str, ...],
    scope_days: int | None,
    all_time: bool,
    max_sessions: int | None,
    top_n: int | None,
    bulk_approve_batch: str | None,
    promote_rel_path: str | None,
    promote_name: str | None,
    promote_trigger: str,
    show_refine: bool,
) -> None:
    """Resumable engine for the memory-bootstrap skill (recall → scope → batch → checkpoint).

    Default (no subflag): plan a run — recall + scope + batch, write per-batch
    review files incrementally to ``$REKOL_HOME/pending-review/``, and persist a
    resume checkpoint. The ``rekol-bootstrap`` skill then loops: ``--next`` to get
    the next batch's file, process it (approve/edit/skip + ``rekol capture``),
    then ``--mark-done <batch>``. ``--resume`` continues an interrupted run;
    ``--reset`` starts over.

    Scalable-review affordances for a big corpus: ``--top N`` (STOP-EARLY, review
    the strongest N per batch, defer the rest), ``--bulk-approve <batch>`` (emit
    every capture command for a batch in one action), ``--promote-always`` (promote
    a captured always-on item's pointer into REKOL.md), and ``--refine`` (the
    end-of-run customize pass). NEVER captures memory itself — review-gated.
    """
    if resume and reset:
        raise click.UsageError("--resume and --reset are mutually exclusive")
    cfg = load_config()

    # Query/advance subcommands operate on the existing checkpoint only.
    if show_status:
        _handle_status(cfg)
        return
    if show_next:
        _handle_next(cfg)
        return
    if mark_done_batch is not None:
        _handle_mark_done(cfg, mark_done_batch)
        return
    if bulk_approve_batch is not None:
        _handle_bulk_approve(cfg, bulk_approve_batch)
        return
    if promote_rel_path is not None:
        if not promote_name:
            raise click.UsageError("--promote-always requires --promote-name")
        _handle_promote_always(
            cfg, rel_path=promote_rel_path, name=promote_name, trigger=promote_trigger
        )
        return
    if show_refine:
        _handle_refine(cfg)
        return

    scope = _build_scope(all_time, scope_days, scope_projects, max_sessions)
    _handle_plan(cfg, scope, resume=resume, reset=reset, top_n=top_n)


if __name__ == "__main__":
    sys.exit(main())
