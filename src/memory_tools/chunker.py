"""Heading-aware markdown chunking for embedding.

Splits a markdown body string into :class:`Chunk` objects whose boundaries
align with markdown headings.  Sections that exceed *max_bytes* are further
split along paragraph boundaries so the embedder receives reasonably-sized
units without silently truncating content.

No I/O is performed here; the module is a pure transformation on strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Matches any ATX heading: ``# Title``, ``## Title``, … ``###### Title``
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """A single chunk of markdown content scoped to one heading section.

    Args:
        heading: The heading text (without ``#`` markers), or ``None`` for
            content that precedes the first heading.
        text: The raw markdown text for this chunk, including the heading
            line itself when present.
        line_start: 1-based line number of the first line of this chunk
            within the original document.
        line_end: 1-based line number of the last line of this chunk.
    """

    heading: Optional[str]
    text: str
    line_start: int
    line_end: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_body(body: str, max_bytes: int = 4_000) -> List[Chunk]:
    """Split a markdown body into heading-scoped chunks, splitting oversized ones.

    Primary splitting boundary is markdown headings (any level ``#``–``######``).
    Any section whose UTF-8 byte length exceeds *max_bytes* is further divided
    at blank-line (paragraph) boundaries.  The final paragraph of an oversized
    section is always emitted even if it alone would exceed *max_bytes*.

    Args:
        body: Raw markdown string (frontmatter must already be stripped).
        max_bytes: Soft upper bound on chunk size in UTF-8 bytes.  A single
            paragraph may push a chunk slightly over this limit.

    Returns:
        Ordered list of :class:`Chunk` objects.  Returns an empty list when
        *body* is blank or contains only whitespace.
    """
    if not body.strip():
        return []

    sections = _split_into_sections(body)
    result: List[Chunk] = []
    for section in sections:
        result.extend(_split_oversized(section, max_bytes))
    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _split_into_sections(body: str) -> List[Chunk]:
    """Split the body on heading lines, keeping each heading with its content.

    Content that appears before the first heading is collected as a single
    preamble chunk with ``heading=None``.

    Args:
        body: Raw markdown body string.

    Returns:
        List of :class:`Chunk` objects, one per heading section (plus an
        optional preamble chunk at index 0).
    """
    lines = body.splitlines()
    sections: List[Chunk] = []

    current_heading: Optional[str] = None
    current_start: int = 1
    current_lines: List[str] = []

    def _flush(end_line: int) -> None:
        """Emit the accumulated lines as a chunk if they contain non-blank text."""
        text = "\n".join(current_lines).strip()
        if text:
            sections.append(
                Chunk(
                    heading=current_heading,
                    text=text,
                    line_start=current_start,
                    line_end=end_line,
                )
            )

    for idx, line in enumerate(lines, start=1):
        match = HEADING_RE.match(line)
        if match:
            # Flush the previous section before starting a new one.
            _flush(idx - 1)
            current_heading = match.group(2).strip()
            current_start = idx
            current_lines = [line]
        else:
            current_lines.append(line)

    _flush(len(lines))
    return sections


def _split_oversized(chunk: Chunk, max_bytes: int) -> List[Chunk]:
    """Split *chunk* along paragraph boundaries if it exceeds *max_bytes*.

    Paragraphs are runs of text separated by blank lines.  The algorithm
    accumulates paragraphs into a buffer and emits a new sub-chunk when adding
    the next paragraph would push the buffer over *max_bytes*.  Because a
    single paragraph may itself exceed the limit, each sub-chunk may slightly
    overshoot *max_bytes* by up to one paragraph's worth of bytes.

    Args:
        chunk: The :class:`Chunk` to (potentially) split.
        max_bytes: Soft byte-size ceiling per output chunk.

    Returns:
        A list containing the original chunk unchanged when it is within the
        limit, or two or more smaller chunks when splitting was required.
        All output chunks inherit the original chunk's ``heading``.
    """
    if len(chunk.text.encode("utf-8")) <= max_bytes:
        return [chunk]

    paragraphs = re.split(r"\n\s*\n", chunk.text)
    output: List[Chunk] = []
    buffer: List[str] = []
    buffer_bytes: int = 0

    # Line tracking is approximate: we advance line_start based on newline
    # counts in emitted paragraphs plus the blank-line separators.
    current_start: int = chunk.line_start

    def _flush_buffer(end_line: int) -> None:
        """Emit the current buffer as a sub-chunk."""
        text = "\n\n".join(buffer).strip()
        if text:
            output.append(
                Chunk(
                    heading=chunk.heading,
                    text=text,
                    line_start=current_start,
                    line_end=min(end_line, chunk.line_end),
                )
            )

    for paragraph in paragraphs:
        paragraph_bytes = len(paragraph.encode("utf-8"))
        if buffer and buffer_bytes + paragraph_bytes > max_bytes:
            # Calculate approximate end line for the current buffer.
            lines_in_buffer = sum(p.count("\n") + 2 for p in buffer)
            end_line = current_start + lines_in_buffer - 1
            _flush_buffer(end_line)
            # Advance start for the next sub-chunk.
            current_start = end_line + 1
            buffer = [paragraph]
            buffer_bytes = paragraph_bytes
        else:
            buffer.append(paragraph)
            buffer_bytes += paragraph_bytes

    if buffer:
        _flush_buffer(chunk.line_end)

    return output
