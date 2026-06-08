"""Tests for the ``rekol bootstrap`` orchestration CLI (T3, #41).

The command is the harness-agnostic engine the ``rekol-bootstrap`` skill drives:
it recalls (reusing T2), scopes, batches, writes per-batch review files
INCREMENTALLY, and checkpoints progress so a reaped run resumes. The skill calls
it in a loop (``--status`` → process ``--next`` file → ``--mark-done`` → repeat).

Hermetic: a throwaway SessionStore is seeded at the resolved cache path with the
deterministic HashingEmbedder; the conftest autouse fixture isolates the cache /
archive / data-home env. No LLM, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from rekol.embeddings import HashingEmbedder
from rekol.sessions.store import SessionStore


def _seed_memory_home(root: Path) -> None:
    (root / "topics").mkdir(parents=True, exist_ok=True)
    (root / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")


def _seed_sessions(db_path: Path, embedder: HashingEmbedder) -> None:
    store = SessionStore(db_path=db_path, dim=embedder.dim)
    store.init_schema()
    messages = [
        dict(
            session_id="sess-rekol",
            message_uuid="u1",
            parent_uuid=None,
            role="user",
            content="We always deploy via docker compose pull, never manually.",
            cwd="/home/leon/github/rekol",
            timestamp_iso="2026-06-01T10:00:00Z",
            timestamp_unix=1780308000,
            jsonl_path="/transcripts/sess-rekol.jsonl",
            line_number=12,
        ),
        dict(
            session_id="sess-infra",
            message_uuid="u2",
            parent_uuid=None,
            role="user",
            content="I prefer verbose descriptive variable names over short ones.",
            cwd="/home/leon/work/infra",
            timestamp_iso="2026-06-02T09:00:00Z",
            timestamp_unix=1780394400,
            jsonl_path="/transcripts/sess-infra.jsonl",
            line_number=3,
        ),
    ]
    for msg in messages:
        rowid = store.insert_message(msg)
        assert rowid is not None
        store.upsert_embedding(rowid, embedder.embed(msg["content"]))
    store.close()


def _setup(tmp_path: Path, monkeypatch) -> Path:
    from rekol.config import load_config

    memory_home = tmp_path / "home"
    _seed_memory_home(memory_home)
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    cfg = load_config()
    _seed_sessions(cfg.sessions_db_path, HashingEmbedder(dim=384))
    return memory_home


def test_bootstrap_plans_batches_and_writes_review_files(tmp_path: Path, monkeypatch) -> None:
    """A fresh bootstrap recalls, batches per-project, and writes review files."""
    from rekol.cli_bootstrap import main as bootstrap_main

    memory_home = _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    # Widen scope so the seeded 2026-06 messages aren't excluded by the default window.
    result = runner.invoke(bootstrap_main, ["--all-time"])
    assert result.exit_code == 0, result.output

    pending = memory_home / "pending-review"
    review_files = sorted(pending.glob("bootstrap-*.md"))
    assert review_files, f"expected per-batch review files, got {list(pending.iterdir())}"
    bodies = "\n".join(p.read_text() for p in review_files)
    assert "docker compose pull" in bodies
    assert "review" in bodies.lower()
    # The checkpoint exists so a resume is possible.
    assert (pending / ".bootstrap-state.json").exists()


def test_bootstrap_status_reports_plan_and_progress(tmp_path: Path, monkeypatch) -> None:
    """`--status` emits machine-readable plan + pending/done for the skill loop."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    result = runner.invoke(bootstrap_main, ["--status"])
    assert result.exit_code == 0, result.output
    status = json.loads(result.output)
    assert "batches" in status and status["batches"]
    assert "pending" in status and "completed" in status
    assert status["completed"] == []


def test_bootstrap_mark_done_advances_checkpoint(tmp_path: Path, monkeypatch) -> None:
    """`--mark-done <batch>` records a finished batch; resume then skips it."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    status = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    first_batch = status["batches"][0]

    result = runner.invoke(bootstrap_main, ["--mark-done", first_batch])
    assert result.exit_code == 0, result.output

    status2 = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    assert first_batch in status2["completed"]
    assert first_batch not in status2["pending"]


def test_bootstrap_next_prints_next_pending_review_file(tmp_path: Path, monkeypatch) -> None:
    """`--next` prints the path of the next not-done batch's review file."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    result = runner.invoke(bootstrap_main, ["--next"])
    assert result.exit_code == 0, result.output
    path = result.output.strip()
    assert path.endswith(".md")
    assert Path(path).exists()


def test_bootstrap_resume_does_not_reprocess_completed_batch(tmp_path: Path, monkeypatch) -> None:
    """RESUMABILITY (hard AC): a resume preserves the existing plan + completed set."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    status = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    first_batch = status["batches"][0]
    runner.invoke(bootstrap_main, ["--mark-done", first_batch])

    # A re-invocation with --resume must keep the completed batch done, not reset.
    result = runner.invoke(bootstrap_main, ["--resume", "--all-time"])
    assert result.exit_code == 0, result.output
    status2 = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    assert first_batch in status2["completed"], "resume must not reprocess a done batch"


def test_bootstrap_bare_reinvoke_rejects_scope_change(tmp_path: Path, monkeypatch) -> None:
    """A bare re-invocation with a different scope is refused (don't silently mix runs).

    Without --resume, a re-invocation is an implicit resume — but only when the
    scope still matches. A differing scope forces the user to choose resume vs
    reset explicitly, so the batch plan never silently desyncs from the corpus.
    """
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    # Re-invoke (no --resume) but narrow scope to a project filter — incompatible.
    result = runner.invoke(bootstrap_main, ["--scope-project", "rekol"])
    assert result.exit_code != 0
    assert "scope" in result.output.lower() or "reset" in result.output.lower()


def test_bootstrap_resume_keeps_original_scope_ignoring_flags(tmp_path: Path, monkeypatch) -> None:
    """--resume continues the existing run with its ORIGINAL scope, ignoring new flags."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    before = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    # Resume while passing a conflicting scope flag — it must be ignored, not error.
    result = runner.invoke(bootstrap_main, ["--resume", "--scope-project", "rekol"])
    assert result.exit_code == 0, result.output
    after = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    assert after["scope"] == before["scope"], "resume must preserve the original scope"
    assert after["batches"] == before["batches"], "resume must preserve the original plan"


def test_bootstrap_reset_starts_fresh_run(tmp_path: Path, monkeypatch) -> None:
    """`--reset` discards prior progress and re-plans from scratch."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    status = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    first_batch = status["batches"][0]
    runner.invoke(bootstrap_main, ["--mark-done", first_batch])

    result = runner.invoke(bootstrap_main, ["--reset", "--all-time"])
    assert result.exit_code == 0, result.output
    status2 = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    assert status2["completed"] == [], "reset must clear the completed set"


def test_bootstrap_empty_corpus_is_graceful(tmp_path: Path, monkeypatch) -> None:
    """With nothing recalled, bootstrap reports no candidates and exits 0."""
    from rekol.cli_bootstrap import main as bootstrap_main

    memory_home = tmp_path / "home"
    _seed_memory_home(memory_home)
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    runner = CliRunner()
    result = runner.invoke(bootstrap_main, ["--all-time"])
    assert result.exit_code == 0, result.output
    assert "no candidates" in result.output.lower() or "nothing" in result.output.lower()


def test_bootstrap_scope_project_filter_limits_batches(tmp_path: Path, monkeypatch) -> None:
    """`--scope-project rekol` keeps only the rekol batch, dropping infra."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(bootstrap_main, ["--all-time", "--scope-project", "rekol"])
    assert result.exit_code == 0, result.output
    status = json.loads(runner.invoke(bootstrap_main, ["--status"]).output)
    assert status["batches"] == ["project-rekol"], status["batches"]


# --------------------------- scalable review UX: stop-early (--top) ---------------------------


def _seed_many_in_one_project(db_path: Path, embedder: HashingEmbedder, *, n: int) -> None:
    """Seed N instruction-bearing messages all in one project (one fat batch).

    Lets the --top / --bulk-approve tests exercise a batch with several candidates
    rather than the single-message-per-project default seed.
    """
    store = SessionStore(db_path=db_path, dim=embedder.dim)
    store.init_schema()
    rules = [
        "We always run ruff before committing.",
        "Never force-push the main branch.",
        "We always deploy via docker compose pull, never manually.",
        "I prefer verbose descriptive variable names over short ones.",
        "When deploying, always pull images first.",
        "Always squash-merge feature branches into main.",
    ]
    for i in range(n):
        content = rules[i % len(rules)] + f" (rule {i})"
        msg = dict(
            session_id=f"sess-{i}",
            message_uuid=f"u{i}",
            parent_uuid=None,
            role="user",
            content=content,
            cwd="/home/leon/github/rekol",
            timestamp_iso="2026-06-01T10:00:00Z",
            timestamp_unix=1780308000 + i,
            jsonl_path=f"/transcripts/sess-{i}.jsonl",
            line_number=i + 1,
        )
        rowid = store.insert_message(msg)
        assert rowid is not None
        store.upsert_embedding(rowid, embedder.embed(content))
    store.close()


def _setup_many(tmp_path: Path, monkeypatch, *, n: int = 6) -> Path:
    from rekol.config import load_config

    memory_home = tmp_path / "home"
    _seed_memory_home(memory_home)
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    cfg = load_config()
    _seed_many_in_one_project(cfg.sessions_db_path, HashingEmbedder(dim=384), n=n)
    return memory_home


def test_bootstrap_top_n_defers_tail_in_review_file(tmp_path: Path, monkeypatch) -> None:
    """`--top N` reviews the top-N candidates and DEFERS the rest (resumable, not dropped)."""
    from rekol.cli_bootstrap import main as bootstrap_main

    memory_home = _setup_many(tmp_path, monkeypatch, n=6)
    runner = CliRunner()
    result = runner.invoke(bootstrap_main, ["--all-time", "--top", "2"])
    assert result.exit_code == 0, result.output

    review = next((memory_home / "pending-review").glob("bootstrap-*project-rekol.md"))
    body = review.read_text().lower()
    # The deferred tail is announced (transparency) and resumable, never dropped.
    assert "deferred" in body
    # All candidates remain present in the file (active head + deferred tail).
    assert "rule 0" in review.read_text()


def test_bootstrap_top_n_keeps_all_candidates_present(tmp_path: Path, monkeypatch) -> None:
    """STOP-EARLY never drops a candidate — every recalled rule is still in the file."""
    from rekol.cli_bootstrap import main as bootstrap_main

    memory_home = _setup_many(tmp_path, monkeypatch, n=6)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time", "--top", "1"])
    review = next((memory_home / "pending-review").glob("bootstrap-*project-rekol.md"))
    body = review.read_text()
    for i in range(6):
        assert f"rule {i}" in body, f"candidate rule {i} was silently dropped by --top"


# --------------------------- scalable review UX: --bulk-approve ---------------------------


def test_bootstrap_bulk_approve_emits_capture_command_per_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    """`--bulk-approve <batch>` prints a ready-to-run capture command per candidate."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup_many(tmp_path, monkeypatch, n=4)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    result = runner.invoke(bootstrap_main, ["--bulk-approve", "project-rekol"])
    assert result.exit_code == 0, result.output
    capture_lines = [ln for ln in result.output.splitlines() if ln.startswith("rekol capture ")]
    assert len(capture_lines) == 4, result.output


def test_bootstrap_bulk_approve_does_not_write_memory(tmp_path: Path, monkeypatch) -> None:
    """`--bulk-approve` is review-gated: it PRINTS commands, never captures itself."""
    from rekol.cli_bootstrap import main as bootstrap_main

    memory_home = _setup_many(tmp_path, monkeypatch, n=4)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    runner.invoke(bootstrap_main, ["--bulk-approve", "project-rekol"])
    # No memory-layer file was written by bulk-approve (the skill runs the commands).
    for layer in ("always", "when", "topics", "knowledge"):
        base = memory_home / layer
        if base.exists():
            assert not list(base.rglob("*.md")), f"bulk-approve wrote into {layer}/"


def test_bootstrap_bulk_approve_unknown_batch_errors(tmp_path: Path, monkeypatch) -> None:
    """`--bulk-approve` on a batch id not in the plan fails loudly (no silent empty)."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup_many(tmp_path, monkeypatch, n=4)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    result = runner.invoke(bootstrap_main, ["--bulk-approve", "project-does-not-exist"])
    assert result.exit_code != 0
    assert "project-does-not-exist" in result.output or "no" in result.output.lower()


# --------------------------- promote always-on → REKOL.md ---------------------------


def test_bootstrap_promote_always_adds_pointer_to_rekol_md(tmp_path: Path, monkeypatch) -> None:
    """`--promote-always` adds a captured always-on file's pointer into REKOL.md."""
    from rekol.cli_bootstrap import main as bootstrap_main

    memory_home = _setup(tmp_path, monkeypatch)
    (memory_home / "REKOL.md").write_text(
        "# Memory Index (always-on)\n\nRe-injected every session.\n"
    )
    runner = CliRunner()
    result = runner.invoke(
        bootstrap_main,
        [
            "--promote-always",
            "always/ruff.md",
            "--promote-name",
            "Run ruff",
            "--promote-trigger",
            "Before committing",
        ],
    )
    assert result.exit_code == 0, result.output
    body = (memory_home / "REKOL.md").read_text()
    assert "always/ruff.md" in body
    assert "Before committing" in body


def test_bootstrap_promote_always_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Promoting the same always-on file twice does not duplicate the pointer."""
    from rekol.cli_bootstrap import main as bootstrap_main

    memory_home = _setup(tmp_path, monkeypatch)
    (memory_home / "REKOL.md").write_text("# Memory Index (always-on)\n")
    runner = CliRunner()
    args = [
        "--promote-always",
        "always/ruff.md",
        "--promote-name",
        "Ruff",
        "--promote-trigger",
        "t",
    ]
    runner.invoke(bootstrap_main, args)
    result = runner.invoke(bootstrap_main, args)
    assert result.exit_code == 0, result.output
    assert (memory_home / "REKOL.md").read_text().count("always/ruff.md") == 1


# --------------------------- end refine pass (--refine) ---------------------------


def test_bootstrap_refine_summarizes_run_for_final_pass(tmp_path: Path, monkeypatch) -> None:
    """`--refine` emits the end-of-run customize/refine summary the skill drives."""
    from rekol.cli_bootstrap import main as bootstrap_main

    _setup(tmp_path, monkeypatch)
    runner = CliRunner()
    runner.invoke(bootstrap_main, ["--all-time"])
    result = runner.invoke(bootstrap_main, ["--refine"])
    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    assert "refine" in lowered or "customize" in lowered
