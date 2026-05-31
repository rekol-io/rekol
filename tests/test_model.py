"""Tests for memory file model parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from memory_tools.model import MemoryFile, ValidationError, parse_file


def test_parse_file_reads_frontmatter_and_body(tmp_path: Path) -> None:
    path = tmp_path / "prometheus.md"
    path.write_text(
        "---\n"
        "name: Prometheus canonical source\n"
        "description: Where the Prometheus URL comes from\n"
        "type: topic\n"
        "tags: [prometheus, monitoring, helm]\n"
        "aliases: [prom, prometheus url]\n"
        "see_also: [topics/reaper.md]\n"
        "created: 2026-04-27\n"
        "updated: 2026-04-27\n"
        "---\n\n"
        "# Prometheus\n\n"
        "The URL is defined in the IaC repo.\n"
    )
    f: MemoryFile = parse_file(path)
    assert f.path == path
    assert f.name == "Prometheus canonical source"
    assert f.type == "topic"
    assert f.tags == ["prometheus", "monitoring", "helm"]
    assert f.aliases == ["prom", "prometheus url"]
    assert f.see_also == ["topics/reaper.md"]
    assert f.body.startswith("# Prometheus")


def test_parse_file_accepts_missing_optional_fields(tmp_path: Path) -> None:
    path = tmp_path / "minimal.md"
    path.write_text("---\nname: Minimal\ndescription: bare minimum\ntype: always\n---\n\nbody\n")
    f = parse_file(path)
    assert f.tags == []
    assert f.aliases == []
    assert f.see_also == []


def test_parse_file_rejects_missing_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("---\ndescription: no name\ntype: topic\n---\n\nbody\n")
    with pytest.raises(ValidationError) as ei:
        parse_file(path)
    assert "name" in str(ei.value)


def test_parse_file_rejects_invalid_type(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("---\nname: x\ndescription: y\ntype: invalid_layer\n---\n\nbody\n")
    with pytest.raises(ValidationError) as ei:
        parse_file(path)
    assert "type" in str(ei.value)


def test_parse_file_rejects_missing_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("just a body, no frontmatter")
    with pytest.raises(ValidationError):
        parse_file(path)


def test_parse_file_rejects_non_list_scalar_for_list_field(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("---\nname: x\ndescription: y\ntype: topic\ntags: not-a-list\n---\n\nbody\n")
    with pytest.raises(ValidationError) as ei:
        parse_file(path)
    assert "tags" in str(ei.value)
    assert "must be a list" in str(ei.value)
