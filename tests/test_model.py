"""Tests for memory file model parsing."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from rekol.model import MemoryFile, ValidationError, _normalize_ts, parse_file


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


def test_parse_file_accepts_nested_metadata_type(tmp_path: Path) -> None:
    # Claude Code's built-in memory format nests `type` under `metadata:`; a
    # nested rekol-valid value must index instead of being silently rejected (#123).
    path = tmp_path / "nested.md"
    path.write_text(
        "---\nname: x\ndescription: y\nmetadata:\n  node_type: memory\n  type: topic\n---\n\nbody\n"
    )
    assert parse_file(path).type == "topic"


@pytest.mark.parametrize(
    ("harness_type", "expected_layer"),
    [("user", "always"), ("feedback", "when"), ("project", "topic"), ("reference", "knowledge")],
)
def test_parse_file_maps_nested_harness_type_to_rekol_layer(
    tmp_path: Path, harness_type: str, expected_layer: str
) -> None:
    path = tmp_path / "harness.md"
    path.write_text(
        f"---\nname: x\ndescription: y\nmetadata:\n  type: {harness_type}\n---\n\nbody\n"
    )
    assert parse_file(path).type == expected_layer


def test_parse_file_maps_flat_harness_type(tmp_path: Path) -> None:
    # A harness type written flat (not nested) is mapped too, for consistency.
    path = tmp_path / "flat-harness.md"
    path.write_text("---\nname: x\ndescription: y\ntype: feedback\n---\n\nbody\n")
    assert parse_file(path).type == "when"


def test_parse_file_flat_type_takes_precedence_over_nested(tmp_path: Path) -> None:
    path = tmp_path / "both.md"
    path.write_text(
        "---\nname: x\ndescription: y\ntype: knowledge\nmetadata:\n  type: topic\n---\n\nbody\n"
    )
    assert parse_file(path).type == "knowledge"


def test_parse_file_rejects_unknown_nested_type(tmp_path: Path) -> None:
    # Unknown values are not mapped — they still fail so typos stay visible.
    path = tmp_path / "unknown.md"
    path.write_text("---\nname: x\ndescription: y\nmetadata:\n  type: bogus\n---\n\nbody\n")
    with pytest.raises(ValidationError) as ei:
        parse_file(path)
    assert "type" in str(ei.value)


def test_normalize_ts_date_object_date_only():
    assert _normalize_ts(dt.date(2026, 5, 31), date_only=True) == "2026-05-31"


def test_normalize_ts_datetime_object_date_only():
    val = dt.datetime(2026, 5, 31, 10, 0, 0)
    assert _normalize_ts(val, date_only=True) == "2026-05-31"


def test_normalize_ts_datetime_full_uses_T_separator():
    val = dt.datetime(2026, 5, 31, 10, 0, 0)
    assert _normalize_ts(val, date_only=False) == "2026-05-31T10:00:00"


def test_normalize_ts_space_separated_string_canonicalizes_to_T():
    assert (
        _normalize_ts("2026-05-31 10:00:00+00:00", date_only=False) == "2026-05-31T10:00:00+00:00"
    )


def test_normalize_ts_string_date_only_truncates():
    assert _normalize_ts("2026-05-31T10:00:00-07:00", date_only=True) == "2026-05-31"


def test_normalize_ts_none_and_empty():
    assert _normalize_ts(None, date_only=True) is None
    assert _normalize_ts("", date_only=False) is None
