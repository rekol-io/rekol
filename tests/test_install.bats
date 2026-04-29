#!/usr/bin/env bats
# Installer smoke-tests for memory-tools/install.sh.
#
# Run with:   bats memory-tools/tests/test_install.bats
# Requires:   bats-core (brew install bats-core) and internet access for the
#             first real-install test (downloads sentence-transformers model).
#
# On a machine without bats, this file documents manual test steps.

setup() {
    TESTROOT="$(mktemp -d)"
    export MEMORY_HOME="${TESTROOT}/mem"
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
    [ ! -f "${MEMORY_HOME}/MEMORY.md" ]
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
    [ -f "${MEMORY_HOME}/MEMORY.md" ]
    [ -f "${MEMORY_HOME}/always/identity.md" ]
    [ -d "${MEMORY_HOME}/when" ]
    [ -d "${MEMORY_HOME}/topics" ]

    # memory.config.yaml was created from .example
    [ -f "${MEMORY_HOME}/memory.config.yaml" ]
    [ ! -f "${MEMORY_HOME}/memory.config.yaml.example" ]

    # Index built
    [ -f "${MEMORY_HOME}/.index/index.db" ]

    # INDEX.md regenerated
    [ -f "${MEMORY_HOME}/INDEX.md" ]

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
# Test 4 — shims error clearly when venv is absent
# ---------------------------------------------------------------------------
@test "shim exits 2 with helpful message when venv is missing" {
    run env MEMORY_TOOLS_HOME="${TOOLS_HOME}" \
        "${COMPONENT_DIR}/bin/memory-search" identity --top 1

    [ "$status" -eq 2 ]
    [[ "$output" == *"run installer"* ]]
}

# ---------------------------------------------------------------------------
# Test 5 — shims work after install
# ---------------------------------------------------------------------------
@test "memory-search returns at least one result after install" {
    "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    run env MEMORY_TOOLS_HOME="${TOOLS_HOME}" \
        "${BIN_DIR}/memory-search" identity --top 2

    [ "$status" -eq 0 ]
    [[ "$output" == *"identity"* ]]
}

# ---------------------------------------------------------------------------
# Test 6 — missing MEMORY_HOME fails with helpful error
# ---------------------------------------------------------------------------
@test "missing MEMORY_HOME exits 2 with error message" {
    run env -u MEMORY_HOME \
        "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    [ "$status" -eq 2 ]
    [[ "$output" == *"MEMORY_HOME"* ]]
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
