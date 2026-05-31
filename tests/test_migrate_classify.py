"""Tests for classifier: deterministic mapping + LLM fallback."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from memory_tools.migrate.classify import (
    build_classifier_prompt,
    classify_file,
    heuristic_classify,
    target_filename_for,
)
from memory_tools.migrate.discover import LegacyFile
from memory_tools.migrate.llm import LLMUnavailable


def _mk_file(tmp_path: Path, name: str, body: str) -> LegacyFile:
    src = tmp_path / "proj" / "memory"
    src.mkdir(parents=True, exist_ok=True)
    f = src / name
    f.write_text(body)
    return LegacyFile(source_path=f, source_root=src, project_slug="proj")


def test_heuristic_feedback_routes_to_when(tmp_path: Path) -> None:
    body = "---\nname: Foo feedback\ndescription: d\ntype: feedback\n---\n\nFoo rule body."
    lf = _mk_file(tmp_path, "feedback_foo.md", body)
    c = heuristic_classify(lf)
    assert c is not None
    assert c.layer == "when"
    assert c.method == "heuristic"
    assert c.frontmatter["name"] == "Foo feedback"
    assert c.frontmatter["description"] == "d"
    assert c.frontmatter["type"] == "when"  # rewritten — feedback→when
    assert "Foo rule body" in c.body


def test_heuristic_project_routes_to_topic(tmp_path: Path) -> None:
    body = "---\nname: Alpha\ndescription: d\ntype: project\n---\n\nbody"
    lf = _mk_file(tmp_path, "project_alpha.md", body)
    c = heuristic_classify(lf)
    assert c is not None
    assert c.layer == "topic"
    assert c.frontmatter["type"] == "topic"


def test_heuristic_reference_routes_to_topic(tmp_path: Path) -> None:
    body = "---\nname: Ref\ndescription: d\ntype: reference\n---\n\nbody"
    lf = _mk_file(tmp_path, "reference_foo.md", body)
    c = heuristic_classify(lf)
    assert c is not None
    assert c.layer == "topic"


def test_heuristic_always_preserved(tmp_path: Path) -> None:
    body = "---\nname: Me\ndescription: d\ntype: always\n---\n\nbody"
    lf = _mk_file(tmp_path, "identity.md", body)
    c = heuristic_classify(lf)
    assert c is not None
    assert c.layer == "always"


def test_heuristic_knowledge_preserved(tmp_path: Path) -> None:
    body = "---\nname: K\ndescription: d\ntype: knowledge\n---\n\nbody"
    lf = _mk_file(tmp_path, "fact.md", body)
    c = heuristic_classify(lf)
    assert c is not None
    assert c.layer == "knowledge"


def test_heuristic_returns_none_when_no_frontmatter(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "raw.md", "# Just a heading\n\nno frontmatter.")
    assert heuristic_classify(lf) is None


def test_heuristic_returns_none_when_unknown_type(tmp_path: Path) -> None:
    body = "---\nname: X\ndescription: d\ntype: mystery\n---\n\nbody"
    lf = _mk_file(tmp_path, "x.md", body)
    assert heuristic_classify(lf) is None


def test_target_filename_prefixes_project_slug(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "feedback_foo.md", "x")
    # For 'feedback_foo.md' under 'proj', target should strip the 'feedback_'
    # prefix (which is just the legacy type hint) and prepend the slug.
    out = target_filename_for(lf, layer="when")
    assert out == "when-proj-foo.md"


def test_target_filename_handles_project_prefix(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "project_alpha.md", "x")
    out = target_filename_for(lf, layer="topic")
    assert out == "proj-alpha.md"


def test_target_filename_no_known_prefix(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "identity.md", "x")
    out = target_filename_for(lf, layer="always")
    assert out == "proj-identity.md"


def test_classify_file_uses_heuristic_when_possible(tmp_path: Path) -> None:
    body = "---\nname: F\ndescription: d\ntype: feedback\n---\n\nbody"
    lf = _mk_file(tmp_path, "feedback_f.md", body)
    with patch("memory_tools.migrate.classify.call_claude_classifier") as llm:
        c = classify_file(lf, index_context="", allow_llm=True)
    assert c.layer == "when"
    assert c.method == "heuristic"
    llm.assert_not_called()


def test_classify_file_falls_back_to_llm(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "x.md", "# just body, no frontmatter")
    with patch("memory_tools.migrate.classify.call_claude_classifier") as llm:
        llm.return_value = {
            "layer": "topic",
            "filename": "topic-proj-x.md",
            "tags": ["t1"],
            "aliases": ["a1"],
            "rationale": "reason",
        }
        c = classify_file(lf, index_context="# INDEX", allow_llm=True)
    assert c.layer == "topic"
    assert c.method == "llm"
    assert c.frontmatter["tags"] == ["t1"]
    assert c.frontmatter["aliases"] == ["a1"]


def test_classify_file_defaults_to_knowledge_when_llm_disabled(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "x.md", "# just body")
    c = classify_file(lf, index_context="", allow_llm=False)
    assert c.layer == "knowledge"
    assert c.method == "heuristic"
    assert c.frontmatter["type"] == "knowledge"


def test_classify_file_defaults_to_knowledge_when_llm_unavailable(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "x.md", "# just body")
    with patch(
        "memory_tools.migrate.classify.call_claude_classifier",
        side_effect=LLMUnavailable("no claude"),
    ):
        c = classify_file(lf, index_context="", allow_llm=True)
    assert c.layer == "knowledge"
    assert c.method == "heuristic"


def test_build_classifier_prompt_contains_required_keys() -> None:
    p = build_classifier_prompt()
    assert "layer" in p
    assert "filename" in p
    assert "tags" in p
    assert "aliases" in p
    # Layers named
    for layer in ("always", "when", "topic", "knowledge"):
        assert layer in p
