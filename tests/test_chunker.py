"""Tests for heading-aware markdown chunker."""
from __future__ import annotations

from memory_tools.chunker import Chunk, chunk_body


def test_chunk_body_splits_on_headings() -> None:
    body = (
        "# Intro\n\n"
        "intro text line 1\n"
        "intro text line 2\n\n"
        "## Details\n\n"
        "detail text\n\n"
        "## More\n\n"
        "more text\n"
    )
    chunks = chunk_body(body, max_bytes=10_000)
    assert [c.heading for c in chunks] == ["Intro", "Details", "More"]
    assert chunks[0].line_start == 1
    assert chunks[0].text.startswith("# Intro")
    assert "intro text line 1" in chunks[0].text
    assert chunks[1].line_start > chunks[0].line_end


def test_chunk_body_preamble_without_heading_becomes_its_own_chunk() -> None:
    body = "preamble paragraph\n\n# First\n\nbody\n"
    chunks = chunk_body(body, max_bytes=10_000)
    assert len(chunks) == 2
    assert chunks[0].heading is None
    assert chunks[0].text.startswith("preamble")


def test_chunk_body_splits_oversized_section_by_paragraph() -> None:
    para = "lorem ipsum " * 20   # ~240 bytes
    body = "# Big\n\n" + "\n\n".join([para] * 10)   # ~2400+ bytes
    chunks = chunk_body(body, max_bytes=500)
    assert len(chunks) > 1
    assert all(len(c.text.encode("utf-8")) <= 500 + 200 for c in chunks), (
        "chunks must not exceed max_bytes by more than one paragraph"
    )
    assert all(c.heading == "Big" for c in chunks)


def test_chunk_body_empty_returns_empty_list() -> None:
    assert chunk_body("", max_bytes=500) == []
    assert chunk_body("   \n\n  \n", max_bytes=500) == []


def test_chunk_body_heading_never_isolated_when_single_paragraph_is_oversized() -> None:
    """A heading line must not flush as its own chunk; it glues to the first paragraph."""
    big_para = "x " * 400   # ~800 bytes
    body = f"# Section\n\n{big_para}\n"
    chunks = chunk_body(body, max_bytes=500)
    # The heading must appear with content, not as a standalone chunk
    assert all(
        not (c.text.strip().startswith("# ") and len(c.text.splitlines()) == 1)
        for c in chunks
    ), "No chunk may be a heading-only line"
    # First chunk carries the heading text AND has content after it
    assert chunks[0].heading == "Section"
    assert chunks[0].text.startswith("# Section")
    assert len(chunks[0].text) > len("# Section") + 10
