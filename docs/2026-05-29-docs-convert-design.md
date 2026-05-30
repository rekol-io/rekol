# Batch Document Converter (`memory-docs-convert`) — Design

**Date:** 2026-05-29
**Status:** Approved (design), pending implementation plan
**Author:** Leon Katz (with Claude)

---

## Problem

`claude-session-index` indexes only Claude Code conversation transcripts under
`~/.claude/projects/**/*.jsonl`, matching a specific schema (`type` ∈
{user, assistant}, `uuid`, `sessionId`, `timestamp`, `message.content`). Any
other corpus of text files is invisible to `memory-search`.

The immediate driver is ~11 MB of `backstage_ai` artifacts under
`cassandra-team-workspace/sessions/` (morning briefings, weekly statuses, 1:1
prep, cost JSON, retro boards, security ticket data) organised as one folder
per topic, with many dated files inside each. This content is not in transcript
form, so it cannot enter `memory-search` without a transform.

The broader driver: there will be **future** occasions where a directory tree
of text files needs to become searchable in bulk. This tool generalises that
one operation.

## Goal

A reusable, first-class memory-tools CLI that converts an arbitrary directory
tree of text files into synthetic Claude Code JSONL transcripts, so the
**existing** `claude-session-index` ingester picks them up with **zero changes**
to the ingester or store.

## Non-Goals

- No change to `claude-session-index`, `SessionStore`, or the search path.
- No binary extraction (xlsx, png, svg) in v1.
- No HTML extraction in v1 (see Decisions).
- No chunking of large files — one file becomes one message.
- No second ingest path that writes directly to `sessions.db`.

---

## Decisions (resolved during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Tool home | First-class memory-tools CLI (`memory-docs-convert`) | Reusable toolkit member; gets shim + tests + versioning |
| Mapping | **folder → session, file → message** | Mirrors how Claude Code stores transcripts (810 sessions / 11.9k messages, avg 14.7 msgs/session). Each dated file stays an independent searchable hit. |
| Message role | `message.role = "document"` | Ingester stores `message.role or row_type`; surfaces as `[document]` in search without touching ingester code. `type` stays `"user"` to pass the `_MESSAGE_TYPES` filter. |
| File scope | Text-native only | Covers all 140 text files (~11 MB), zero new dependencies |
| HTML | **Dropped in v1** | The 13 HTML files are weekly statuses / Confluence drafts; user confirmed the content is captured elsewhere. Dropping removes all markup-parsing code and dependencies. |
| Output location | `~/.claude/projects/<prefix>/` | The dir the ingester already reads (`cfg.claude_projects_dir`) |
| Source folder | **Left untouched** | `sessions/` stays in the repo as source of truth / git history |
| Privacy | Local-only; no Dropbox, no git | `~/.claude/projects` is not synced/committed; `sessions.db` already Dropbox-excluded via xattr. No new leak surface. |
| uuids | **Deterministic** (sha1 of relative path) | Makes re-runs idempotent via the ingester's `UNIQUE(session_id, message_uuid)` |

---

## Architecture

A new CLI plus a `docs_convert/` subpackage in `memory_tools`, mirroring the
existing `sessions/` and `migrate/` subpackage structure.

```
memory-tools/
  bin/memory-docs-convert                  # shim → venv python -m memory_tools.cli_docs_convert
  src/memory_tools/
    cli_docs_convert.py                     # Click CLI: flags, orchestration, stats output
    docs_convert/
      __init__.py
      walk.py        # discover leaf folders + text files; classify by extension
      extract.py     # bytes → plain text per file type (raw passthrough)
      transcript.py  # build synthetic JSONL rows (deterministic uuids, mtime timestamps)
      writer.py      # write one .jsonl per session folder into the target dir
  tests/
    test_docs_walk.py
    test_docs_extract.py
    test_docs_transcript.py
    test_docs_convert_cli.py
    fixtures/docs_tree/...                   # tiny synthetic source tree
```

Each module has one job and a clean interface, so the schema knowledge lives in
exactly one place (`transcript.py`) and each piece tests independently:

- `walk.py`: filesystem → structured list of `(session_folder, [files])`, where
  `session_folder` is an immediate child of `SOURCE_DIR` and `files` are all
  text-native files recursively beneath it (see Folder Grouping Rule).
- `extract.py`: `(path) → Optional[str]`. The only module that knows file types.
- `transcript.py`: `(session_id, [(path, text, mtime)]) → [row dict]`. The only
  module that knows the Claude Code JSONL schema.
- `writer.py`: `(target_dir, prefix, {session → [rows]}) → [written paths]`.
- `cli_docs_convert.py`: wires the four together, prints stats, optionally chains
  the ingester.

---

## Folder Grouping Rule

A "session folder" is **each immediate child directory of `SOURCE_DIR`**. All
text files found *recursively* beneath it become messages of that one session.

This matters because files live at different depths: `Lets Get Started/` holds
files directly, while `Security/` nests them 4 levels deep
(`Security/tmsec-scope-2026-05-14/pci_investigation/*.json`). Grouping by
immediate child means "Security" is **one** session containing all its nested
files — not fragmented into one session per leaf directory. This matches the
mental model: one `backstage_ai` topic folder = one session.

- Files placed directly under `SOURCE_DIR` (not in any child dir) are grouped
  into a synthetic `_root` session.
- A child directory with **zero** text files anywhere beneath it produces **no**
  `.jsonl` (counted in `folders_seen` but not `jsonl_written`). This is why the
  stats example below shows `folders_seen=24 jsonl_written=18` — 6 folders had no
  text-native files.
- Each message's `content` is prefixed with the file's path relative to its
  session folder, so a search hit shows which nested file it came from.

## Data Flow & Synthetic JSONL Contract

Per session folder → one `.jsonl` file. Per text file → one row:

```json
{
  "type": "user",
  "uuid": "<sha1(relative_path) hex>",
  "parentUuid": null,
  "sessionId": "<sha1(folder_relative_path) hex>",
  "timestamp": "<file mtime, ISO-8601 with Z>",
  "cwd": "<absolute path of the session folder>",
  "message": { "role": "document", "content": "<rel/path.md>\n\n<extracted text>" }
}
```

Field rationale:

- **`type: "user"`** — required to pass the ingester's `_MESSAGE_TYPES` filter.
- **`message.role: "document"`** — stored role (ingester uses `message.role or
  row_type`); tags the hit as a document in search output.
- **`message.content`** — the file's path relative to its session folder, then a
  blank line, then the extracted text. The path prefix makes the originating
  nested file visible in search snippets.
- **Deterministic `uuid`** = `sha1(path relative to SOURCE_DIR)` — stable across
  runs; the ingester's `UNIQUE(session_id, message_uuid)` makes re-conversion
  idempotent.
- **`sessionId`** = `sha1(session-folder name relative to SOURCE_DIR)` — groups
  all of an immediate-child folder's (recursive) files under one session.
- **`timestamp`** = file mtime — real chronology for date-sorted search and the
  `YYYY-MM-DD` display line.
- **`cwd`** = source folder absolute path — shown in search so the origin folder
  is visible.
- **One row per file** (no chunking) — large files become one big message; FTS5
  handles arbitrary length and search snippets truncate anyway.

---

## Extraction & Classification

The only module aware of file types. Best-effort; never throws on one bad file.

**Text-native** (raw UTF-8 passthrough, `errors="replace"`):
`md, txt, log, csv, tsv, json, py, sh, xml, yaml, yml`

**Skip** (counted, never opened):
`html, htm, xlsx, png, svg, pyc, ds_store`, and any other unlisted extension.

**Guards (each lands in a stat bucket — no silent drops):**
- Empty after read (whitespace-only) → skip, `files_skipped_empty`.
- Over `--max-bytes` (default 10 MB) → skip with logged warning,
  `files_skipped_too_large`. (The largest real file is a 5.3 MB JSON, which
  passes; this is a backstop for pathological inputs.)
- Unreadable (permissions / decode failure beyond `replace`) → caught per file,
  `errors`, conversion continues.

No new dependencies: text passthrough is stdlib `open()`. HTML is out of scope
in v1, so no markup parser is needed.

---

## CLI Surface

```
memory-docs-convert SOURCE_DIR [--prefix backstage-ai-archive]
                               [--max-bytes 10485760]
                               [--index/--no-index]
                               [--dry-run]
```

- `SOURCE_DIR` — tree to convert (e.g. `…/cassandra-team-workspace/sessions`).
- `--prefix` — subdir under `~/.claude/projects/` to namespace the archive
  (default `backstage-ai-archive`).
- `--index/--no-index` (default `--index`) — after writing JSONL, chain
  `claude-session-index --full`. `--no-index` writes files only (inspectable
  artifact preserved). Resolves the "two commands" cost of the chosen approach.
- `--dry-run` — walk + classify + report what *would* be written, write nothing.
  Mirrors the `install.sh` dry-run convention.
- Target dir resolves from `cfg.claude_projects_dir` (same config the ingester
  reads), never hardcoded.

**Stats output** (matches `claude-session-index` style):

```
folders_seen=24 jsonl_written=18 files_converted=140
files_skipped_unsupported=143 files_skipped_empty=3 files_skipped_too_large=0 errors=0
```

---

## Idempotency

Three independent layers make re-running always safe:

1. Deterministic uuids → identical rows on every run.
2. Writer overwrites its own `--prefix` dir cleanly.
3. Ingester `UNIQUE(session_id, message_uuid)` dedups at insert.

---

## Error Handling

- Per-file failures are caught, counted, and reported; one bad file never aborts
  the run (explicit exception types, meaningful messages — no bare `except`).
- `SOURCE_DIR` missing / not a dir → exit 2 with a clear message.
- Target dir (`claude_projects_dir`) missing → created (mirrors ingester).
- `--index` chosen but `claude-session-index` not on PATH → warning, JSONL still
  written, non-zero advisory exit.

---

## Testing (TDD)

- **`test_docs_walk`** — leaf-folder discovery, extension classification, nested
  dirs (≥4 levels, matching `Security/tmsec-scope/.../xlsx`), empty folders.
- **`test_docs_extract`** — text passthrough; empty → None; oversize → None;
  bad-encoding → replace, no crash.
- **`test_docs_transcript`** — deterministic uuid stability (same path → same
  uuid), mtime → ISO timestamp, `role=document`, schema validity.
- **`test_docs_convert_cli`** — end-to-end on `fixtures/docs_tree/`: dry-run
  writes nothing; real run writes N jsonl; **round-trip**: convert fixture → run
  the real `SessionStore`/ingest path → assert messages are searchable. The
  round-trip is the load-bearing test — it proves the synthetic JSONL satisfies
  the real ingester, not just our mental model of it.

---

## Packaging

- `pyproject.toml` — register `memory-docs-convert =
  "memory_tools.cli_docs_convert:main"` under `[project.scripts]`.
- `bin/memory-docs-convert` — shim mirroring `bin/claude-session-index`
  (delegates to venv `python -m memory_tools.cli_docs_convert`).
- `install.sh` — add `memory-docs-convert` to the shim loop in Step 2.

---

## Out of Scope / Future

- HTML extraction (revisit if weekly-status history is needed and not captured
  elsewhere).
- xlsx extraction via openpyxl (the 123 Security/TMSEC spreadsheets).
- Per-file chunking for very large files.
- A generic multi-source "synthetic transcript" library — generalise only when a
  second concrete source type appears (YAGNI).
