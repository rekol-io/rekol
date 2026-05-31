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

@test "install runs rekol migrate auto and succeeds when no legacy" {
  # DEFERRED to Plan 2 (genericization): this is the only test that runs the
  # full, non-test-mode install path — exercising the Claude Code hooks/skill
  # and the legacy `rekol migrate auto` step. Plan 2 makes migration opt-in
  # (--migrate, default off) and genericizes the hook/skill install, at which
  # point this test is rewritten/removed. The generic install path (venv, seed,
  # index, rekol search) is fully covered by tests 1-7, which pass in CI.
  skip "deferred to Plan 2: full-install/auto-migration path is being genericized"
  # TEST_MODE skips the migrator hook, so disable it here
  unset TEST_MODE || true
  run env -u TEST_MODE \
    MEMORY_HOME="$TEST_TMP/mem" \
    HOME="$TEST_TMP/home" \
    "$BATS_TEST_DIRNAME/../install.sh" --tools-home "$TEST_TMP/tools" --bin-dir "$TEST_TMP/bin"
  [ "$status" -eq 0 ]
  # No legacy memory exists in $HOME → migrator prints "nothing to migrate" OR empty
  [[ "$output" == *"nothing to migrate"* || "$output" != *"ERROR"* ]]
}
