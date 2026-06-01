#!/usr/bin/env bats
# Installer smoke-tests for rekol/install.sh.
#
# Run with:   bats rekol/tests/test_install.bats
# Requires:   bats-core (brew install bats-core) and internet access for the
#             first real-install test (downloads sentence-transformers model).
#
# On a machine without bats, this file documents manual test steps.
#
# These tests deliberately export MEMORY_HOME (not REKOL_HOME) so the installer's
# MEMORY_HOME-fallback path stays exercised — that path guards existing installs.

setup() {
    TESTROOT="$(mktemp -d)"
    # Export MEMORY_HOME (the fallback) and unset REKOL_HOME so the resolver
    # exercises the fallback path; the missing-home test unsets both.
    export MEMORY_HOME="${TESTROOT}/mem"
    unset REKOL_HOME || true
    TOOLS_HOME="${TESTROOT}/tools"
    BIN_DIR="${TESTROOT}/bin"
    # Resolve component dir relative to this test file
    COMPONENT_DIR="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    export COMPONENT_DIR TOOLS_HOME BIN_DIR TESTROOT
}

teardown() {
    rm -rf "${TESTROOT}"
}

# ---------------------------------------------------------------------------
# Test 1 — dry-run leaves MEMORY_HOME entirely empty
# ---------------------------------------------------------------------------
@test "dry-run does not create MEMORY_HOME contents" {
    run "${COMPONENT_DIR}/install.sh" \
        --dry-run \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    [ "$status" -eq 0 ]

    # MEMORY_HOME directory itself should not exist (mkdir -p is dry-run wrapped)
    [ ! -d "${MEMORY_HOME}/always" ]
    [ ! -d "${MEMORY_HOME}/.index" ]
    [ ! -f "${MEMORY_HOME}/REKOL.md" ]
}

# ---------------------------------------------------------------------------
# Test 2 — real install seeds MEMORY_HOME and builds the index
# ---------------------------------------------------------------------------
@test "real install seeds MEMORY_HOME, builds index, creates INDEX.md" {
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    [ "$status" -eq 0 ]

    # Template content seeded correctly
    [ -f "${MEMORY_HOME}/REKOL.md" ]
    [ -f "${MEMORY_HOME}/always/identity.md" ]
    [ -d "${MEMORY_HOME}/when" ]
    [ -d "${MEMORY_HOME}/topics" ]

    # rekol.config.yaml was created from .example
    [ -f "${MEMORY_HOME}/rekol.config.yaml" ]
    [ ! -f "${MEMORY_HOME}/rekol.config.yaml.example" ]

    # Index built
    [ -f "${MEMORY_HOME}/.index/index.db" ]

    # INDEX.md regenerated under .index/ (not at root)
    [ -f "${MEMORY_HOME}/.index/INDEX.md" ]

    # .dropboxignore created
    [ -f "${MEMORY_HOME}/.dropboxignore" ]
}

# ---------------------------------------------------------------------------
# Test 3 — rerun is idempotent; seeded files are not modified
# ---------------------------------------------------------------------------
@test "rerun is safe and does not overwrite seeded files" {
    # First install
    "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    HASH1="$(shasum "${MEMORY_HOME}/always/identity.md" | awk '{print $1}')"

    # Second install — must not change user content
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    [ "$status" -eq 0 ]

    HASH2="$(shasum "${MEMORY_HOME}/always/identity.md" | awk '{print $1}')"
    [ "$HASH1" = "$HASH2" ]

    # Two separate journal files should exist
    JOURNAL_COUNT="$(find "${MEMORY_HOME}/.install-logs" -name '.install-journal-*' | wc -l | tr -d ' ')"
    [ "$JOURNAL_COUNT" -ge 2 ]
}

# ---------------------------------------------------------------------------
# Test 4 — the rekol shim errors clearly when the venv is absent
# ---------------------------------------------------------------------------
@test "shim exits 2 with helpful message when venv is missing" {
    run env REKOL_TOOLS_HOME="${TOOLS_HOME}" \
        "${COMPONENT_DIR}/bin/rekol" search identity --top 1

    [ "$status" -eq 2 ]
    [[ "$output" == *"run installer"* ]]
}

# ---------------------------------------------------------------------------
# Test 5 — the rekol shim works after install
# ---------------------------------------------------------------------------
@test "rekol search returns at least one result after install" {
    "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    run env REKOL_TOOLS_HOME="${TOOLS_HOME}" \
        "${BIN_DIR}/rekol" search identity --top 2

    [ "$status" -eq 0 ]
    [[ "$output" == *"identity"* ]]
}

# ---------------------------------------------------------------------------
# Test 6 — missing home (neither REKOL_HOME nor MEMORY_HOME) fails with error
# ---------------------------------------------------------------------------
@test "missing REKOL_HOME and MEMORY_HOME exits 2 with error message" {
    run env -u REKOL_HOME -u MEMORY_HOME \
        "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    [ "$status" -eq 2 ]
    [[ "$output" == *"REKOL_HOME"* ]]
}

# ---------------------------------------------------------------------------
# Test 7 — --test-mode does not modify ~/.zshrc
# ---------------------------------------------------------------------------
@test "test-mode does not modify ~/.zshrc" {
    ZSHRC_BEFORE="$(md5 -q "$HOME/.zshrc" 2>/dev/null || echo '')"
    run "${COMPONENT_DIR}/install.sh" --test-mode --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"
    [ "$status" -eq 0 ]
    ZSHRC_AFTER="$(md5 -q "$HOME/.zshrc" 2>/dev/null || echo '')"
    [ "$ZSHRC_BEFORE" = "$ZSHRC_AFTER" ]
}

@test "install wires SessionEnd transcript-index hook and backfills sessions" {
  # Verifies FIX 1: install.sh merges the SessionEnd hook (Step 7D) so
  # transcripts index automatically, and backfills existing history (Step 9.5).
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  # Sandbox HOME so the hook merge targets a throwaway settings.json, never the
  # real ~/.claude/settings.json.
  SBHOME="$TESTROOT/sandhome"
  mkdir -p "$SBHOME/.claude/projects/proj"
  # A real transcript to backfill (the same fixture the Python tests use).
  cp "$COMPONENT_DIR/tests/fixtures/sample_session.jsonl" \
     "$SBHOME/.claude/projects/proj/session.jsonl"

  # Pre-seed REKOL_HOME with a test-hashing config so the home is non-empty
  # (skips template seeding) and both the Step 9 rebuild and Step 9.5 backfill
  # use the fast hashing embedder — no sentence-transformers model download.
  # git_track: false is required — install.sh Step 8.5 greps the config for it
  # under `set -o pipefail`, so a config missing the key aborts the install.
  REKOLH="$TESTROOT/rekolhome"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: true\ngit_track: false\nclaude_projects_dir: %s/.claude/projects\n' \
    "$SBHOME" > "$REKOLH/rekol.config.yaml"

  # Hooks ON (no --no-hook); skip skill/shellrc to keep side effects in the box.
  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # Step 7D: the SessionEnd hook now carries the transcript-index command.
  run jq -e '.hooks.SessionEnd[].hooks[] | select(.command | test("session-index"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]

  # Step 9.5: the backfill created the sessions index from the fixture history.
  [ -f "$REKOLH/.index/sessions.db" ]
}

@test "install does not abort when the config omits git_track" {
  # Regression: Step 8.5 greps the config for git_track under `set -o pipefail`.
  # A config that omits the key makes grep exit 1, which used to abort the whole
  # install. A hand-written/partial config must still install cleanly (git
  # tracking simply stays off, the safe default).
  SBHOME="$TESTROOT/sandhome-gt"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-gt"
  mkdir -p "$REKOLH"
  # Deliberately NO git_track line, and no projects dir (backfill self-gates).
  printf 'embedding_model: test-hashing\nsession_search_enabled: true\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-hook --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]
  # Got past Step 8.5 and built the curated index.
  [ -f "$REKOLH/.index/index.db" ]
  # git tracking stayed off (no key present, no repo initialised).
  [ ! -d "$REKOLH/.git" ]
}

@test "full install seeds generic template and yields a working search" {
  # Plan 2: migration is now opt-in (no --migrate here), and the template is
  # genericized, so the from-zero install path can run end-to-end in CI.
  unset TEST_MODE || true
  # -u MEMORY_HOME: setup() exports MEMORY_HOME with the same value, so unset it
  # here to exercise a true REKOL_HOME-only install (no fallback var present).
  run env -u TEST_MODE -u MEMORY_HOME \
    REKOL_HOME="$TESTROOT/mem" \
    HOME="$TESTROOT/home" \
    "$BATS_TEST_DIRNAME/../install.sh" \
      --no-hook --no-skill --no-shellrc \
      --tools-home "$TESTROOT/tools" --bin-dir "$TESTROOT/bin"
  [ "$status" -eq 0 ]
  # Template seeded REKOL.md + identity example into the empty root
  [ -f "$TESTROOT/mem/REKOL.md" ]
  # Search over the seeded content returns a hit (index was built by install)
  run env REKOL_HOME="$TESTROOT/mem" "$TESTROOT/tools/.venv/bin/rekol" search "identity" --top 3
  [ "$status" -eq 0 ]
}

@test "install wires UserPromptSubmit time-context and Stop record-stop hooks" {
  # Verifies Phase B: install.sh Steps 7E/7F merge REKOL's own time hooks so the
  # <env-time> block comes from rekol, not the external mac_setup component.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-time"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-time"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: true\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  run jq -e '.hooks.UserPromptSubmit[].hooks[] | select(.command == "rekol _hook time-context")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  run jq -e '.hooks.Stop[].hooks[] | select(.command == "rekol _hook record-stop")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
}

@test "double-injection guard warns and skips on a legacy mac_setup time hook" {
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-legacy"
  mkdir -p "$SBHOME/.claude"
  printf '%s' \
    '{"hooks":{"UserPromptSubmit":[{"matcher":"","hooks":[{"type":"command","command":"~/.local/share/mac_setup/hooks/inject-time-context.sh"}]}]}}' \
    > "$SBHOME/.claude/settings.json"
  REKOLH="$TESTROOT/rekolhome-legacy"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: true\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Legacy mac_setup time hook detected"* ]]

  # REKOL's time-context hook must NOT have been added while the legacy one stands.
  run jq -e '[.hooks.UserPromptSubmit[].hooks[].command] | any(. == "rekol _hook time-context")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -ne 0 ]
}

@test "re-install adds the review nudge to a legacy two-handler SessionEnd block" {
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-nudge"
  mkdir -p "$SBHOME/.claude"
  # A SessionEnd block as a PR#1-era install would have it: capture-reminder +
  # session-index, but no review nudge. Step 7D no-ops on this; Step 7G adds it.
  printf '%s' \
    '{"hooks":{"SessionEnd":[{"matcher":"","hooks":[{"type":"command","command":"echo hi"},{"type":"command","command":"rekol session-index --incremental"}]}]}}' \
    > "$SBHOME/.claude/settings.json"
  REKOLH="$TESTROOT/rekolhome-nudge"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: true\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  run jq -e '[.hooks.SessionEnd[].hooks[].command] | any(. == "rekol review --nudge")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  # session-index must not be duplicated by the merge.
  run jq -e '[.hooks.SessionEnd[].hooks[].command] | map(select(. == "rekol session-index --incremental")) | length == 1' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
}

@test "install rebuilds (not just updates) when the existing index has a legacy schema" {
  command -v jq >/dev/null 2>&1 || skip "jq required"
  command -v sqlite3 >/dev/null 2>&1 || skip "sqlite3 required"

  SBHOME="$TESTROOT/sandhome-legacy-idx"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-legacy-idx"
  mkdir -p "$REKOLH/always" "$REKOLH/.index"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf -- '---\nname: id\ndescription: d\ntype: always\n---\nbody\n' \
    > "$REKOLH/always/identity.md"
  # A legacy curated index: chunks table WITHOUT the timestamp columns. Step 9's
  # `index update` must detect this, NOT abort the install, and rebuild instead.
  sqlite3 "$REKOLH/.index/index.db" \
    "CREATE TABLE files (path TEXT PRIMARY KEY, mtime INT, content_hash TEXT, indexed_at INT); CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT, heading TEXT, line_start INT, line_end INT, text TEXT, tags_json TEXT, aliases_json TEXT, embedding BLOB);"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-hook --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # Rebuilt (migrated): the chunks table now has the timestamp columns.
  run sqlite3 "$REKOLH/.index/index.db" \
    "SELECT count(*) FROM pragma_table_info('chunks') WHERE name='created';"
  [ "$output" = "1" ]
}
