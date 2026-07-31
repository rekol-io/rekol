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

# #78: build ONE venv for the whole file and reuse it across tests. Each test used
# to run the full installer → a fresh venv → `pip install` pulling torch (731 MB),
# ~18 min/test and CI cancellations. We build the venv once here (a real install,
# so the venv-creation + pip path stays covered), then per-test setup() points
# TOOLS_HOME at it and exports REKOL_INSTALL_SKIP_DEPS=1 so install.sh reuses it
# instead of reinstalling. Per-test HOME/REKOL_HOME/XDG_* stay isolated; only the
# immutable venv is shared.
setup_file() {
    SHARED_TOOLS="${BATS_FILE_TMPDIR}/shared-tools"
    export SHARED_TOOLS
    local cdir seed
    cdir="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    seed="${BATS_FILE_TMPDIR}/seed"
    mkdir -p "${seed}/mem" "${seed}/home"
    printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
        > "${seed}/mem/rekol.config.yaml"
    # Real install (no --skip-deps): builds the venv + pip installs rekol once.
    REKOL_HOME="${seed}/mem" HOME="${seed}/home" \
        XDG_CACHE_HOME="${seed}/cache" XDG_DATA_HOME="${seed}/data" \
        "${cdir}/install.sh" --test-mode \
        --tools-home "${SHARED_TOOLS}" --bin-dir "${seed}/bin" >/dev/null 2>&1
    if [ ! -x "${SHARED_TOOLS}/.venv/bin/rekol" ]; then
        echo "setup_file: shared venv build failed (no rekol in ${SHARED_TOOLS}/.venv)" >&2
        return 1
    fi
}

setup() {
    TESTROOT="$(mktemp -d)"
    # Export MEMORY_HOME (the fallback) and unset REKOL_HOME so the resolver
    # exercises the fallback path; the missing-home test unsets both.
    export MEMORY_HOME="${TESTROOT}/mem"
    unset REKOL_HOME || true
    # #78: reuse the shared venv (built in setup_file) and skip the pip reinstall.
    # Tests needing a fresh/empty venv (e.g. the shim-missing-venv test) override
    # TOOLS_HOME locally. Per-test timeout is a safety net so a pathological test
    # fails fast instead of cancelling the whole job.
    TOOLS_HOME="${SHARED_TOOLS}"
    export REKOL_INSTALL_SKIP_DEPS=1
    export BATS_TEST_TIMEOUT=300
    BIN_DIR="${TESTROOT}/bin"
    # SECURITY/test-hygiene: the index now lives in ${XDG_CACHE_HOME:-~/.cache}/
    # rekol/<hash>. Sandbox XDG_CACHE_HOME so building the index in these tests
    # writes ONLY under the throwaway TESTROOT and never pollutes the real
    # ~/.cache. (HOME is left alone so the test-mode .zshrc check still verifies
    # the real ~/.zshrc is untouched.)
    export XDG_CACHE_HOME="${TESTROOT}/cache"
    # SECURITY/test-hygiene: the durable transcript archive (#8) defaults to
    # ${XDG_DATA_HOME:-~/.local/share}/rekol/archive. Sandbox XDG_DATA_HOME so any
    # archive resolution lands under TESTROOT and never touches the real
    # ~/.local/share/rekol/archive (mirrors the conftest fix). The archive
    # backfill itself is skipped in --test-mode, but resolve_archive_dir(None) is
    # still consulted to record ARCHIVE_DIR in the manifest.
    export XDG_DATA_HOME="${TESTROOT}/data"
    # Resolve component dir relative to this test file
    COMPONENT_DIR="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    export COMPONENT_DIR TOOLS_HOME BIN_DIR TESTROOT XDG_CACHE_HOME XDG_DATA_HOME
}

teardown() {
    rm -rf "${TESTROOT}"
}

# Reads the local-only INDEX_DIR install recorded in the manifest under the given
# REKOL_HOME. The index lives in a cache OUTSIDE $REKOL_HOME, so tests resolve it
# from the same path install wrote rather than hard-coding the hash. $1 = the
# REKOL_HOME whose manifest to read. Echoes the path (empty if none).
manifest_index_dir() {
    local home="$1" f line
    f="${home}/.install-logs/manifest.env"
    [[ -f "$f" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            INDEX_DIR=*) printf '%s' "${line#INDEX_DIR=}" ;;
        esac
    done < "$f"
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
@test "real install seeds MEMORY_HOME, builds index in the cache, creates INDEX.md" {
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

    # Index + INDEX.md built in the local cache OUTSIDE $REKOL_HOME, not in-tree.
    cache="$(manifest_index_dir "${MEMORY_HOME}")"
    [ -n "$cache" ]
    [ -f "${cache}/index.db" ]
    [ -f "${cache}/INDEX.md" ]

    # SECURITY: nothing derived (and no .index/, no .dropboxignore) under the
    # possibly-synced memory home.
    [ ! -d "${MEMORY_HOME}/.index" ]
    [ ! -f "${MEMORY_HOME}/.dropboxignore" ]
    run find "${MEMORY_HOME}" -name '*.db'
    [ "$status" -eq 0 ]
    [ -z "$output" ]
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
# Test 3b — install writes a durable manifest recording the resolved paths
# ---------------------------------------------------------------------------
# The manifest is what makes uninstall deterministic: it records the resolved
# TOOLS_HOME / BIN_DIR / REKOL_HOME / INDEX_DIR so `uninstall.sh` (no flags) can
# find a custom install AND its local index cache. It lives at a STABLE path
# under REKOL_HOME (.install-logs/manifest.env) and is overwritten — not appended
# — on rerun.
@test "install writes the manifest with the resolved paths and overwrites on rerun" {
    "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode

    MANIFEST="${MEMORY_HOME}/.install-logs/manifest.env"
    [ -f "${MANIFEST}" ]
    grep -q "^TOOLS_HOME=${TOOLS_HOME}$" "${MANIFEST}"
    grep -q "^BIN_DIR=${BIN_DIR}$" "${MANIFEST}"
    grep -q "^REKOL_HOME=${MEMORY_HOME}$" "${MANIFEST}"
    grep -q "^SHIM=${BIN_DIR}/rekol$" "${MANIFEST}"
    # The local index cache is recorded too, and points OUTSIDE $REKOL_HOME so
    # uninstall can remove it. (It lands under the sandboxed XDG_CACHE_HOME.)
    grep -q "^INDEX_DIR=${XDG_CACHE_HOME}/rekol/" "${MANIFEST}"
    ! grep -q "^INDEX_DIR=${MEMORY_HOME}" "${MANIFEST}"

    # Rerun overwrites in place: exactly one manifest, still the right TOOLS_HOME.
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode
    [ "$status" -eq 0 ]
    MANIFEST_COUNT="$(find "${MEMORY_HOME}/.install-logs" -name 'manifest.env' | wc -l | tr -d ' ')"
    [ "$MANIFEST_COUNT" -eq 1 ]
    [ "$(grep -c '^TOOLS_HOME=' "${MANIFEST}")" -eq 1 ]
    grep -q "^TOOLS_HOME=${TOOLS_HOME}$" "${MANIFEST}"
}

# ---------------------------------------------------------------------------
# Test 3c — dry-run does not write the manifest
# ---------------------------------------------------------------------------
@test "dry-run install does not write the manifest" {
    run "${COMPONENT_DIR}/install.sh" \
        --dry-run \
        --tools-home "${TOOLS_HOME}" \
        --bin-dir "${BIN_DIR}" \
        --test-mode
    [ "$status" -eq 0 ]
    [ ! -f "${MEMORY_HOME}/.install-logs/manifest.env" ]
}

# ---------------------------------------------------------------------------
# Test 4 — the rekol shim errors clearly when the venv is absent
# ---------------------------------------------------------------------------
@test "shim exits 2 with helpful message when venv is missing" {
    # Must point at a genuinely EMPTY tools-home (not the shared venv) — this test
    # asserts the shim's behavior when no venv exists.
    run env REKOL_TOOLS_HOME="${TESTROOT}/empty-tools" \
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
# Piece 1: a NON-interactive run (stdin is the bats pipe, not a TTY) with both
# vars unset must still hard-exit 2 — the prompt only fires on a real TTY.
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
# Piece 1 — interactive prompt-with-default (prompt logic, sourced in isolation)
# ---------------------------------------------------------------------------
# bats's `run` pipes stdin, so the installer's `[[ -t 0 ]]` TTY check is false
# and the prompt never fires end-to-end. We instead source ONLY the helper
# functions (REKOL_INSTALL_SOURCE_ONLY=1 makes install.sh return early) and call
# prompt_for_memory_home directly with piped input — exactly what the TTY path
# would read. The prompt text goes to stderr, so we discard stderr (2>/dev/null)
# and assert against stdout, which carries only the resolved path.

@test "prompt_for_memory_home accepts a typed path" {
    run env REKOL_INSTALL_SOURCE_ONLY=1 HOME="$TESTROOT/home" bash -c '
        source "'"${COMPONENT_DIR}"'/install.sh"
        printf "%s\n" "/tmp/my-mem" | prompt_for_memory_home "$HOME/rekol-memory" 2>/dev/null
    '
    [ "$status" -eq 0 ]
    [ "$output" = "/tmp/my-mem" ]
}

@test "prompt_for_memory_home defaults on empty input" {
    run env REKOL_INSTALL_SOURCE_ONLY=1 HOME="$TESTROOT/home" bash -c '
        source "'"${COMPONENT_DIR}"'/install.sh"
        printf "\n" | prompt_for_memory_home "$HOME/rekol-memory" 2>/dev/null
    '
    [ "$status" -eq 0 ]
    [ "$output" = "$TESTROOT/home/rekol-memory" ]
}

@test "prompt_for_memory_home expands a leading tilde" {
    run env REKOL_INSTALL_SOURCE_ONLY=1 HOME="$TESTROOT/home" bash -c '
        source "'"${COMPONENT_DIR}"'/install.sh"
        printf "%s\n" "~/elsewhere" | prompt_for_memory_home "$HOME/rekol-memory" 2>/dev/null
    '
    [ "$status" -eq 0 ]
    [ "$output" = "$TESTROOT/home/elsewhere" ]
}

@test "prompt_for_memory_home defaults on EOF without aborting under set -e" {
    # Closed stdin (EOF) must yield the default, not a non-zero exit — the `read`
    # is guarded with `|| true` precisely so `set -euo pipefail` does not abort.
    run env REKOL_INSTALL_SOURCE_ONLY=1 HOME="$TESTROOT/home" bash -c '
        source "'"${COMPONENT_DIR}"'/install.sh"
        prompt_for_memory_home "$HOME/rekol-memory" < /dev/null 2>/dev/null
    '
    [ "$status" -eq 0 ]
    [ "$output" = "$TESTROOT/home/rekol-memory" ]
}

@test "install writes PATH + REKOL_HOME to the bash rc when SHELL is bash" {
    # bash users must get a working CLI too: with SHELL=bash the exports go to a
    # bash rc (.bashrc on Linux, .bash_profile on macOS), NOT ~/.zshrc.
    unset TEST_MODE || true
    local sbhome="${TESTROOT}/home-bash" rekolh="${TESTROOT}/rekolhome-bash"
    mkdir -p "${sbhome}/.claude" "${rekolh}"
    printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
        > "${rekolh}/rekol.config.yaml"
    run env -u TEST_MODE -u MEMORY_HOME \
        REKOL_HOME="${rekolh}" HOME="${sbhome}" SHELL="/bin/bash" \
        "${COMPONENT_DIR}/install.sh" --no-hook --no-skill \
        --tools-home "${SHARED_TOOLS}" --bin-dir "${sbhome}/bin"
    [ "$status" -eq 0 ]
    # The rekol exports landed in a bash rc (whichever the OS uses)...
    rc=""
    if [ -f "${sbhome}/.bashrc" ] && grep -q '^export REKOL_HOME=' "${sbhome}/.bashrc"; then rc="${sbhome}/.bashrc"; fi
    if [ -f "${sbhome}/.bash_profile" ] && grep -q '^export REKOL_HOME=' "${sbhome}/.bash_profile"; then rc="${sbhome}/.bash_profile"; fi
    [ -n "$rc" ]
    grep -q '# rekol' "$rc"
    # ...and NOT in ~/.zshrc (a bash user's zshrc must be untouched / absent).
    [ ! -f "${sbhome}/.zshrc" ]
}

# ---------------------------------------------------------------------------
# Test 6b (#83) — the REKOL_HOME rc export is a default-if-unset guard, so an
# inherited REKOL_HOME (automation/CI/tests redirecting the store) survives a
# re-source of the rc instead of being clobbered back to the installed path.
# ---------------------------------------------------------------------------
@test "REKOL_HOME rc export guards an inherited env value (#83)" {
    unset TEST_MODE || true
    local sbhome="${TESTROOT}/home-83" rekolh="${TESTROOT}/rekolhome-83"
    mkdir -p "${sbhome}/.claude" "${rekolh}"
    printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
        > "${rekolh}/rekol.config.yaml"
    run env -u TEST_MODE -u MEMORY_HOME \
        REKOL_HOME="${rekolh}" HOME="${sbhome}" SHELL="/bin/zsh" \
        "${COMPONENT_DIR}/install.sh" --no-hook --no-skill \
        --tools-home "${SHARED_TOOLS}" --bin-dir "${sbhome}/bin"
    [ "$status" -eq 0 ]
    local rc="${sbhome}/.zshrc"
    [ -f "$rc" ]
    # The export is the guard form, NOT a hardcoded value baked from install time.
    grep -qF 'export REKOL_HOME="${REKOL_HOME:-' "$rc"
    # A fresh shell (REKOL_HOME unset) still resolves to the installed path.
    run env -u REKOL_HOME sh -c ". '$rc'; printf '%s' \"\$REKOL_HOME\""
    [ "$output" = "${rekolh}" ]
    # An inherited REKOL_HOME is preserved (the bug this fixes): sourcing the rc
    # must NOT override a redirect to a throwaway store.
    run env REKOL_HOME="${TESTROOT}/throwaway-83" sh -c ". '$rc'; printf '%s' \"\$REKOL_HOME\""
    [ "$output" = "${TESTROOT}/throwaway-83" ]
}

# ---------------------------------------------------------------------------
# Test 6c (#83 follow-up, QA 20260620-2145) — a machine that installed pre-#119
# has an OLD hardcoded `export REKOL_HOME="<path>"` line; #119 only added-when-
# absent, so it was never upgraded. A re-run must rewrite it in place to the guard.
# ---------------------------------------------------------------------------
@test "install upgrades an existing pre-#119 hardcoded REKOL_HOME rc line in place" {
    unset TEST_MODE || true
    local sbhome="${TESTROOT}/home-upg" rekolh="${TESTROOT}/rekolhome-upg"
    mkdir -p "${sbhome}/.claude" "${rekolh}"
    printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
        > "${rekolh}/rekol.config.yaml"
    # Pre-seed the OLD hardcoded (unguarded) form, as a pre-#119 install left it.
    printf '# rekol\nexport PATH="%s/bin:$PATH"\nexport REKOL_HOME="%s"\n' "${sbhome}" "${rekolh}" \
        > "${sbhome}/.zshrc"
    run env -u TEST_MODE -u MEMORY_HOME \
        REKOL_HOME="${rekolh}" HOME="${sbhome}" SHELL="/bin/zsh" \
        "${COMPONENT_DIR}/install.sh" --no-hook --no-skill \
        --tools-home "${SHARED_TOOLS}" --bin-dir "${sbhome}/bin"
    [ "$status" -eq 0 ]
    local rc="${sbhome}/.zshrc"
    # The old line was rewritten IN PLACE to the guard form...
    grep -qF 'export REKOL_HOME="${REKOL_HOME:-' "$rc"
    # ...exactly one REKOL_HOME export (upgraded, not a duplicate appended).
    [ "$(grep -c '^export REKOL_HOME=' "$rc")" -eq 1 ]
    # Fresh shell resolves to the installed path; an inherited value now survives.
    run env -u REKOL_HOME sh -c ". '$rc'; printf '%s' \"\$REKOL_HOME\""
    [ "$output" = "${rekolh}" ]
    run env REKOL_HOME="${TESTROOT}/throwaway-upg" sh -c ". '$rc'; printf '%s' \"\$REKOL_HOME\""
    [ "$output" = "${TESTROOT}/throwaway-upg" ]
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

  # Step 9.5: the backfill created the sessions index from the fixture history —
  # in the local cache outside $REKOL_HOME (never in-tree).
  cache="$(manifest_index_dir "$REKOLH")"
  [ -n "$cache" ]
  [ -f "$cache/sessions.db" ]
  [ ! -d "$REKOLH/.index" ]
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
  # Got past Step 8.5 and built the curated index — in the cache, not in-tree.
  cache="$(manifest_index_dir "$REKOLH")"
  [ -n "$cache" ]
  [ -f "$cache/index.db" ]
  [ ! -d "$REKOLH/.index" ]
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
      --tools-home "$SHARED_TOOLS" --bin-dir "$TESTROOT/bin"
  [ "$status" -eq 0 ]
  # Template seeded REKOL.md + identity example into the empty root
  [ -f "$TESTROOT/mem/REKOL.md" ]
  # Search over the seeded content returns a hit (index was built by install)
  run env REKOL_HOME="$TESTROOT/mem" "$SHARED_TOOLS/.venv/bin/rekol" search "identity" --top 3
  [ "$status" -eq 0 ]
}

@test "install with NO REKOL_HOME preset (prompt-driven) completes and builds the index" {
  # Regression (launch-blocker): the documented customer flow is `./install.sh`
  # answering the memory-home PROMPT, NOT pre-exporting REKOL_HOME. The prompt's
  # `REKOL_HOME=$(...)` is a non-exported shell var, so install must `export` the
  # resolved home or its `rekol` subprocesses (index rebuild / session-index /
  # archive / migrate) hit load_config() with no REKOL_HOME and crash on the last
  # steps. The Docker acceptance set ENV REKOL_HOME, which masked it — so here we
  # run with NO REKOL_HOME / MEMORY_HOME and answer the prompt over a real PTY
  # (so `[[ -t 0 ]]` is true), then assert install finished AND the index built.
  command -v python3 >/dev/null 2>&1 || skip "python3 required to drive the PTY prompt"
  unset TEST_MODE || true
  local memhome="$TESTROOT/prompted-mem"
  mkdir -p "$TESTROOT/home-prompt"
  run env -u TEST_MODE -u REKOL_HOME -u MEMORY_HOME HOME="$TESTROOT/home-prompt" \
    python3 - "$BATS_TEST_DIRNAME/../install.sh" "$memhome" \
      "$SHARED_TOOLS" "$TESTROOT/bin-prompt" <<'PY'
import os, pty, select, sys
install, answer, tools, bindir = sys.argv[1], sys.argv[2] + "\n", sys.argv[3], sys.argv[4]
argv = ["bash", install, "--no-hook", "--no-skill", "--no-shellrc",
        "--tools-home", tools, "--bin-dir", bindir]
pid, fd = pty.fork()
if pid == 0:
    os.execv("/bin/bash", argv)  # child: stdin is the PTY slave -> [[ -t 0 ]] true
written = False
while True:
    try:
        r, _, _ = select.select([fd], [], [], 300)
    except OSError:
        break
    if not r:
        break
    try:
        data = os.read(fd, 4096)
    except OSError:
        break
    if not data:
        break
    sys.stdout.buffer.write(data)
    if not written:           # answer the memory-home prompt exactly once
        os.write(fd, answer.encode()); written = True
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
PY
  [ "$status" -eq 0 ]
  # The post-resolve subprocesses ran (didn't die on load_config): search works.
  run env REKOL_HOME="$memhome" "$SHARED_TOOLS/.venv/bin/rekol" search "identity" --top 3
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
  # (bare) session-index, but no review nudge. Step 7D now UPGRADES the bare
  # session-index to the detached `nohup ... &` form in place (#135); Step 7G adds
  # the nudge.
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
  # session-index must not be duplicated by the merge — exactly one handler.
  run jq -e '[.hooks.SessionEnd[].hooks[].command | select(test("session-index --incremental"))] | length == 1' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  # #135: the bare handler was upgraded IN PLACE to the detached (nohup ... &) form,
  # so a large backlog can't block session end / exceed the hook timeout.
  run jq -e '[.hooks.SessionEnd[].hooks[].command | select(test("session-index --incremental"))][0] | test("^nohup .* &$")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
}

@test "install migrates a legacy in-tree index into the cache with a fresh schema" {
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
  # A legacy IN-TREE curated index: chunks table WITHOUT the timestamp columns,
  # under $REKOL_HOME/.index/ as a pre-relocation install left it. Install must
  # rebuild it into the cache (with the current schema) and delete the in-tree
  # copy — without ever aborting.
  sqlite3 "$REKOLH/.index/index.db" \
    "CREATE TABLE files (path TEXT PRIMARY KEY, mtime INT, content_hash TEXT, indexed_at INT); CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT, heading TEXT, line_start INT, line_end INT, text TEXT, tags_json TEXT, aliases_json TEXT, embedding BLOB);"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-hook --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # The legacy in-tree index is gone (relocated + cleaned up).
  [ ! -d "$REKOLH/.index" ]

  # Rebuilt into the cache with the current schema (chunks has timestamp columns).
  cache="$(manifest_index_dir "$REKOLH")"
  [ -n "$cache" ]
  [ -f "$cache/index.db" ]
  run sqlite3 "$cache/index.db" \
    "SELECT count(*) FROM pragma_table_info('chunks') WHERE name='created';"
  [ "$output" = "1" ]
}

@test "fresh install does NOT wire the SessionStart ingest-nudge handler" {
  # Onboarding is pull-only: the SessionStart auto-nudge was removed. A fresh
  # install must never wire `rekol _hook session-start-nudge`.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-no-ssnudge"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-no-ssnudge"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # No SessionStart handler may reference the nudge.
  run jq -e '[.hooks.SessionStart[]?.hooks[]?.command] | any(. | contains("session-start-nudge"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -ne 0 ]
}

@test "re-install strips an already-wired SessionStart nudge, keeping the index-cat handler" {
  # An earlier rekol version wired `rekol _hook session-start-nudge` into the
  # SessionStart block on existing machines. (Re-)installing must actively
  # remove that handler while leaving the index-cat handler intact.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-strip-ssnudge"
  mkdir -p "$SBHOME/.claude"
  # SessionStart block as a nudge-era install left it: index-cat handler plus a
  # separate nudge handler entry.
  printf '%s' \
    '{"hooks":{"SessionStart":[{"matcher":"","hooks":[{"type":"command","command":"HOME_DIR=\"${REKOL_HOME:-$MEMORY_HOME}\"; cat \"$HOME_DIR/REKOL.md\""}]},{"matcher":"","hooks":[{"type":"command","command":"rekol _hook session-start-nudge"}]}]}}' \
    > "$SBHOME/.claude/settings.json"
  REKOLH="$TESTROOT/rekolhome-strip-ssnudge"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # The nudge handler is gone...
  run jq -e '[.hooks.SessionStart[]?.hooks[]?.command] | any(. | contains("session-start-nudge"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -ne 0 ]
  # ...and the index-cat handler survives.
  run jq -e '[.hooks.SessionStart[]?.hooks[]?.command] | any(. | contains("REKOL.md"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
}

@test "re-install leaves a nudge-free SessionStart block untouched (idempotent strip)" {
  # The strip must be a no-op when no nudge handler is present, and stay clean
  # across repeated installs over a settings.json that once had the nudge.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-idem-strip"
  mkdir -p "$SBHOME/.claude"
  printf '%s' \
    '{"hooks":{"SessionStart":[{"matcher":"","hooks":[{"type":"command","command":"rekol _hook session-start-nudge"}]}]}}' \
    > "$SBHOME/.claude/settings.json"
  REKOLH="$TESTROOT/rekolhome-idem-strip"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  for _ in 1 2; do
    run env -u MEMORY_HOME -u TEST_MODE \
      REKOL_HOME="$REKOLH" HOME="$SBHOME" \
      "$COMPONENT_DIR/install.sh" \
        --no-skill --no-shellrc \
        --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
    [ "$status" -eq 0 ]
  done

  run jq -e '[.hooks.SessionStart[]?.hooks[]?.command] | any(. | contains("session-start-nudge"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -ne 0 ]
}

@test "fresh install wires the SessionStart coverage banner handler (#123)" {
  # `rekol _hook session-coverage` warns when memory files are invisible to search.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-cov-fresh"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-cov-fresh"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  run jq -e '[.hooks.SessionStart[]?.hooks[]?.command] | any(. | contains("_hook session-coverage"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
}

@test "re-install adds the coverage banner to an existing block exactly once (#123)" {
  # An existing install has the memory-loader handler but no coverage handler yet.
  # (Re-)install must ADD the coverage handler, keep the memory-loader, and stay
  # idempotent — a second install must not wire a duplicate.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-cov-add"
  mkdir -p "$SBHOME/.claude"
  # Pre-existing SessionStart block: memory-loader only (the delicate command).
  printf '%s' \
    '{"hooks":{"SessionStart":[{"matcher":"","hooks":[{"type":"command","command":"HOME_DIR=\"${REKOL_HOME:-$MEMORY_HOME}\"; cat \"$HOME_DIR/REKOL.md\"; rekol _hook session-confidence 2>/dev/null || true"}]}]}}' \
    > "$SBHOME/.claude/settings.json"
  REKOLH="$TESTROOT/rekolhome-cov-add"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  for _ in 1 2; do
    run env -u MEMORY_HOME -u TEST_MODE \
      REKOL_HOME="$REKOLH" HOME="$SBHOME" \
      "$COMPONENT_DIR/install.sh" \
        --no-skill --no-shellrc \
        --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
    [ "$status" -eq 0 ]
  done

  # The memory-loader survives...
  run jq -e '[.hooks.SessionStart[]?.hooks[]?.command] | any(. | contains("REKOL.md"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  # ...and exactly ONE coverage handler exists after two installs (idempotent).
  run jq '[.hooks.SessionStart[]?.hooks[]?.command | select(contains("_hook session-coverage"))] | length' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  [ "$output" -eq 1 ]
}

@test "fresh install wires the SessionStart task-injection handler (#113)" {
  # `rekol _hook session-tasks` surfaces open cross-session tasks at start.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-tasks-fresh"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-tasks-fresh"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  run jq -e '[.hooks.SessionStart[]?.hooks[]?.command] | any(. | contains("_hook session-tasks"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
}

@test "re-install adds the task-injection handler exactly once (#113)" {
  # Existing block (memory-loader + coverage), no task handler yet: (re-)install
  # must add it, keep everything else, and not duplicate on a second run.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-tasks-add"
  mkdir -p "$SBHOME/.claude"
  printf '%s' \
    '{"hooks":{"SessionStart":[{"matcher":"","hooks":[{"type":"command","command":"HOME_DIR=\"${REKOL_HOME:-$MEMORY_HOME}\"; cat \"$HOME_DIR/REKOL.md\"; rekol _hook session-confidence 2>/dev/null || true"}]},{"matcher":"","hooks":[{"type":"command","command":"rekol _hook session-coverage 2>/dev/null || true"}]}]}}' \
    > "$SBHOME/.claude/settings.json"
  REKOLH="$TESTROOT/rekolhome-tasks-add"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  for _ in 1 2; do
    run env -u MEMORY_HOME -u TEST_MODE \
      REKOL_HOME="$REKOLH" HOME="$SBHOME" \
      "$COMPONENT_DIR/install.sh" \
        --no-skill --no-shellrc \
        --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
    [ "$status" -eq 0 ]
  done

  # Memory-loader + coverage survive...
  run jq -e '[.hooks.SessionStart[]?.hooks[]?.command] | (any(. | contains("REKOL.md")) and any(. | contains("session-coverage")))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  # ...and exactly ONE task handler exists after two installs.
  run jq '[.hooks.SessionStart[]?.hooks[]?.command | select(contains("_hook session-tasks"))] | length' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  [ "$output" -eq 1 ]
}

@test "re-install adds the capture-nudge handler even when a time hook exists (#122)" {
  # Step 7E no-ops when a time hook is present (incl. legacy mac_setup), which
  # would strand capture-nudge — Step 7E2 must add it independently, exactly once.
  command -v jq >/dev/null 2>&1 || skip "jq required for hook merge"

  SBHOME="$TESTROOT/sandhome-nudge-add"
  mkdir -p "$SBHOME/.claude"
  printf '%s' \
    '{"hooks":{"UserPromptSubmit":[{"matcher":"","hooks":[{"type":"command","command":"rekol _hook time-context"}]}]}}' \
    > "$SBHOME/.claude/settings.json"
  REKOLH="$TESTROOT/rekolhome-nudge-add"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  for _ in 1 2; do
    run env -u MEMORY_HOME -u TEST_MODE \
      REKOL_HOME="$REKOLH" HOME="$SBHOME" \
      "$COMPONENT_DIR/install.sh" \
        --no-skill --no-shellrc \
        --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
    [ "$status" -eq 0 ]
  done

  # time-context survives, capture-nudge added exactly once.
  run jq -e '[.hooks.UserPromptSubmit[]?.hooks[]?.command] | any(. == "rekol _hook time-context")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  run jq '[.hooks.UserPromptSubmit[]?.hooks[]?.command | select(contains("_hook capture-nudge"))] | length' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  [ "$output" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Phase 8 — durable transcript archive flags
# ---------------------------------------------------------------------------
# REKOL_ARCHIVE_DIR is sandboxed under TESTROOT (and XDG_DATA_HOME is too, via
# setup) so no real machine archive is ever written by these tests.

@test "--no-archive seeds archive_enabled:false in the config" {
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}" \
        --test-mode --no-archive
    [ "$status" -eq 0 ]
    grep -q '^archive_enabled: false' "${MEMORY_HOME}/rekol.config.yaml"
}

@test "--archive-dir is recorded so the archive lands where asked" {
    export REKOL_ARCHIVE_DIR=""
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}" \
        --test-mode --archive-dir "${TESTROOT}/custom-archive"
    [ "$status" -eq 0 ]
    # The chosen archive_dir is persisted to config.
    grep -q "archive_dir: ${TESTROOT}/custom-archive" "${MEMORY_HOME}/rekol.config.yaml"
    # And it is recorded in the manifest so uninstall can find it.
    grep -q "^ARCHIVE_DIR=${TESTROOT}/custom-archive$" "${MEMORY_HOME}/.install-logs/manifest.env"
}

@test "--help prints usage including the archive flags" {
    run "${COMPONENT_DIR}/install.sh" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--no-archive"* ]]
    [[ "$output" == *"--archive-dir"* ]]
}

@test "install output discloses the local archive" {
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}" --test-mode
    [ "$status" -eq 0 ]
    [[ "$output" == *"local copy of your sessions"* ]]
}
