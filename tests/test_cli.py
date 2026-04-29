import json
from pathlib import Path

from click.testing import CliRunner

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
