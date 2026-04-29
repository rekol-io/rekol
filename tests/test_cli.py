import json
from pathlib import Path

from click.testing import CliRunner

from memory_tools.cli_capture import main as capture_main
from memory_tools.cli_index import main as index_main
from memory_tools.cli_search import main as search_main


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
    (root / "memory.config.yaml").write_text(
        "embedding_model: test-hashing\n"
    )


def test_memory_index_rebuild_cli(tmp_path: Path, monkeypatch) -> None:
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(index_main, ["rebuild"])
    assert result.exit_code == 0, result.output
    assert "indexed" in result.output.lower()
    assert (tmp_path / ".index" / "index.db").exists()
    assert (tmp_path / "INDEX.md").exists()


def test_memory_search_cli_json(tmp_path: Path, monkeypatch) -> None:
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(index_main, ["rebuild"])
    result = runner.invoke(search_main, ["prometheus url", "--top", "3", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) >= 1
    assert any("prometheus" in hit["file_path"].lower() for hit in data)
    assert "score" in data[0]


def test_memory_capture_cli_handles_colon_in_name(tmp_path: Path, monkeypatch) -> None:
    """A name containing ': ' must not corrupt the emitted YAML."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    runner = CliRunner()

    # First establish the index
    runner.invoke(index_main, ["rebuild"])

    # Capture a new topic file whose name has a colon
    result = runner.invoke(capture_main, [
        "--layer", "topic",
        "--file", "reaper.md",
        "--name", "Reaper: canonical source",
        "--description", "Where the repair schedules come from",
        "--tags", "reaper,repair,urls",
        "--aliases", "repair schedule",
    ])
    assert result.exit_code == 0, result.output

    # File exists and parses cleanly (if YAML was invalid, reindex would have reported 0 updates)
    reaper = tmp_path / "topics" / "reaper.md"
    assert reaper.exists()
    # Re-run search and confirm the new file is reachable
    result = runner.invoke(search_main, ["reaper canonical source", "--top", "3", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert any("reaper" in hit["file_path"].lower() for hit in data), \
        f"reaper.md was not indexed after capture. Output: {result.output}"
