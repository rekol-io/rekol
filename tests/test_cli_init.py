"""Tests for the forked ``rekol init`` onboarding flow (T1, #39).

``rekol init`` auto-detects on ``count_claude_transcripts`` and forks:

* **Path A** (transcripts found) — three INDEPENDENT opt-in steps: index
  [default yes], memory bootstrap [now → ``rekol bootstrap`` with the shared
  scope knobs, or later], starter pack [default off].
* **Path B** (no/few transcripts) — a "light session": seed the starter pack +
  teach how to grow REKOL. No bootstrap (nothing to mine).

Every step is opt-in and re-runnable; every prompt defaults to a safe no-op.

Hermetic: ``claude_projects_dir`` is pointed at a controlled tmp dir via the
home's config file, and ``_invoke`` (the in-process sibling-subcommand dispatch)
is monkeypatched to record argv rather than actually index/bootstrap. No LLM, no
network, no real ``~/.claude`` read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner


def _home_with_projects(tmp_path: Path, monkeypatch, *, n_transcripts: int) -> tuple[Path, Path]:
    """Create a REKOL home whose config points claude_projects_dir at a tmp dir
    seeded with ``n_transcripts`` ``*.jsonl`` files. Returns (home, projects_dir).
    """
    home = tmp_path / "home"
    home.mkdir()
    projects = tmp_path / "claude-projects"
    projects.mkdir()
    for i in range(n_transcripts):
        (projects / f"sess-{i}.jsonl").write_text("{}\n", encoding="utf-8")
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {projects}\nembedding_model: test-hashing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return home, projects


# The shared tail (cloud-sync hint, notes import, legacy migration) asks a few
# more confirms after the path-specific steps. Tests append this so any trailing
# prompt defaults to its safe no-op (blank Enter) rather than running off the end
# of the input (which Click reports as an Abort).
_TAIL = "\n\n\n\n"


@pytest.fixture(autouse=True)
def _hermetic_tail(monkeypatch) -> None:
    """Stub the shared tail so init tests don't read the developer's real ~.

    ``_offer_cloud_sync_hint`` inspects real cloud-sync folders under ``$HOME``;
    stubbing it keeps the A/B-fork tests hermetic and focused on the fork itself.
    The notes-import / legacy-migration prompts stay live (they default to no-op).
    """
    monkeypatch.setattr("rekol.cli_init._offer_cloud_sync_hint", lambda: None)


@pytest.fixture
def recorded_invokes(monkeypatch) -> list[list[str]]:
    """Capture every ``_invoke`` argv so tests assert on the steps init ran."""
    calls: list[list[str]] = []

    def _fake_invoke(argv: list[str]) -> None:
        calls.append(list(argv))

    monkeypatch.setattr("rekol.cli_init._invoke", _fake_invoke)
    return calls


# --- the A/B fork -------------------------------------------------------------


def test_zero_transcripts_takes_path_b(tmp_path, monkeypatch, recorded_invokes) -> None:
    """AC: 0 transcripts → Path B (light session): seeds starter pack, no bootstrap."""
    from rekol.cli_init import main as init_main

    home, _ = _home_with_projects(tmp_path, monkeypatch, n_transcripts=0)
    runner = CliRunner()
    # Path B has no prompts of its own; the input only feeds the shared tail.
    result = runner.invoke(init_main, [], input=_TAIL)
    assert result.exit_code == 0, result.output

    out = result.output.lower()
    # Path B is named/signalled and explains how to grow REKOL over time.
    assert "no" in out and ("light" in out or "starter" in out or "grow" in out)
    # No bootstrap on Path B — nothing to mine.
    assert not any(c and c[0] == "bootstrap" for c in recorded_invokes)
    # Starter pack was seeded (the template REKOL.md landed in the home).
    assert (home / "REKOL.md").is_file()


def test_transcripts_take_path_a(tmp_path, monkeypatch, recorded_invokes) -> None:
    """AC: transcripts present → Path A. Index step offered (default yes)."""
    from rekol.cli_init import main as init_main

    _home_with_projects(tmp_path, monkeypatch, n_transcripts=12)
    runner = CliRunner()
    # index=yes(default), bootstrap=later(default no), starter=no(default).
    result = runner.invoke(init_main, [], input="\n\n\n" + _TAIL)
    assert result.exit_code == 0, result.output

    indexed = [c for c in recorded_invokes if c and c[0] == "session-index"]
    assert indexed, "Path A must offer indexing (default yes)"
    assert "12" in result.output  # surfaces the transcript count


# --- step independence + opt-in defaults --------------------------------------


def test_path_a_steps_are_independent_index_no_bootstrap_yes(
    tmp_path, monkeypatch, recorded_invokes
) -> None:
    """Declining index must not block opting into bootstrap (steps independent)."""
    from rekol.cli_init import main as init_main

    _home_with_projects(tmp_path, monkeypatch, n_transcripts=5)
    runner = CliRunner()
    # index? no | bootstrap now? yes | scope-days blank | projects blank |
    # max-sessions blank | all-time? no | starter? no
    result = runner.invoke(init_main, [], input="n\ny\n\n\n\nn\nn\n" + _TAIL)
    assert result.exit_code == 0, result.output

    assert not any(c and c[0] == "session-index" for c in recorded_invokes)
    assert any(c and c[0] == "bootstrap" for c in recorded_invokes), (
        "bootstrap must be reachable even when index was declined"
    )


def test_path_a_bootstrap_later_does_not_invoke(tmp_path, monkeypatch, recorded_invokes) -> None:
    """Choosing 'later' for bootstrap defers it (safe no-op) but tells you how to run it."""
    from rekol.cli_init import main as init_main

    _home_with_projects(tmp_path, monkeypatch, n_transcripts=5)
    runner = CliRunner()
    # index? yes | bootstrap now? no(default) | starter? no(default)
    result = runner.invoke(init_main, [], input="\nn\nn\n" + _TAIL)
    assert result.exit_code == 0, result.output

    assert not any(c and c[0] == "bootstrap" for c in recorded_invokes)
    # Re-runnable: the deferral tells the user how to do it later.
    assert "rekol bootstrap" in result.output


# --- scope control wiring (consistent with T3) --------------------------------


def test_path_a_bootstrap_now_threads_scope_flags(tmp_path, monkeypatch, recorded_invokes) -> None:
    """Opting into bootstrap surfaces the scope control and threads it to T3's flags."""
    from rekol.cli_init import main as init_main

    _home_with_projects(tmp_path, monkeypatch, n_transcripts=5)
    runner = CliRunner()
    # index? no | bootstrap now? yes | scope-days? 30 | projects? (blank=all) |
    # max-sessions? (blank=default) | all-time? no(default) | starter? no
    result = runner.invoke(init_main, [], input="n\ny\n30\n\n\nn\nn\n" + _TAIL)
    assert result.exit_code == 0, result.output

    boot = next((c for c in recorded_invokes if c and c[0] == "bootstrap"), None)
    assert boot is not None, "bootstrap must have been invoked"
    assert "--scope-days" in boot and "30" in boot


# --- starter pack opt-in (Path A) ---------------------------------------------


def test_path_a_starter_pack_off_by_default(tmp_path, monkeypatch, recorded_invokes) -> None:
    """On Path A the starter pack is OFF by default — declined leaves home unseeded."""
    from rekol.cli_init import main as init_main

    home, _ = _home_with_projects(tmp_path, monkeypatch, n_transcripts=5)
    runner = CliRunner()
    # index? yes | bootstrap? no | starter pack? no(default)
    result = runner.invoke(init_main, [], input="\nn\nn\n" + _TAIL)
    assert result.exit_code == 0, result.output
    assert not (home / "REKOL.md").exists(), "starter pack must be opt-in on Path A"


def test_path_a_starter_pack_opt_in_seeds(tmp_path, monkeypatch, recorded_invokes) -> None:
    """Opting into the starter pack on Path A seeds the template into the home."""
    from rekol.cli_init import main as init_main

    home, _ = _home_with_projects(tmp_path, monkeypatch, n_transcripts=5)
    runner = CliRunner()
    # index? yes | bootstrap? no | starter pack? yes
    result = runner.invoke(init_main, [], input="\nn\ny\n" + _TAIL)
    assert result.exit_code == 0, result.output
    assert (home / "REKOL.md").is_file()


# --- re-runnable / idempotent -------------------------------------------------


def test_starter_pack_seed_is_idempotent_across_runs(
    tmp_path, monkeypatch, recorded_invokes
) -> None:
    """Re-running init never clobbers an existing home file (every step re-runnable)."""
    from rekol.cli_init import main as init_main

    home, _ = _home_with_projects(tmp_path, monkeypatch, n_transcripts=0)
    runner = CliRunner()
    runner.invoke(init_main, [], input=_TAIL)  # Path B seeds the pack
    # User edits a seeded file.
    rekol_md = home / "REKOL.md"
    rekol_md.write_text("MY EDITS\n", encoding="utf-8")

    result = runner.invoke(init_main, [], input=_TAIL)  # re-run
    assert result.exit_code == 0, result.output
    assert rekol_md.read_text(encoding="utf-8") == "MY EDITS\n", "re-run must not clobber edits"
