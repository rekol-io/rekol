"""Tests for the cross-session task layer (#113): store CAS + CLI + hook."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from rekol.cli_hooks import session_tasks
from rekol.cli_task import main as task_cli
from rekol.tasks import (
    Task,
    TaskError,
    create_task,
    list_tasks,
    parse_task,
    slugify,
    tasks_dir,
    update_task,
)

# ------------------------------- store: basics -------------------------------


def test_slugify_lowercases_and_dashes() -> None:
    assert slugify("Fix the Lane-Watch seen set!") == "fix-the-lane-watch-seen-set"
    assert slugify("???") == "task"  # degenerate title still yields a usable id


def test_create_and_parse_roundtrip(tmp_path: Path) -> None:
    path = create_task(
        tmp_path,
        Task(id="a-task", title="A task", owner_role="dev", links=["org/repo#1"], body="next: x"),
    )
    task = parse_task(path)
    assert (task.id, task.title, task.status) == ("a-task", "A task", "open")
    assert task.owner_role == "dev"
    assert task.session_id == ""
    assert task.links == ["org/repo#1"]
    assert task.body.strip() == "next: x"
    assert task.created is not None and task.updated is not None


def test_create_collision_appends_suffix_never_clobbers(tmp_path: Path) -> None:
    first = create_task(tmp_path, Task(id="same", title="first"))
    second = create_task(tmp_path, Task(id="same", title="second"))
    assert first.stem == "same" and second.stem == "same-2"
    assert parse_task(first).title == "first"  # original untouched
    assert parse_task(second).title == "second"


def test_task_id_path_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(TaskError):
        update_task(tmp_path, "../escape", lambda t: t)


def test_list_skips_malformed_and_sorts_oldest_first(tmp_path: Path) -> None:
    directory = tasks_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "new.md").write_text(
        "---\nid: new\ntitle: newer\nstatus: open\ncreated: 2026-07-30\n---\n"
    )
    (directory / "old.md").write_text(
        "---\nid: old\ntitle: older\nstatus: open\ncreated: 2026-01-01\n---\n"
    )
    (directory / "broken.md").write_text("no frontmatter at all")
    (directory / "README.md").write_text("# not a task")
    tasks = list_tasks(tmp_path)
    assert [t.id for t in tasks] == ["old", "new"]  # broken + README excluded


def test_parse_rejects_invalid_status(tmp_path: Path) -> None:
    directory = tasks_dir(tmp_path)
    directory.mkdir(parents=True)
    bad = directory / "bad.md"
    bad.write_text("---\nid: bad\ntitle: t\nstatus: doing\n---\n")
    with pytest.raises(TaskError) as ei:
        parse_task(bad)
    assert "status" in str(ei.value)


# ------------------------------- store: CAS ----------------------------------


def test_update_task_applies_mutation_atomically(tmp_path: Path) -> None:
    create_task(tmp_path, Task(id="t", title="t"))
    updated = update_task(
        tmp_path, "t", lambda t: replace(t, status="in_progress", session_id="s1")
    )
    assert (updated.status, updated.session_id) == ("in_progress", "s1")
    on_disk = parse_task(tasks_dir(tmp_path) / "t.md")
    assert (on_disk.status, on_disk.session_id) == ("in_progress", "s1")
    # No temp files left behind.
    assert not list(tasks_dir(tmp_path).glob(".*.tmp"))


def test_update_task_cas_retries_after_concurrent_write(tmp_path: Path) -> None:
    """A write landing between read and re-hash forces a retry; the retry must
    base its mutation on the OTHER writer's content (nothing lost)."""
    path = create_task(tmp_path, Task(id="t", title="t"))
    calls = {"n": 0}

    def mutate(task: Task) -> Task:
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate another session claiming the task mid-update.
            path.write_text(path.read_text().replace('owner_role: ""', 'owner_role: "qa"'))
        return replace(task, status="blocked")

    updated = update_task(tmp_path, "t", mutate)
    assert calls["n"] == 2  # first attempt lost the race, second won
    assert updated.status == "blocked"
    assert updated.owner_role == "qa"  # the concurrent claim SURVIVED the retry


def test_update_task_surfaces_persistent_contention(tmp_path: Path) -> None:
    path = create_task(tmp_path, Task(id="t", title="t"))
    counter = {"n": 0}

    def always_racing(task: Task) -> Task:
        counter["n"] += 1
        path.write_text(path.read_text() + f"\nrace {counter['n']}\n")
        return replace(task, status="done")

    with pytest.raises(TaskError) as ei:
        update_task(tmp_path, "t", always_racing)
    assert "concurrent" in str(ei.value)


def test_update_unknown_task_errors(tmp_path: Path) -> None:
    with pytest.raises(TaskError) as ei:
        update_task(tmp_path, "ghost", lambda t: t)
    assert "no such task" in str(ei.value)


# --------------------------------- CLI ---------------------------------------


@pytest.fixture()
def cli_home(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    return tmp_path


def test_cli_full_lifecycle(cli_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(task_cli, ["add", "Ship the thing", "--role", "dev", "--link", "o/r#9"])
    assert result.exit_code == 0, result.output
    assert "ship-the-thing" in result.output

    result = runner.invoke(task_cli, ["start", "ship-the-thing", "--session", "sess-1"])
    assert result.exit_code == 0, result.output
    assert "in_progress" in result.output and "sess-1" in result.output

    result = runner.invoke(task_cli, ["list"])
    assert "ship-the-thing" in result.output and "session=sess-1" in result.output

    result = runner.invoke(task_cli, ["done", "ship-the-thing"])
    assert result.exit_code == 0, result.output
    # done clears the claim so a finished task never looks resumable (#143).
    assert parse_task(tasks_dir(cli_home) / "ship-the-thing.md").session_id == ""

    result = runner.invoke(task_cli, ["list"])
    assert "no matching tasks" in result.output
    result = runner.invoke(task_cli, ["list", "--all"])
    assert "ship-the-thing" in result.output


def test_cli_block_appends_reason(cli_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(task_cli, ["add", "Parked work"])
    result = runner.invoke(task_cli, ["block", "parked-work", "--reason", "waiting on QA"])
    assert result.exit_code == 0, result.output
    task = parse_task(tasks_dir(cli_home) / "parked-work.md")
    assert task.status == "blocked"
    assert "waiting on QA" in task.body


def test_cli_unknown_id_exits_nonzero(cli_home: Path) -> None:
    result = CliRunner().invoke(task_cli, ["done", "ghost"])
    assert result.exit_code != 0
    assert "no such task" in result.output


# ------------------------------ SessionStart hook -----------------------------


def test_hook_silent_when_no_tasks(cli_home: Path) -> None:
    result = CliRunner().invoke(session_tasks, [])
    assert result.exit_code == 0
    assert result.output == ""


def test_hook_lists_open_and_in_progress_only(cli_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(task_cli, ["add", "Open one"])
    runner.invoke(task_cli, ["add", "Active one", "--role", "dev"])
    runner.invoke(task_cli, ["start", "active-one"])
    runner.invoke(task_cli, ["add", "Finished one"])
    runner.invoke(task_cli, ["done", "finished-one"])

    result = runner.invoke(session_tasks, [])
    assert result.exit_code == 0
    assert "open-one" in result.output and "active-one" in result.output
    assert "finished-one" not in result.output
    assert "rekol task" in result.output


def test_hook_caps_output_and_counts_overflow(cli_home: Path) -> None:
    runner = CliRunner()
    for i in range(12):
        runner.invoke(task_cli, ["add", f"Task number {i:02d}"])
    result = runner.invoke(session_tasks, [])
    assert result.exit_code == 0
    assert "…and 2 more" in result.output


def test_hook_never_raises_without_memory_home(monkeypatch) -> None:
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    result = CliRunner().invoke(session_tasks, [])
    assert result.exit_code == 0
    assert result.output == ""
