import json
from datetime import datetime
from pathlib import Path

import frontmatter
from click.testing import CliRunner

from rekol.cli_capture import main as capture_main
from rekol.cli_index import main as index_main
from rekol.cli_invalidate import main as invalidate_main
from rekol.cli_propose import extract_candidates
from rekol.cli_propose import main as propose_main
from rekol.cli_search import main as search_main


def _seed_memory(root: Path) -> None:
    (root / "topics").mkdir(parents=True, exist_ok=True)
    (root / "topics" / "prometheus.md").write_text(
        "---\n"
        "name: Prometheus\n"
        "description: URL source\n"
        "type: topic\n"
        "tags: [prometheus, urls]\n"
        "aliases: [prom, prometheus url]\n"
        "---\n\n"
        "# Prometheus\n\nURL lives in the IaC repo.\n"
    )
    (root / "memory.config.yaml").write_text("embedding_model: test-hashing\n")


def test_memory_index_rebuild_cli(tmp_path: Path, monkeypatch) -> None:
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(index_main, ["rebuild"])
    assert result.exit_code == 0, result.output
    assert "indexed" in result.output.lower()
    assert (tmp_path / ".index" / "index.db").exists()
    assert (tmp_path / ".index" / "INDEX.md").exists()
    # Root-level INDEX.md is no longer used; only MEMORY.md belongs at root
    assert not (tmp_path / "INDEX.md").exists()


def test_memory_search_cli_json(tmp_path: Path, monkeypatch) -> None:
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])
    result = runner.invoke(search_main, ["prometheus url", "--top", "3", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # New JSON shape (Phase 2): top-level object {query, memory, sessions,
    # is_promotion_candidate, sources_queried} — flat-array shape is gone.
    assert isinstance(data, dict)
    assert data["query"] == "prometheus url"
    hits = data["memory"]
    assert len(hits) >= 1
    assert any("prometheus" in hit["file_path"].lower() for hit in hits)
    assert "cosine_score" in hits[0]
    assert "final_score" in hits[0]
    assert "memory" in data["sources_queried"]


def test_memory_capture_cli_handles_colon_in_name(tmp_path: Path, monkeypatch) -> None:
    """A name containing ': ' must not corrupt the emitted YAML."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()

    # First establish the index
    runner.invoke(index_main, ["rebuild"])

    # Capture a new topic file whose name has a colon
    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "reaper.md",
            "--name",
            "Reaper: canonical source",
            "--description",
            "Where the repair schedules come from",
            "--tags",
            "reaper,repair,urls",
            "--aliases",
            "repair schedule",
        ],
    )
    assert result.exit_code == 0, result.output

    # File exists and parses cleanly (if YAML was invalid, reindex would have reported 0 updates)
    reaper = tmp_path / "topics" / "reaper.md"
    assert reaper.exists()
    # Re-run search and confirm the new file is reachable
    result = runner.invoke(search_main, ["reaper canonical source", "--top", "3", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert any("reaper" in hit["file_path"].lower() for hit in data["memory"]), (
        f"reaper.md was not indexed after capture. Output: {result.output}"
    )


def test_memory_capture_rejects_near_duplicate(tmp_path: Path, monkeypatch) -> None:
    """Capturing a body that is near-identical to an existing memory must
    fail with a non-zero exit and no file written, unless --force."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])

    # First capture establishes the canonical entry
    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "alpha.md",
            "--name",
            "Alpha service",
            "--description",
            "API service for alpha data",
            "--body",
            "Alpha service runs on port 9001 in cluster east-1.\n"
            "Owned by the platform team.  Restart via `kubectl rollout`.",
        ],
    )
    assert result.exit_code == 0, result.output

    # Second capture with the same body but different filename should be rejected
    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "alpha-2.md",
            "--name",
            "Alpha service",
            "--description",
            "API service for alpha data",
            "--body",
            "Alpha service runs on port 9001 in cluster east-1.\n"
            "Owned by the platform team.  Restart via `kubectl rollout`.",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "near-duplicate" in result.output
    assert "alpha.md" in result.output
    assert not (tmp_path / "topics" / "alpha-2.md").exists()

    # --force bypasses the check and writes anyway
    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "alpha-2.md",
            "--name",
            "Alpha service v2",
            "--description",
            "API service for alpha data",
            "--body",
            "Alpha service runs on port 9001 in cluster east-1.\n"
            "Owned by the platform team.  Restart via `kubectl rollout`.",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "topics" / "alpha-2.md").exists()


def test_memory_capture_conflict_check_works_for_large_bodies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Regression: the conflict check must chunk the candidate body the same
    way the indexer chunks stored content.  Embedding the full unsliced body
    against per-chunk stored vectors underestimates cosine similarity for any
    file larger than chunk_max_bytes, silently letting duplicates through.
    """
    # Use a deliberately small chunk size so it's easy to exceed.
    (tmp_path / "topics").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory.config.yaml").write_text(
        "embedding_model: test-hashing\nchunk_max_bytes: 200\n"
    )
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()

    # Build a body well above 200 bytes with multiple distinct sections.
    big_body = (
        "# Section A\n"
        "Alpha service deployment lives in the platform/iac repo. "
        "Helm chart at charts/alpha; values in values.alpha.yaml. "
        "Restart via kubectl rollout restart deployment/alpha -n platform.\n\n"
        "# Section B\n"
        "Logs ship to Loki at loki.platform.svc:3100; dashboards in Grafana "
        "folder 'Platform/Alpha'. PagerDuty service id PD-ALPHA-PROD.\n\n"
        "# Section C\n"
        "On-call rotation managed in Opsgenie team 'platform'. "
        "Escalation policy: 5min L1, 10min L2, 15min manager.\n"
    )
    assert len(big_body) > 200, len(big_body)

    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "alpha-big.md",
            "--name",
            "Alpha service",
            "--description",
            "Alpha deployment, logs, on-call",
            "--body",
            big_body,
        ],
    )
    assert result.exit_code == 0, result.output

    # Now try to capture the same body with a different filename — should be
    # rejected as a near-duplicate even though the body is much larger than
    # chunk_max_bytes.
    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "alpha-big-2.md",
            "--name",
            "Alpha service",
            "--description",
            "Alpha deployment, logs, on-call",
            "--body",
            big_body,
        ],
    )
    assert result.exit_code != 0, result.output
    assert "near-duplicate" in result.output
    assert "alpha-big.md" in result.output


def test_memory_capture_emits_valid_from(tmp_path: Path, monkeypatch) -> None:
    """A captured file gets a ``valid_from`` field defaulting to today."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])
    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "with-valid-from.md",
            "--name",
            "Service Z",
            "--description",
            "Z service URL",
            "--body",
            "Z service runs at https://z.internal:8443",
        ],
    )
    assert result.exit_code == 0, result.output
    post = frontmatter.load(tmp_path / "topics" / "with-valid-from.md")
    assert post.metadata.get("valid_from"), post.metadata
    # And user-provided --valid-from is honored
    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "back-dated.md",
            "--name",
            "Y service",
            "--description",
            "Y service legacy URL",
            "--body",
            "Y was at port 7000 in the old cluster",
            "--valid-from",
            "2024-06-01",
        ],
    )
    assert result.exit_code == 0, result.output
    post = frontmatter.load(tmp_path / "topics" / "back-dated.md")
    assert post.metadata["valid_from"] == "2024-06-01"


def test_memory_invalidate_marks_file(tmp_path: Path, monkeypatch) -> None:
    """memory-invalidate sets ``invalidated_at`` and bumps ``updated``,
    leaves the file on disk and in the index, and rejects double-invalidation."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])

    target = tmp_path / "topics" / "prometheus.md"
    result = runner.invoke(invalidate_main, [str(target), "--reason", "decommissioned"])
    assert result.exit_code == 0, result.output

    post = frontmatter.load(target)
    assert post.metadata.get("invalidated_at"), post.metadata
    assert "decommissioned" in post.content
    # File still on disk and still searchable (de-prioritization is a retrieval-
    # side concern; the indexer just re-embeds whatever is in the body)
    assert target.exists()

    # Second invalidate refuses
    result = runner.invoke(invalidate_main, [str(target)])
    assert result.exit_code != 0
    assert "already invalidated" in result.output


def test_memory_capture_project_scoping(tmp_path: Path, monkeypatch) -> None:
    """--project <slug> writes to projects/<slug>/<layer>/<file>; the indexer
    finds it; INDEX.md surfaces a per-project section."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])

    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "phase2.md",
            "--name",
            "Phase 2 architecture",
            "--description",
            "Transformer agent details",
            "--body",
            "Phase 2 uses a 4L x 256d x 4h transformer at ~3.16M params.",
            "--project",
            "math-evolution-agent",
        ],
    )
    assert result.exit_code == 0, result.output

    expected = tmp_path / "projects" / "math-evolution-agent" / "topics" / "phase2.md"
    assert expected.exists(), list((tmp_path / "projects").rglob("*"))

    # Reachable via search after capture
    result = runner.invoke(search_main, ["transformer agent", "--top", "5", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert any("phase2.md" in hit["file_path"] for hit in data["memory"]), result.output

    # INDEX.md has a per-project section
    index_md = (tmp_path / ".index" / "INDEX.md").read_text()
    assert "## projects/" in index_md
    assert "### math-evolution-agent" in index_md
    assert "phase2.md" in index_md

    # Bad slug rejected
    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "x.md",
            "--name",
            "x",
            "--description",
            "x",
            "--body",
            "x",
            "--project",
            "../escape",
        ],
    )
    assert result.exit_code != 0
    assert "alphanumeric" in result.output


def test_memory_invalidate_refuses_path_outside_memory_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    # Create a markdown file OUTSIDE memory_home
    outside = tmp_path.parent / "outside-memory-home.md"
    outside.write_text("---\nname: x\ndescription: y\ntype: topic\n---\n\nbody\n")
    try:
        runner = CliRunner()
        result = runner.invoke(invalidate_main, [str(outside)])
        assert result.exit_code != 0
        assert "not under MEMORY_HOME" in result.output
    finally:
        outside.unlink(missing_ok=True)


def test_extract_candidates_finds_memorable_lines() -> None:
    text = """
    Decision: use sqlite-vec for the index.
    - remember: prefer absolute paths in bash
    Note: the dev server runs on port 9001
    plain prose without any signal
    Don't forget to invalidate stale entries
    you forgot the pre-commit hook
    """
    candidates = extract_candidates(text)
    assert "use sqlite-vec for the index" in candidates
    assert any("absolute paths" in c for c in candidates)
    assert any("dev server runs on port 9001" in c for c in candidates)
    assert any("invalidate stale" in c.lower() for c in candidates)
    assert any("pre-commit" in c for c in candidates)
    assert "plain prose without any signal" not in candidates


def test_memory_propose_writes_pending_review(tmp_path: Path, monkeypatch) -> None:
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])

    notes = tmp_path / "notes.md"
    notes.write_text(
        "Today's session:\n"
        "- Decision: use sqlite-vec for the index\n"
        "- Note: deploy via docker compose pull on Dell\n"
        "Random unrelated paragraph.\n"
    )
    result = runner.invoke(propose_main, ["-i", str(notes)])
    assert result.exit_code == 0, result.output

    pending = list((tmp_path / "pending-review").glob("*.md"))
    assert len(pending) == 1, list((tmp_path / "pending-review").iterdir())
    body = pending[0].read_text()
    # Each candidate gets a checklist item
    assert "use sqlite-vec for the index" in body
    assert "deploy via docker compose pull on Dell" in body
    # Plain prose without signal markers is not surfaced
    assert "Random unrelated paragraph" not in body


def test_memory_capture_timestamps_are_datetime(tmp_path: Path, monkeypatch) -> None:
    """Captured ``created`` and ``updated`` are full ISO-8601 datetimes with
    timezone offset, not just calendar dates.  This makes ordering between
    same-day captures unambiguous and supports time-anchored queries.

    ``valid_from`` stays date-only because its semantics are "started being
    true on this day", not "captured at this instant".
    """
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])

    result = runner.invoke(
        capture_main,
        [
            "--layer",
            "topic",
            "--file",
            "timestamped.md",
            "--name",
            "Time-stamped capture",
            "--description",
            "Tests that captures include datetime + offset",
            "--body",
            "A small body so the conflict check has something to compare.",
        ],
    )
    assert result.exit_code == 0, result.output

    post = frontmatter.load(tmp_path / "topics" / "timestamped.md")
    for field in ("created", "updated"):
        value = post.metadata[field]
        assert "T" in value, (
            f"expected ISO-8601 datetime with T separator for {field}, got {value!r}"
        )
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None, (
            f"expected timezone-aware datetime for {field}, got {value!r}"
        )

    # valid_from stays date-only — different semantics from created/updated.
    valid_from = post.metadata["valid_from"]
    assert "T" not in valid_from, f"expected date-only valid_from, got {valid_from!r}"


def test_memory_invalidate_writes_datetime(tmp_path: Path, monkeypatch) -> None:
    """memory-invalidate writes datetime timestamps to invalidated_at + updated
    (not just calendar dates) so same-day invalidations order unambiguously."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])

    target = tmp_path / "topics" / "prometheus.md"
    result = runner.invoke(invalidate_main, [str(target)])
    assert result.exit_code == 0, result.output

    post = frontmatter.load(target)
    for field in ("invalidated_at", "updated"):
        value = post.metadata[field]
        assert "T" in value, (
            f"expected ISO-8601 datetime with T separator for {field}, got {value!r}"
        )
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None, (
            f"expected timezone-aware datetime for {field}, got {value!r}"
        )


def test_memory_search_source_memory_skips_sessions(tmp_path, monkeypatch):
    # Set up a memory-only environment; sessions DB will be empty
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {tmp_path}/no-projects-here\n"
    )
    (home / "topics").mkdir()
    (home / "topics" / "litellm.md").write_text(
        "---\nname: litellm\ndescription: notes\ntype: reference\n"
        "tags: [litellm]\naliases: [base url]\ncreated: 2026-05-01T00:00:00+00:00\n"
        "updated: 2026-05-01T00:00:00+00:00\n---\n"
        "## LiteLLM\n\nbase_url for the proxy.\n"
    )
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])

    result = runner.invoke(search_main, ["litellm", "--source", "memory"])
    assert result.exit_code == 0, result.output
    assert "FROM MEMORY" in result.output
    assert "FROM SESSIONS" not in result.output


def test_memory_search_promote_candidates_flag(tmp_path, monkeypatch):
    # Memory empty, sessions populated — query surfaces as a promotion candidate
    home = tmp_path / "h"
    home.mkdir()
    fake_projects = tmp_path / "projects" / "proj"
    fake_projects.mkdir(parents=True)
    import shutil

    shutil.copy(
        Path(__file__).parent / "fixtures" / "sample_session.jsonl",
        fake_projects / "session.jsonl",
    )
    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {fake_projects.parent}\n"
    )

    from rekol.cli_session_index import main as session_idx_main

    runner = CliRunner()
    setup_result = runner.invoke(session_idx_main, ["--full"])
    assert setup_result.exit_code == 0, setup_result.output

    result = runner.invoke(search_main, ["hello there", "--promote-candidates"])
    assert result.exit_code == 0, result.output
    # Confirm the promotion-candidate annotation fired because sessions
    # actually contributed, not because of an unrelated empty-search path.
    assert "FROM SESSIONS (top " in result.output
    # Sanity: 'top 0' would mean sessions didn't actually hit, which would
    # make the promotion-candidate assertion below a false signal.
    assert "(top 0)" not in result.output, result.output
    # promote-candidates surface should mention session count + zero memory hits
    assert "promotion candidate" in result.output.lower()


def test_search_has_include_invalidated_flag() -> None:
    res = CliRunner().invoke(search_main, ["--help"])
    assert "--include-invalidated" in res.output


def test_outdated_schema_instructs_rebuild(tmp_path: Path, monkeypatch) -> None:
    import sqlite3

    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    (tmp_path / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")
    idx = tmp_path / ".index"
    idx.mkdir()
    con = sqlite3.connect(idx / "index.db")
    con.execute(
        "CREATE TABLE files (path TEXT PRIMARY KEY, mtime INT, content_hash TEXT, indexed_at INT)"
    )
    con.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT, heading TEXT, "
        "line_start INT, line_end INT, text TEXT, tags_json TEXT, aliases_json TEXT, embedding BLOB)"
    )
    con.commit()
    con.close()
    res = CliRunner().invoke(search_main, ["something", "--source", "memory"])
    assert "rekol index rebuild" in res.output


def test_overdue_durable_tag_helper() -> None:
    import datetime as dt
    from pathlib import Path

    from rekol.cli_search import _review_tag

    hit = {"file_path": "/m/knowledge/x.md", "updated": "2020-01-01"}
    assert (
        _review_tag(
            hit,
            memory_home=Path("/m"),
            exempt_layers=["knowledge"],
            interval_days=180,
            today=dt.date(2026, 6, 1),
        )
        == " [review?]"
    )
    fresh = {"file_path": "/m/topics/y.md", "updated": "2026-05-31"}
    assert (
        _review_tag(
            fresh,
            memory_home=Path("/m"),
            exempt_layers=["knowledge"],
            interval_days=180,
            today=dt.date(2026, 6, 1),
        )
        == ""
    )


def test_relative_phrasing_helper() -> None:
    import datetime as dt

    from rekol.cli_search import _relative_age

    assert _relative_age(dt.date(2026, 5, 11), today=dt.date(2026, 6, 1)) == "3 weeks ago"
    assert _relative_age(dt.date(2026, 6, 1), today=dt.date(2026, 6, 1)) == "today"
