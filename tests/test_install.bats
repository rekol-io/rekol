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
    # HERMETIC, and it must be here as well as in setup(): setup_file runs FIRST,
    # so the unset in setup() cannot protect it. This function performs a REAL
    # install — including `rekol index rebuild` with `embedding_model:
    # test-hashing` — and REKOL_INDEX_DIR is resolution order #1, used verbatim.
    # Inherited from a developer shell it points at the LIVE cache, so this
    # rebuilt the user's real curated index with test vectors. That is what
    # happened on 2026-08-18: search over curated memory returned nothing for two
    # days. The #183 fix added these unsets to setup() only and therefore missed
    # the one function that does the destructive work.
    unset REKOL_INDEX_DIR REKOL_ARCHIVE_DIR REKOL_TOOLS_HOME || true
    unset MEMORY_TOOLS_HOME MEMORY_HOME CLAUDE_CONFIG_DIR CLAUDE_SETTINGS_PATH || true

    SHARED_TOOLS="${BATS_FILE_TMPDIR}/shared-tools"
    export SHARED_TOOLS
    local cdir seed
    cdir="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    seed="${BATS_FILE_TMPDIR}/seed"
    mkdir -p "${seed}/mem" "${seed}/home"
    printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
        > "${seed}/mem/rekol.config.yaml"
    # Real install (no --skip-deps): builds the venv + pip installs rekol once.
    #
    # The output goes to a LOG rather than /dev/null. This single command is the
    # point of failure for the entire file — when it fails bats reports only
    # "not ok 1 setup_file failed" plus the offending line, and with the output
    # discarded the REASON is gone for good. A macOS-only CI failure stayed
    # undiagnosable for exactly that reason: the log could never have contained
    # the cause, so re-reading it was never going to help.
    #
    # Captured rather than streamed so a passing run stays quiet (this install
    # prints a full pip transcript), and printed in full on either failure path.
    local install_log="${BATS_FILE_TMPDIR}/install.log"
    local install_rc=0
    # `|| install_rc=$?` keeps this safe under bats' errexit AND records the real
    # status. Testing `$?` inside an `if ! cmd` branch would report 0, since `!`
    # inverted it — a wrong exit code in the very message meant to explain one.
    REKOL_HOME="${seed}/mem" HOME="${seed}/home" \
        XDG_CACHE_HOME="${seed}/cache" XDG_DATA_HOME="${seed}/data" \
        "${cdir}/install.sh" --test-mode \
        --tools-home "${SHARED_TOOLS}" --bin-dir "${seed}/bin" \
        > "${install_log}" 2>&1 || install_rc=$?
    if [ "${install_rc}" -ne 0 ]; then
        echo "setup_file: install.sh exited ${install_rc}; its output follows:" >&2
        cat "${install_log}" >&2
        return 1
    fi
    if [ ! -x "${SHARED_TOOLS}/.venv/bin/rekol" ]; then
        echo "setup_file: shared venv build failed (no rekol in ${SHARED_TOOLS}/.venv)" >&2
        echo "setup_file: install.sh exited 0, so its output is the only evidence:" >&2
        cat "${install_log}" >&2
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
        # HERMETIC ENV (incident 2026-08-17): these tests run `uninstall.sh
    # --purge-index`, which rm -rf's the resolved index dir. The suite sandboxes
    # XDG_CACHE_HOME/XDG_DATA_HOME — but REKOL_INDEX_DIR takes PRECEDENCE over
    # them, and since #164 that variable is exported into every Claude Code
    # session's environment, which bats inherits. A test therefore resolved the
    # DEVELOPER'S REAL index and deleted it (~36k indexed messages).
    #
    # tests/conftest.py already clears these for pytest; bats never got the same
    # treatment, and hardening one harness is what made the gap in the other
    # dangerous. Clear every override so only the sandboxed XDG paths decide.
    unset REKOL_INDEX_DIR REKOL_ARCHIVE_DIR REKOL_TOOLS_HOME || true
    unset MEMORY_TOOLS_HOME CLAUDE_CONFIG_DIR CLAUDE_SETTINGS_PATH || true
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

# Refuses to let a destructive test run against a path outside the sandbox.
# The unset above prevents the KNOWN leak; this catches the next one, whatever
# shape it takes. Call before anything that deletes.
assert_sandboxed() {
    local resolved="$1" label="$2"
    [[ -n "$resolved" ]] || return 0
    case "$resolved" in
        "$TESTROOT"/*|/private"$TESTROOT"/*|/tmp/*|/private/tmp/*) : ;;
        *) printf 'REFUSING: %s resolved OUTSIDE the sandbox: %s\n' "$label" "$resolved" >&2
           printf '  TESTROOT=%s\n' "$TESTROOT" >&2
           return 1 ;;
    esac
}

teardown() {
    rm -rf "${TESTROOT}"
}

# ---------------- plugin/install backend coexistence (#167) -----------------

@test "coexistence detector recognizes owned hook forms without false positives" {
  detector="$COMPONENT_DIR/plugin/hooks/coexistence.sh"
  fixtures="$TESTROOT/coexistence"
  mkdir -p "$fixtures"
  printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"command":"\"$(command -v rekol || echo '\''/custom/bin/rekol'\'')\" _hook session-tasks"}]}]}}' > "$fixtures/current.json"
  printf '%s\n' '{"hooks":{"Stop":[{"hooks":[{"command":"rekol _hook record-stop"}]}]}}' > "$fixtures/bare.json"
  printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"command":": rekol:backend=install.sh:v1; anything"}]}]}}' > "$fixtures/marker.json"
  printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"command":"other-tool _hook session-tasks"}]}]}}' > "$fixtures/unrelated.json"
  printf '%s\n' '{"hooks":' > "$fixtures/malformed.json"
  for owned in current bare marker; do
    run bash -c 'source "$1"; settings_has_rekol_install_hooks "$2"' _ "$detector" "$fixtures/$owned.json"
    [ "$status" -eq 0 ] || { echo "$owned fixture was not detected"; false; }
  done
  run bash -c 'source "$1"; settings_has_rekol_install_hooks "$2"' _ "$detector" "$fixtures/unrelated.json"
  [ "$status" -eq 1 ]
  run bash -c 'source "$1"; settings_has_rekol_install_hooks "$2"' _ "$detector" "$fixtures/malformed.json"
  [ "$status" -eq 2 ]
}

@test "plugin activation detector ignores disabled cached installs" {
  detector="$COMPONENT_DIR/plugin/hooks/coexistence.sh"
  printf '%s\n' '{"enabledPlugins":{"rekol@local":true}}' > "$TESTROOT/enabled.json"
  printf '%s\n' '{"enabledPlugins":{"rekol@local":false},"plugins":{"rekol@local":[{}]}}' > "$TESTROOT/disabled.json"
  run bash -c 'source "$1"; settings_has_enabled_rekol_plugin "$2"' _ "$detector" "$TESTROOT/enabled.json"
  [ "$status" -eq 0 ]
  run bash -c 'source "$1"; settings_has_enabled_rekol_plugin "$2"' _ "$detector" "$TESTROOT/disabled.json"
  [ "$status" -eq 1 ]
}

@test "plugin bootstrap stands down for installer hooks and runs when clean" {
  state="$TESTROOT/plugin-state"
  mkdir -p "$state/venv/bin"
  printf '#!/usr/bin/env bash\nprintf "called\\n" >> "%s"\n' "$TESTROOT/plugin-calls" > "$state/venv/bin/rekol"
  chmod +x "$state/venv/bin/rekol"
  printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"command":"\"/custom/bin/rekol\" _hook session-confidence"}]}]}}' > "$TESTROOT/legacy.json"
  printf '%s\n' '{}' > "$TESTROOT/clean.json"
  run env CLAUDE_PLUGIN_DATA="$state" CLAUDE_SETTINGS_PATH="$TESTROOT/legacy.json" "$COMPONENT_DIR/plugin/hooks/bootstrap" inject
  [ "$status" -eq 0 ]
  [[ "$output" == *"standing down"* ]]
  [ ! -f "$TESTROOT/plugin-calls" ]
  run env CLAUDE_PLUGIN_DATA="$state" CLAUDE_SETTINGS_PATH="$TESTROOT/clean.json" "$COMPONENT_DIR/plugin/hooks/bootstrap" inject
  [ "$status" -eq 0 ]
  [ "$(wc -l < "$TESTROOT/plugin-calls" | tr -d ' ')" -eq 3 ]
}

# #201: the stand-down must stay visible. The original test ran bootstrap ONCE,
# so it could not distinguish "announces every session" from "announces once and
# then goes silent forever" — the latter being the actual behaviour, and the
# reason an inert plugin was indistinguishable from a working one.
@test "plugin says it is standing down on EVERY session, not just the first" {
  state="$TESTROOT/plugin-state-201"
  mkdir -p "$state/venv/bin"
  printf '#!/usr/bin/env bash\nprintf "called\\n" >> "%s"\n' "$TESTROOT/calls-201" > "$state/venv/bin/rekol"
  chmod +x "$state/venv/bin/rekol"
  printf '%s\n' '{"hooks":{"SessionStart":[{"hooks":[{"command":"\"/custom/bin/rekol\" _hook session-confidence"}]}]}}' \
    > "$TESTROOT/legacy-201.json"

  # First run: the full explanation.
  run env CLAUDE_PLUGIN_DATA="$state" CLAUDE_SETTINGS_PATH="$TESTROOT/legacy-201.json" \
    "$COMPONENT_DIR/plugin/hooks/bootstrap" inject
  [ "$status" -eq 0 ]
  [[ "$output" == *"standing down"* ]]

  # SECOND run: must still say something. This is the assertion the old test
  # lacked, and the one that fails against the once-only gate.
  run env CLAUDE_PLUGIN_DATA="$state" CLAUDE_SETTINGS_PATH="$TESTROOT/legacy-201.json" \
    "$COMPONENT_DIR/plugin/hooks/bootstrap" inject
  [ "$status" -eq 0 ]
  [ -n "$output" ] || { echo "second run was SILENT — an inert plugin with no signal"; false; }
  [[ "$output" == *"plugin inactive"* ]]
  [[ "$output" == *"install.sh backend owns the hooks"* ]]

  # And it still never runs the binary while stood down.
  [ ! -f "$TESTROOT/calls-201" ]

  # Third run, to prove it is recurring rather than twice.
  run env CLAUDE_PLUGIN_DATA="$state" CLAUDE_SETTINGS_PATH="$TESTROOT/legacy-201.json" \
    "$COMPONENT_DIR/plugin/hooks/bootstrap" inject
  [[ "$output" == *"plugin inactive"* ]]
}

@test "install skips hook wiring when the rekol plugin is enabled" {
  sandhome="$TESTROOT/plugin-enabled-home"
  rekolhome="$TESTROOT/plugin-enabled-memory"
  mkdir -p "$sandhome/.claude" "$rekolhome"
  printf '%s\n' '{"enabledPlugins":{"rekol@local":true},"env":{"KEEP_ME":"yes"}}' > "$sandhome/.claude/settings.json"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' > "$rekolhome/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE REKOL_HOME="$rekolhome" HOME="$sandhome" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]
  [[ "$output" == *"hook wiring skipped"* ]]
  run jq -e '.enabledPlugins["rekol@local"] == true and .env.KEEP_ME == "yes" and ((.hooks // {}) == {})' \
    "$sandhome/.claude/settings.json"
  [ "$status" -eq 0 ]
  # ...and the env block IS still written. Standing down is supposed to skip hook
  # WIRING only (#199). The plugin cannot edit settings.json at all — plugin/hooks/
  # bootstrap says so outright — so nothing else supplies REKOL_HOME, and without
  # it Step 7.5's own comment says "the entire memory system is dark".
  #
  # The assertion above cannot see that: it checks that hooks are empty and that an
  # unrelated key survived, which holds identically whether the env block was
  # written or skipped. That is why this shipped.
  run jq -e --arg want "$rekolhome" '.env.REKOL_HOME == $want' \
    "$sandhome/.claude/settings.json"
  [ "$status" -eq 0 ]
}

@test "install still wires the env block, and prints the snippet, when jq is absent" {
  # REGRESSION (release blocker, found by the 0.6.0 fan-out review): with an
  # existing settings.json and no jq, the coexistence detector returns 2 ("cannot
  # inspect"), which used to clear DO_HOOK — skipping every hook step AND the env
  # block, while printing a reassuring message and exiting 0. The documented
  # jq-less fallback (README: "without it the installer prints the snippet to merge
  # by hand") became unreachable dead code.
  #
  # Without jq install.sh cannot MUTATE settings.json either, so it cannot create
  # the duplicate backend the gate exists to prevent: there was nothing to guard.
  sandhome="$TESTROOT/nojq-home"
  rekolhome="$TESTROOT/nojq-memory"
  mkdir -p "$sandhome/.claude" "$rekolhome" "$TESTROOT/nojq-bin"
  printf '%s\n' '{"env":{"KEEP_ME":"yes"},"hooks":{}}' > "$sandhome/.claude/settings.json"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' > "$rekolhome/rekol.config.yaml"

  # Hide ONLY jq: mirror its directory, symlinking every entry except jq itself,
  # then swap that mirror in for the original directory in PATH. An allow-list of
  # hand-picked tools was the first attempt and it was worse than useless —
  # install.sh needs binaries the list did not name, so it died with exit 127
  # while the assertions below still passed. The test reported success for a run
  # that never reached the code under test.
  command -v jq >/dev/null 2>&1 || skip "jq not installed; nothing to hide"
  # EVERY PATH entry holding a jq must be mirrored, not just the first one found:
  # this machine has both /opt/homebrew/bin/jq and /usr/bin/jq, and hiding only
  # the first left jq reachable, which the assertion below caught.
  # Each directory is mirrored in place so PATH order — and therefore which
  # python3/git/etc wins — is exactly unchanged.
  nojq_path=""
  mirror_n=0
  IFS=':' read -ra _path_dirs <<< "$PATH"
  for pdir in "${_path_dirs[@]}"; do
    if [ -z "$pdir" ]; then continue; fi
    if [ -x "$pdir/jq" ]; then
      mirror_n=$((mirror_n + 1))
      pmirror="$TESTROOT/nojq-bin/$mirror_n"
      mkdir -p "$pmirror"
      for entry in "$pdir"/*; do
        if [ ! -e "$entry" ]; then continue; fi
        if [ "$(basename "$entry")" = "jq" ]; then continue; fi
        ln -sf "$entry" "$pmirror/$(basename "$entry")" 2>/dev/null || true
      done
      nojq_path="${nojq_path:+$nojq_path:}$pmirror"
    else
      nojq_path="${nojq_path:+$nojq_path:}$pdir"
    fi
  done

  # Assert the hiding actually worked BEFORE relying on it: if jq were still
  # reachable this test would exercise the ordinary path and pass for nothing.
  # `command` is a shell builtin, so this needs a shell, not `env`.
  run bash -c "PATH='$nojq_path' command -v jq"
  [ "$status" -ne 0 ]

  run env -u MEMORY_HOME -u TEST_MODE PATH="$nojq_path" \
    REKOL_HOME="$rekolhome" HOME="$sandhome" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc --skip-deps \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  # Exit status is asserted FIRST. Without it an early crash (exit 127 on a
  # too-thin PATH — the first version of this test did exactly that) satisfies
  # every string assertion below while never reaching the code under test.
  [ "$status" -eq 0 ]
  # It must NOT stand down over a missing jq...
  [[ "$output" != *"hook wiring skipped"* ]]
  # ...must say why it could not detect a backend, rather than going quiet...
  [[ "$output" == *"jq not found"* ]]
  # ...and must reach the DOCUMENTED fallback, which standing down had turned into
  # unreachable dead code (README promises it).
  [[ "$output" == *"printing hook snippet"* ]]
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
    printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
    printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
    printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
    # Used a macOS-only hash with `|| echo ''`, so on Linux BOTH sides became ""
    # and this compared empty to empty -- it PASSED while asserting nothing, on
    # every ubuntu run. shasum works on both; assert non-empty before trusting it.
    [ -f "$HOME/.zshrc" ] || skip "no ~/.zshrc on this machine"
    ZSHRC_BEFORE="$(shasum "$HOME/.zshrc" | awk '{print $1}')"
    [ -n "$ZSHRC_BEFORE" ]
    run "${COMPONENT_DIR}/install.sh" --test-mode --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"
    [ "$status" -eq 0 ]
    ZSHRC_AFTER="$(shasum "$HOME/.zshrc" | awk '{print $1}')"
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: true\ngit_track: false\nclaude_projects_dir: %s/.claude/projects\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: true\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: true\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # Present, but NOT as a bare `rekol` — that exits 127 in a non-interactive
  # hook shell (#159), so assert the subcommand and the absence of the bare form.
  run jq -e '.hooks.UserPromptSubmit[].hooks[] | select(.command | test("_hook time-context"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  run jq -e '[.hooks.UserPromptSubmit[].hooks[].command] | any(. == "rekol _hook time-context")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -ne 0 ]
  run jq -e '.hooks.Stop[].hooks[] | select(.command | test("_hook record-stop"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  run jq -e '[.hooks.Stop[].hooks[].command] | any(. == "rekol _hook record-stop")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -ne 0 ]
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: true\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: true\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" \
      --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  run jq -e '[.hooks.SessionEnd[].hooks[].command] | any(test("review --nudge"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  # ...and never as a bare `rekol` (#159).
  run jq -e '[.hooks.SessionEnd[].hooks[].command] | any(. == "rekol review --nudge")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -ne 0 ]
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
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
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  for _ in 1 2; do
    run env -u MEMORY_HOME -u TEST_MODE \
      REKOL_HOME="$REKOLH" HOME="$SBHOME" \
      "$COMPONENT_DIR/install.sh" \
        --no-skill --no-shellrc \
        --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
    [ "$status" -eq 0 ]
  done

  # time-context survives -- but MIGRATED to the PATH-independent form (#159), so
  # match the stable subcommand rather than the old bare text, and assert the bare
  # form is gone (a bare `rekol` in a hook exits 127).
  run jq -e '[.hooks.UserPromptSubmit[]?.hooks[]?.command] | any(contains("_hook time-context"))' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  run jq -e '[.hooks.UserPromptSubmit[]?.hooks[]?.command] | any(. == "rekol _hook time-context")' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -ne 0 ]
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

# --------------- #27: does the update REPAIR the drift it detects? -------------
# QA's most important acceptance criterion: the case for #27 is a machine that
# was wired at an older version and silently missing handlers shipped since. The
# test is therefore NOT "detects a new version" but "a machine wired the old way
# ends up correctly wired". A version bump that notifies but does not re-wire
# would pass every other test and still not fix the problem that motivated this.
@test "install repairs an old-style install: all shipped handlers wired + version stamped" {
  SBHOME="$TESTROOT/sandhome-drift"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-drift"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  # Simulate a v0.3.1-era install: a BARE `rekol` memory loader with no
  # session-confidence tail, and none of the handlers shipped since.
  cat > "$SBHOME/.claude/settings.json" <<'JSON'
{
  "hooks": {
    "SessionStart": [
      {"matcher": "", "hooks": [{"type": "command", "command": "HOME_DIR=\"${REKOL_HOME:-$MEMORY_HOME}\"; cat \"$HOME_DIR/REKOL.md\""}]}
    ],
    "UserPromptSubmit": [
      {"matcher": "", "hooks": [{"type": "command", "command": "rekol _hook time-context"}]}
    ]
  },
  "env": {"KEEP_ME": "1"}
}
JSON

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # 1. EVERY handler the shipped snippets declare must now be registered. The
  #    expected list is derived from the snippets, never hardcoded here — a
  #    hardcoded list would silently stop covering the next handler added.
  expected="$(grep -ohE '_hook [a-z-]+' "$COMPONENT_DIR"/hooks/*-snippet.json \
              | sed 's/.*_hook //' | sort -u)"
  [ -n "$expected" ]
  wired="$(grep -oE '_hook [a-z-]+' "$SBHOME/.claude/settings.json" \
           | sed 's/.*_hook //' | sort -u)"
  missing="$(comm -23 <(printf '%s\n' "$expected") <(printf '%s\n' "$wired"))"
  [ -z "$missing" ] || { echo "NOT wired after install: $missing"; false; }

  # 2. No bare `rekol` invocation survives the repair (#159).
  bare="$(jq -r '[.hooks[]?[]?.hooks[]?.command] | map(select(startswith("rekol "))) | length' \
          "$SBHOME/.claude/settings.json")"
  [ "$bare" -eq 0 ] || { echo "$bare bare invocation(s) left"; false; }

  # 3. Exactly ONE memory loader — the repair must upgrade in place, never append
  #    a second (the #135 Step-7D failure).
  loaders="$(jq -r '[.hooks.SessionStart[]?.hooks[]?.command | select(test("REKOL.md"))] | length' \
             "$SBHOME/.claude/settings.json")"
  [ "$loaders" -eq 1 ] || { echo "expected 1 memory loader, found $loaders"; false; }

  # 4. The manifest records WHAT was installed, so drift is detectable at all.
  mf="$REKOLH/.install-logs/manifest.env"
  [ -f "$mf" ]
  ver="$(sed -n 's/^VERSION=//p' "$mf")"
  [ -n "$ver" ] || { echo "manifest has no VERSION"; false; }
  pkg="$(sed -n 's/^version = "\(.*\)"/\1/p' "$COMPONENT_DIR/pyproject.toml" | head -1)"
  [ "$ver" = "$pkg" ] || { echo "manifest VERSION=$ver but package is $pkg"; false; }

  # 5. The user's own env keys survive.
  run jq -e '.env.KEEP_ME == "1"' "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
}

# ============================ #170 ============================================
# Nothing in this repo ever executed a hook it registered. install.sh's eight jq
# gates, cli_resume.py's marker match and update.py's regex are all STRING checks,
# and this suite's 85 `run` invocations only ever assert presence. That is why
# #159 shipped with five hooks silently dead, why the plugin's coexistence guard
# broke unnoticed, and why a "✓ all handlers registered" check reached review
# while every command pointed at a nonexistent path.
#
# This is the missing test. It runs every command the installer actually wrote.
#
# The environment is the whole point: PATH deliberately EXCLUDES ${BIN_DIR}.
# Claude Code runs hooks in a non-interactive shell that never sourced .zshrc —
# which is where install.sh adds BIN_DIR — so a bare `rekol` MUST fail here and
# only the PATH-independent form can pass. Handing these commands an interactive
# PATH would make the test pass for a reason the hooks cannot rely on, which is
# precisely the mistake being corrected.
@test "every hook command the installer wrote actually executes (#170)" {
  SBHOME="$TESTROOT/sandhome-exec"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-exec"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # Every rekol-related hook command the install produced, one per line.
  jq -r '[.hooks[]?[]?.hooks[]?.command] | .[] | select(test("rekol|REKOL"))' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/cmds.txt"

  # Guard against a command containing a newline, which would silently split and
  # leave us executing fragments: the line count must equal the array length.
  expected_n="$(jq -r '[.hooks[]?[]?.hooks[]?.command | select(test("rekol|REKOL"))] | length' \
                "$SBHOME/.claude/settings.json")"
  actual_n="$(wc -l < "$TESTROOT/cmds.txt" | tr -d ' ')"
  [ "$expected_n" = "$actual_n" ] || {
    echo "command count mismatch ($expected_n vs $actual_n) — a command contains a newline"; false; }
  # Never let this pass vacuously: there must be real hooks to run.
  [ "$actual_n" -ge 6 ] || { echo "only $actual_n hook commands found — expected >= 6"; false; }

  # A non-interactive PATH: no BIN_DIR, no venv bin. This is the #159 condition.
  HOOK_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

  # ASSERT THE PRECONDITION (QA, 2026-08-24). This test is only meaningful if
  # `rekol` is genuinely unreachable on HOOK_PATH — otherwise every hook resolves
  # the bare name and the test passes while proving nothing. QA hit the same trap
  # from the other side: their probe used `bash -lc`, a LOGIN shell, which sources
  # the rc files the installer edits and re-adds BIN_DIR to PATH — testing the
  # exact opposite of the #159 condition and passing for the wrong reason. They
  # caught it only because they asserted the precondition explicitly.
  if env -i PATH="$HOOK_PATH" /bin/bash -c 'command -v rekol' >/dev/null 2>&1; then
    echo "PRECONDITION FAILED: rekol resolves on HOOK_PATH ($HOOK_PATH)"
    echo "  this test would pass for the wrong reason — it must run with rekol OFF the PATH"
    false
  fi

  # The environment comes from settings.json's own `env` block — the only channel
  # that reaches a hook subshell (install.sh Step 7.5 documents this). Reading it
  # from the file the installer just wrote means a variable the installer FAILS to
  # propagate makes this test fail, instead of being papered over by the harness
  # exporting it. That is how the missing REKOL_TOOLS_HOME was found.
  jq -r '(.env // {}) | to_entries[] | "\(.key)=\(.value)"' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/hookenv.txt"
  HOOK_ENV=""
  while IFS= read -r kv || [ -n "$kv" ]; do
    [ -n "$kv" ] && HOOK_ENV="$HOOK_ENV $kv"
  done < "$TESTROOT/hookenv.txt"
  echo "hook env from settings.json:$HOOK_ENV"
  failures=0
  checked=0
  while IFS= read -r cmd || [ -n "$cmd" ]; do
    [ -n "$cmd" ] || continue
    # Strip ONLY the trailing mask, so a real failure surfaces as a real exit code.
    unmasked="${cmd% 2>/dev/null || true}"
    unmasked="${unmasked% || true}"
    # The strip itself must not silently produce nothing (a sed delimiter bug once
    # emptied a command here and `bash -c ""` returned 0 for eleven "passes").
    [ -n "$unmasked" ] || { echo "strip emptied the command — would pass vacuously"; false; }
    case "$unmasked" in
      *"|| true"*) echo "masking survived the strip: $unmasked"; false ;;
    esac

    checked=$((checked + 1))
    if env -i HOME="$SBHOME" PATH="$HOOK_PATH" \
         XDG_CACHE_HOME="$XDG_CACHE_HOME" XDG_DATA_HOME="$XDG_DATA_HOME" \
         $HOOK_ENV \
         /bin/bash -c "$unmasked" </dev/null > "$TESTROOT/hook.out" 2>&1
    then :; else
      echo "--- HOOK FAILED (exit $?)"
      echo "    command: $unmasked"
      echo "    output : $(head -c 300 "$TESTROOT/hook.out")"
      failures=$((failures + 1))
    fi
  done < "$TESTROOT/cmds.txt"

  echo "executed $checked hook command(s), $failures failure(s)"
  [ "$checked" -eq "$actual_n" ] || { echo "loop skipped commands: $checked/$actual_n"; false; }
  [ "$failures" -eq 0 ]
}

# ------------------------------- #164 ----------------------------------------
# The index location depends on XDG_CACHE_HOME and the archive on XDG_DATA_HOME,
# and NEITHER reaches a hook — only settings.json's env block does. So a user who
# exports XDG_CACHE_HOME from .zshrc got TWO indexes: interactive `rekol search`
# read one while every hook wrote the other. Transcripts landed in the index
# nobody searched, search silently returned nothing, and `doctor` — running in the
# user's shell — printed the shell's path and "index is healthy".
#
# The fix pins the already-RESOLVED absolute paths, so there is nothing left for a
# hook to resolve differently. This test asserts that equivalence directly: the
# index a hook resolves, with NO XDG_CACHE_HOME in its environment, must be the
# one the installer recorded.
@test "a hook resolves the same index as the shell when XDG_CACHE_HOME is set (#164)" {
  SBHOME="$TESTROOT/sandhome-xdg"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-xdg"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  # A NON-default cache home, exactly like an `export` in .zshrc.
  CUSTOM_CACHE="$TESTROOT/custom-cache"
  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" XDG_CACHE_HOME="$CUSTOM_CACHE" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  installed_idx="$(manifest_index_dir "$REKOLH")"
  [ -n "$installed_idx" ]
  # Sanity: the install really did honour the custom cache home, otherwise this
  # test would be comparing two default paths and proving nothing.
  case "$installed_idx" in
    "$CUSTOM_CACHE"/*) : ;;
    *) echo "install ignored XDG_CACHE_HOME: $installed_idx"; false ;;
  esac

  # Build the hook environment the way a hook actually gets it: settings.json's
  # env block only. Note XDG_CACHE_HOME is deliberately ABSENT.
  jq -r '(.env // {}) | to_entries[] | "\(.key)=\(.value)"' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/xdgenv.txt"
  HOOK_ENV=""
  while IFS= read -r kv || [ -n "$kv" ]; do
    [ -n "$kv" ] && HOOK_ENV="$HOOK_ENV $kv"
  done < "$TESTROOT/xdgenv.txt"
  case "$HOOK_ENV" in
    *XDG_CACHE_HOME*) echo "test flaw: XDG_CACHE_HOME leaked into the hook env"; false ;;
  esac

  hook_idx="$(env -i HOME="$SBHOME" PATH="/usr/bin:/bin" $HOOK_ENV \
              "${BIN_DIR}/rekol" doctor 2>/dev/null | sed -n 's/^.*index cache: *//p' | head -1)"
  [ -n "$hook_idx" ] || { echo "could not read the index dir a hook resolves"; false; }
  [ "$hook_idx" = "$installed_idx" ] || {
    echo "SPLIT INDEX — shell: $installed_idx"
    echo "              hook : $hook_idx"; false; }
}

# ---------------- a rekol hook must NEVER be able to block a session ---------
# `_hook time-context` shipped on UserPromptSubmit without a `|| true`. A
# non-zero exit there fails the EVENT, which blocks prompt submission — rekol
# locking the user out of Claude Code. cli_hooks.py documents the soft-fail
# contract four times and every handler catches broadly, but that only guards
# exceptions INSIDE the handler; if the COMMAND fails (missing binary, deleted
# venv, exit 127) the shell returns non-zero and Python never runs.
@test "install repairs hooks that could block a session, and de-duplicates" {
  SBHOME="$TESTROOT/sandhome-softfail"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-softfail"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"

  # An install in the shape that locked a real user out: UserPromptSubmit and
  # Stop unguarded, plus the duplicate auto-reindex handler #176 introduced.
  cat > "$SBHOME/.claude/settings.json" <<'JSON'
{
  "hooks": {
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "rekol _hook time-context"}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "rekol _hook record-stop"}]}],
    "PostToolUse": [
      {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "$HOME/.local/share/rekol/hooks/auto-reindex.sh"}]},
      {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "/somewhere/rekol/hooks/auto-reindex.sh"}]}
    ],
    "SessionEnd": [{"matcher": "", "hooks": [{"type": "command", "command": "other-tool --run"}]}]
  },
  "env": {"KEEP_ME": "1"}
}
JSON

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # 1. NO rekol hook can fail its event any more.
  # The invariant is "cannot fail its event", not "ends with || true": a command
  # ending in `&` is asynchronous, so the shell returns 0 immediately and it can
  # never fail either. Appending a guard AFTER the `&` would start a separate
  # command and corrupt the handler — which an earlier version of this fix did,
  # caught by the #135 test asserting the detached form survives.
  blocking="$(jq -r '[.hooks[]?[]?.hooks[]?.command
                      | select(test("rekol|REKOL"))
                      | select(test("\\|\\| true$") | not)
                      | select(test("&[[:space:]]*$") | not)] | length' \
              "$SBHOME/.claude/settings.json")"
  [ "$blocking" -eq 0 ] || {
    echo "$blocking rekol hook(s) can still fail their event:"
    jq -r '.hooks[]?[]?.hooks[]?.command | select(test("rekol|REKOL"))
           | select(test("\\|\\| true$")|not) | select(test("&[[:space:]]*$")|not)' \
      "$SBHOME/.claude/settings.json"; false; }

  # 2. The duplicate auto-reindex handler is collapsed to one.
  ar="$(jq -r '[.hooks[]?[]?.hooks[]?.command | select(test("auto-reindex"))] | length' \
        "$SBHOME/.claude/settings.json")"
  [ "$ar" -eq 1 ] || { echo "expected 1 auto-reindex handler, found $ar"; false; }

  # 3. Another tool's hook and the user's env key are untouched — the migration
  #    rewrites settings.json, so proving it does not eat neighbours matters.
  run jq -e '[.hooks.SessionEnd[]?.hooks[]?.command | select(. == "other-tool --run")] | length == 1' \
    "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  run jq -e '.env.KEEP_ME == "1"' "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]

  # 4. Idempotent: a second install reports a no-op rather than re-appending.
  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]
  [[ "$output" == *"already soft-fail"* ]] || { echo "second run did not report a no-op"; echo "$output" | grep -i soft; false; }
}

@test "a wired hook runs the recorded path even when an orphan rekol is first on its PATH (#203)" {
  # THE #203 failure, end to end. The orphan console script is FIRST on the
  # hook's PATH, so `command -v rekol` succeeds and — under the old resolution
  # order — the hook ran the orphan and the session started with no memory.
  #
  # Note what this does NOT depend on: the interactive shell's PATH, or anything
  # doctor can see. Detection cannot close this; only resolution order can.
  SBHOME="$TESTROOT/sandhome-order"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-order"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ]

  # An orphan that leaves a trace if it is ever executed.
  orphan_dir="$TESTROOT/orphan-order/bin"
  mkdir -p "$orphan_dir"
  marker="$TESTROOT/orphan-ran.marker"
  {
    printf '#!/bin/sh\n'
    printf 'echo orphan-was-executed >> "%s"\n' "$marker"
    printf 'echo "ModuleNotFoundError: No module named rekol" >&2\n'
    printf 'exit 1\n'
  } > "$orphan_dir/rekol"
  chmod 755 "$orphan_dir/rekol"

  HOOK_PATH="$orphan_dir:/usr/bin:/bin:/usr/sbin:/sbin"

  # ASSERT THE PRECONDITION both ways round, or this proves nothing:
  # (a) the orphan really is what a bare `rekol` resolves to on HOOK_PATH, and
  # (b) the recorded shim really exists, so there is something to prefer.
  resolved="$(env -i PATH="$HOOK_PATH" /bin/sh -c 'command -v rekol' 2>/dev/null || true)"
  [ "$resolved" = "$orphan_dir/rekol" ] || {
    echo "PRECONDITION FAILED: bare 'rekol' resolves to '$resolved', not the orphan"; false; }
  [ -x "$BIN_DIR/rekol" ] || {
    echo "PRECONDITION FAILED: no recorded shim at $BIN_DIR/rekol to prefer"; false; }

  # POSITIVE TRACE (Codex, 2026-11-06). "The orphan did not run" is also
  # satisfied when NOTHING ran — every hook here is executed with errors
  # suppressed, so a command that silently fails to resolve anything at all
  # looks identical to a pass. Make the RECORDED binary leave its own trace and
  # require it, so the test distinguishes "preferred the recorded path" from
  # "executed neither".
  recorded_marker="$TESTROOT/recorded-ran.marker"
  mv "$BIN_DIR/rekol" "$BIN_DIR/rekol.real"
  {
    printf '#!/bin/sh\n'
    printf 'echo recorded-was-executed >> "%s"\n' "$recorded_marker"
    printf 'exec "%s" "$@"\n' "$BIN_DIR/rekol.real"
  } > "$BIN_DIR/rekol"
  chmod 755 "$BIN_DIR/rekol"

  jq -r '[.hooks[]?[]?.hooks[]?.command] | .[] | select(test("rekol|REKOL"))' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/cmds-order.txt"
  n="$(wc -l < "$TESTROOT/cmds-order.txt" | tr -d ' ')"
  [ "$n" -ge 6 ] || { echo "only $n hook commands found — expected >= 6"; false; }

  jq -r '(.env // {}) | to_entries[] | "\(.key)=\(.value)"' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/hookenv-order.txt"
  HOOK_ENV=""
  while IFS= read -r kv || [ -n "$kv" ]; do
    [ -n "$kv" ] && HOOK_ENV="$HOOK_ENV $kv"
  done < "$TESTROOT/hookenv-order.txt"

  while IFS= read -r cmd || [ -n "$cmd" ]; do
    [ -n "$cmd" ] || continue
    # shellcheck disable=SC2086
    env -i PATH="$HOOK_PATH" HOME="$SBHOME" $HOOK_ENV /bin/sh -c "$cmd" >/dev/null 2>&1 || true
  done < "$TESTROOT/cmds-order.txt"

  # The orphan must never have been executed by ANY wired hook.
  [ ! -f "$marker" ] || {
    echo "FAIL: a hook executed the orphan at $orphan_dir/rekol"
    echo "      resolution order still prefers PATH over the recorded path"
    cat "$marker"
    false; }

  # ...and the recorded binary must actually have been executed, or the
  # assertion above proves only that nothing ran.
  [ -f "$recorded_marker" ] || {
    echo "FAIL: no wired hook executed the recorded binary at $BIN_DIR/rekol"
    echo "      the orphan assertion above passes vacuously when neither runs"
    false; }
}

@test "re-running install UPGRADES hooks left in the old PATH-first form (#203)" {
  # Codex P1: Step 6.95 rewrote only BARE `rekol <subcommand>`, never the
  # previous `$(command -v rekol || echo '<path>')` rendering. Every existing
  # install carries that form, so the handler-presence checks saw a correctly
  # named handler and no-op'd: re-running left the PATH-first hooks byte-for-byte
  # as they were. Only the memory loader was upgraded, which is what made this
  # look fixed. So the changelog remedy and closing #203 were overclaims for
  # installs that already exist — which is every install.
  SBHOME="$TESTROOT/sandhome-upgrade"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-upgrade"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
    --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ] || { echo "$output"; false; }

  # Rewrite every wired rekol hook BACK to the old PATH-first rendering, which
  # is exactly the state an existing install is in before an upgrade.
  jq --arg bin "$BIN_DIR/rekol" '
    .hooks |= with_entries(
      .value |= map(
        if (.hooks? | type) == "array" then
          .hooks |= map(
            if ((.command? // "") | test("_hook|review --nudge|session-index")) then
              .command |= sub("\\$\\(if \\[ -x .*? fi\\)";
                              "$(command -v rekol || echo '"'"'" + $bin + "'"'"')")
            else . end)
        else . end))' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/downgraded.json"
  mv "$TESTROOT/downgraded.json" "$SBHOME/.claude/settings.json"

  downgraded="$(grep -c "command -v rekol || echo" "$SBHOME/.claude/settings.json" || true)"
  [ "$downgraded" -ge 1 ] || {
    echo "PRECONDITION FAILED: nothing was downgraded, so this proves nothing"; false; }

  # The upgrade under test.
  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
    --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ] || { echo "$output"; false; }

  left="$(grep -c "command -v rekol || echo" "$SBHOME/.claude/settings.json" || true)"
  [ "$left" -eq 0 ] || {
    echo "FAIL: $left hook(s) still carry the old PATH-first rendering"
    jq -r '[.hooks[]?[]?.hooks[]?.command] | .[] | select(test("command -v rekol"))' \
      "$SBHOME/.claude/settings.json"
    false; }

  # ONE handler per owned slot: the upgrade must rewrite in place, not append a
  # second copy alongside the old one. A duplicate would run every session.
  for handler in session-tasks record-stop time-context capture-nudge; do
    count="$(jq -r --arg h "_hook $handler" \
      '[.hooks[]?[]?.hooks[]?.command // "" | select(contains($h))] | length' \
      "$SBHOME/.claude/settings.json")"
    [ "$count" -eq 1 ] || {
      echo "FAIL: $handler is wired $count time(s), expected exactly 1"; false; }
  done

  # And the upgraded hooks must really prefer the recorded binary when executed.
  orphan_dir="$TESTROOT/upgrade-orphan/bin"
  mkdir -p "$orphan_dir"
  orphan_marker="$TESTROOT/upgrade-orphan-ran.marker"
  {
    printf '#!/bin/sh\n'
    printf 'echo orphan-was-executed >> "%s"\n' "$orphan_marker"
    printf 'exit 0\n'
  } > "$orphan_dir/rekol"
  chmod 755 "$orphan_dir/rekol"

  recorded_marker="$TESTROOT/upgrade-recorded-ran.marker"
  mv "$BIN_DIR/rekol" "$BIN_DIR/rekol.real"
  {
    printf '#!/bin/sh\n'
    printf 'echo recorded-was-executed >> "%s"\n' "$recorded_marker"
    printf 'exec "%s" "$@"\n' "$BIN_DIR/rekol.real"
  } > "$BIN_DIR/rekol"
  chmod 755 "$BIN_DIR/rekol"

  HOOK_PATH="$orphan_dir:/usr/bin:/bin:/usr/sbin:/sbin"
  jq -r '(.env // {}) | to_entries[] | "\(.key)=\(.value)"' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/upgrade-env.txt"
  HOOK_ENV=""
  while IFS= read -r kv || [ -n "$kv" ]; do
    [ -n "$kv" ] && HOOK_ENV="$HOOK_ENV $kv"
  done < "$TESTROOT/upgrade-env.txt"

  jq -r '[.hooks[]?[]?.hooks[]?.command] | .[] | select(test("rekol|REKOL"))' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/upgrade-cmds.txt"
  while IFS= read -r cmd || [ -n "$cmd" ]; do
    [ -n "$cmd" ] || continue
    # shellcheck disable=SC2086
    env -i PATH="$HOOK_PATH" HOME="$SBHOME" $HOOK_ENV /bin/sh -c "$cmd" >/dev/null 2>&1 || true
  done < "$TESTROOT/upgrade-cmds.txt"

  [ ! -f "$orphan_marker" ] || {
    echo "FAIL: an UPGRADED hook executed the orphan — the migration did not take"
    cat "$orphan_marker"; false; }
  [ -f "$recorded_marker" ] || {
    echo "FAIL: no upgraded hook executed the recorded binary"
    echo "      the orphan assertion passes vacuously when neither runs"; false; }
}

# Shared body for the awkward-path installs below. $1 = the bin dir to install
# into. Asserts the install succeeds AND that every wired hook command parses,
# because a quoting or substitution fault shows up only when something runs it.
_install_into_bin_dir_and_run_hooks() {
  nasty="$1"
  mkdir -p "$nasty"
  SBHOME="$TESTROOT/sandhome-$2"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-$2"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$nasty"
  [ "$status" -eq 0 ] || { echo "install failed for bin-dir '$nasty':"; echo "$output" | tail -15; false; }
  [ -x "$nasty/rekol" ] || { echo "no shim at '$nasty/rekol'"; false; }

  jq -r '[.hooks[]?[]?.hooks[]?.command] | .[] | select(test("rekol|REKOL"))' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/cmds-$2.txt"
  n="$(wc -l < "$TESTROOT/cmds-$2.txt" | tr -d ' ')"
  [ "$n" -ge 6 ] || { echo "only $n hook commands found — expected >= 6"; false; }

  jq -r '(.env // {}) | to_entries[] | "\(.key)=\(.value)"' \
    "$SBHOME/.claude/settings.json" > "$TESTROOT/env-$2.txt"
  HOOK_ENV=""
  while IFS= read -r kv || [ -n "$kv" ]; do
    [ -n "$kv" ] && HOOK_ENV="$HOOK_ENV $kv"
  done < "$TESTROOT/env-$2.txt"

  faults=0
  while IFS= read -r cmd || [ -n "$cmd" ]; do
    [ -n "$cmd" ] || continue
    # shellcheck disable=SC2086
    err="$(env -i PATH="/usr/bin:/bin" HOME="$SBHOME" $HOOK_ENV /bin/sh -c "$cmd" 2>&1 >/dev/null || true)"
    case "$err" in
      *"unexpected EOF"* | *"syntax error"* | *"unterminated"*)
        echo "QUOTING FAULT in: $cmd"; echo "  -> $err"; faults=$((faults + 1)) ;;
    esac
  done < "$TESTROOT/cmds-$2.txt"
  [ "$faults" -eq 0 ] || { echo "$faults hook command(s) failed to parse"; false; }
}

@test "install survives a bin-dir containing a space and an ampersand, and its hooks run" {
  # The `&` is the load-bearing character here. bash >= 5.2 expands `&` in the
  # replacement of ${var//pat/rep} to the matched text, and every snippet
  # placeholder is filled with a PATH — so `a&b` produced `@REKOL@@REKOL@`, the
  # placeholder survived, and install.sh refused. Measured in a container:
  # without `shopt -u patsub_replacement` this exact path gives EXIT=1 and 0
  # hooks; with it, EXIT=0 and 11 hooks.
  #
  # NOTE FOR ANYONE READING A GREEN RUN ON macOS: bash 3.2 has no such option,
  # so this test cannot fail there. It is meaningful only on bash >= 5.2, i.e.
  # the ubuntu job.
  _install_into_bin_dir_and_run_hooks "$TESTROOT/My & Dir bin" "amp"
}

@test "install survives a bin-dir containing an apostrophe" {
  # SKIPPED, not deleted: reproduction for #222. install.sh dies with
  # `eval: line 165: unexpected EOF` because 32 `run "..."` sites interpolate
  # paths into a string that `run` then evals, and an apostrophe closes the
  # quote early. Pre-existing and out of scope here; unskip with that sweep.
  skip "reproduction for #222 (run() evals interpolated paths); unskip with the sweep"
  _install_into_bin_dir_and_run_hooks "$TESTROOT/My Dir's bin" "apos"
}

@test "the hook migration leaves commands rekol does not own alone (#203)" {
  # Codex, 2026-11-06: the migration applied its gsub to every command string
  # with no ownership or shell-context boundary, so it rewrote a `printf` that
  # merely PRINTS the old text, and a user's own command built on it. This step
  # writes to the user's settings.json, so it must only touch invocations that
  # are ours.
  SBHOME="$TESTROOT/sandhome-ownership"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-ownership"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  cat > "$SBHOME/.claude/settings.json" <<'JSON'
{"hooks":{"SessionStart":[{"matcher":"","hooks":[
  {"type":"command","command":"\"$(command -v rekol || echo '/old/rekol')\" _hook session-tasks"},
  {"type":"command","command":"\"$(command -v rekol || echo '/old/rekol')\" my-custom-thing --flag"}
]}]}}
JSON

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
    --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ] || { echo "$output"; false; }

  # REQUIRE THE UPGRADE, not installer exit 0: a swallowed jq error leaves the
  # migration unapplied while the installer still succeeds (Codex).
  upgraded="$(jq -r '[.. | objects | .command? // empty
                      | select(contains("_hook session-tasks") and contains("if [ -x"))] | length' \
             "$SBHOME/.claude/settings.json")"
  [ "$upgraded" -ge 1 ] || {
    echo "FAIL: the owned session-tasks hook was NOT upgraded"
    jq -r '[.. | objects | .command? // empty] | .[]' "$SBHOME/.claude/settings.json"
    false; }

  # The user's own command must still carry the OLD text: rekol does not own it.
  kept="$(jq -r '[.. | objects | .command? // empty
                  | select(contains("my-custom-thing") and contains("command -v rekol || echo"))] | length' \
          "$SBHOME/.claude/settings.json")"
  [ "$kept" -eq 1 ] || {
    echo "FAIL: rekol rewrote a command it does not own"
    jq -r '[.. | objects | .command? // empty | select(contains("my-custom-thing"))] | .[]' \
      "$SBHOME/.claude/settings.json"
    false; }

  # And the skip is reported rather than silent.
  [[ "$output" == *"this migration step did not rewrite them"* ]] || {
    echo "FAIL: the preserved occurrence was not reported"; echo "$output" | tail -20; false; }

  # The message must NOT claim more than this step delivers. Step 7's soft-fail
  # pass still appends to any command mentioning rekol (#235), so a blanket
  # "rekol does not rewrite what it does not own" is false in this very run, and
  # printing a guarantee the next step breaks is worse than printing nothing.
  [[ "$output" != *"rekol does not rewrite what it does not own"* ]] || {
    echo "FAIL: printed a byte-identical guarantee that Step 7 then breaks"
    echo "$output" | tail -20; false; }
  [[ "$output" == *"#235"* ]] || {
    echo "FAIL: the known gap is not named, so the user cannot act on it"
    echo "$output" | tail -20; false; }
}

@test "the migration rewrites ONLY whole commands rekol itself renders (#203)" {
  # The exact-match contract, as four controls and two positives.
  #
  # This matcher had three rounds of near-misses, each shipped as a refinement of
  # the same regex, and each time a reviewer found another command it should not
  # have touched. The rule is now an equality: the WHOLE command has to be a
  # rendering the installer produces for a handler on its allowlist, with only
  # the recorded path free to differ. The controls below are the shapes a pattern
  # kept getting wrong.
  SBHOME="$TESTROOT/sandhome-exact"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-exact"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  # POSITIVE 1  ours, with the soft-fail tail                     -> upgraded
  # POSITIVE 2  ours, recorded path contains a close-paren        -> upgraded
  #             (a pattern that stops at the first `)` truncates it; splitting
  #             on the literal text either side of the path does not)
  # CONTROL A   a literal STRING holding the old form and a real handler name
  # CONTROL B   ours, plus an option rekol does not pass
  # CONTROL C   a handler name rekol does not own
  # CONTROL D   the user's own command, which merely mentions rekol
  cat > "$SBHOME/.claude/settings.json" <<'JSON'
{"hooks":{"SessionStart":[{"matcher":"","hooks":[
  {"type":"command","command":"\"$(command -v rekol || echo '/old/bin/rekol')\" _hook session-tasks 2>/dev/null || true"},
  {"type":"command","command":"\"$(command -v rekol || echo '/tmp/a)b/bin/rekol')\" _hook update-check 2>/dev/null || true"},
  {"type":"command","command":"printf '%s\\n' '\"$(command -v rekol || echo /x)\" _hook time-context 2>/dev/null || true'"},
  {"type":"command","command":"\"$(command -v rekol || echo '/old/bin/rekol')\" _hook time-context --verbose 2>/dev/null || true"},
  {"type":"command","command":"\"$(command -v rekol || echo '/old/bin/rekol')\" _hook not-ours 2>/dev/null || true"},
  {"type":"command","command":"my-tool --uses rekol --flag"}
]}]}}
JSON
  jq -e . "$SBHOME/.claude/settings.json" >/dev/null || {
    echo "FAIL: the fixture itself is not valid JSON"; false; }

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
    --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ] || { echo "$output"; false; }

  all_cmds="$(jq -r '[.. | objects | .command? // empty] | .[]' "$SBHOME/.claude/settings.json")"

  # REQUIRE THE UPGRADE, not installer exit 0: a swallowed jq error leaves the
  # migration unapplied while the installer still succeeds (Codex).
  for want in "_hook session-tasks" "_hook update-check"; do
    upgraded="$(jq -r --arg w "$want" '[.. | objects | .command? // empty
                 | select(contains($w) and contains("if [ -x"))] | length' \
               "$SBHOME/.claude/settings.json")"
    [ "$upgraded" -ge 1 ] || {
      echo "FAIL: the owned $want hook was NOT upgraded"; echo "$all_cmds"; false; }
  done

  # And no trace of the old form is left on either positive.
  stale="$(jq -r '[.. | objects | .command? // empty
            | select((contains("_hook session-tasks") or contains("_hook update-check"))
                     and contains("command -v rekol || echo"))] | length' \
          "$SBHOME/.claude/settings.json")"
  [ "$stale" -eq 0 ] || {
    echo "FAIL: an owned hook still carries the old form"; echo "$all_cmds"; false; }

  # CONTROLS: the old text intact, and in particular NOT carrying the new
  # invocation. The fragments are chosen to be UNIQUE to each control: "printf"
  # is not, because the new invocation itself contains `printf %s`, so the
  # memory loader matched and the check failed on the wrong command.
  #
  # Byte-identity is deliberately NOT asserted here. On this branch Step 6.96
  # still appends ` 2>/dev/null || true` to anything mentioning rekol (#235), so
  # these come back changed by a LATER step. What this test owns is that Step
  # 6.95 did not rewrite the invocation; #235 owns the soft-fail boundary.
  for frag in "echo /x" "--verbose" "_hook not-ours"; do
    kept="$(jq -r --arg f "$frag" '[.. | objects | .command? // empty
             | select(contains($f) and contains("command -v rekol || echo"))] | length' \
           "$SBHOME/.claude/settings.json")"
    [ "$kept" -eq 1 ] || {
      echo "FAIL: rekol rewrote a command it does not own: $frag"; echo "$all_cmds"; false; }
    rewritten="$(jq -r --arg f "$frag" '[.. | objects | .command? // empty
                  | select(contains($f) and contains("if [ -x"))] | length' \
                "$SBHOME/.claude/settings.json")"
    [ "$rewritten" -eq 0 ] || {
      echo "FAIL: $frag was rewritten to the new invocation"; echo "$all_cmds"; false; }
  done
  users="$(jq -r '[.. | objects | .command? // empty
            | select(contains("my-tool --uses rekol"))] | length' \
          "$SBHOME/.claude/settings.json")"
  [ "$users" -eq 1 ] || {
    echo "FAIL: the user's own command did not survive"; echo "$all_cmds"; false; }

  # The skip is reported, and counted as OCCURRENCES: three commands here still
  # carry the old text, and `grep -c` counts LINES, which on a jq-formatted file
  # agrees only by accident.
  [[ "$output" == *"this migration step did not rewrite them"* ]] || {
    echo "FAIL: the preserved occurrences were not reported"; echo "$output" | tail -20; false; }
  [[ "$output" == *"left 3 occurrence(s)"* ]] || {
    echo "FAIL: the preserved COUNT is wrong (expected 3)"
    echo "$output" | grep -i "occurrence" || echo "  (no occurrence line at all)"
    false; }
}

@test "the preserved-count report runs even when NOTHING was migrated (#203)" {
  # The case that matters most, and the one the report used to miss: a
  # settings.json holding only commands rekol does not own. The migration
  # correctly changes nothing, so the whole "we changed something" branch is
  # skipped — and the diagnostic used to live inside it, which made its silence
  # indistinguishable from having nothing to say.
  SBHOME="$TESTROOT/sandhome-nomig"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-nomig"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  cat > "$SBHOME/.claude/settings.json" <<'JSON'
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[
  {"type":"command","command":"\"$(command -v rekol || echo '/old/bin/rekol')\" _hook not-ours 2>/dev/null || true"},
  {"type":"command","command":"\"$(command -v rekol || echo '/old/bin/rekol')\" gate --strict"}
]}]}}
JSON

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
    --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ] || { echo "$output"; false; }

  [[ "$output" == *"left 2 occurrence(s)"* ]] || {
    echo "FAIL: nothing was migrated, so the skip report did not run"
    echo "$output" | grep -i "occurrence" || echo "  (no occurrence line at all)"
    false; }
  [[ "$output" == *"#235"* ]] || {
    echo "FAIL: the known gap is not named, so the user cannot act on it"; false; }
}

@test "the migration allowlist covers every hook command a fresh install writes (#203)" {
  # THE DRIFT GUARD. The matcher is an exact comparison against an allowlist, so
  # a handler added to the installer and not to that list would be left
  # un-upgradeable forever — which is the failure #203 itself was. Rather than
  # trusting two lists to be edited together, this reads what a real install
  # WROTE and requires every rekol command in it to be an allowlist entry.
  SBHOME="$TESTROOT/sandhome-drift"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-drift"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
    --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ] || { echo "$output"; false; }

  # The allowlist, read out of install.sh itself. It holds no apostrophes by
  # construction (they would end the single-quoted jq program), so stripping
  # them is safe and leaves valid JSON.
  allow="$(sed -n "/REKOL_OWNED_HOOKS_JSON=/,/^  ]'/p" "$COMPONENT_DIR/install.sh" \
           | sed "s/.*REKOL_OWNED_HOOKS_JSON=//" | tr -d "'")"
  echo "$allow" | jq -e 'type == "array" and length > 0' >/dev/null || {
    echo "FAIL: could not read the allowlist out of install.sh"; echo "$allow"; false; }

  # Every command the install wrote that STARTS with rekol's new invocation.
  # The memory loader starts with its own sentinel, so it is excluded here for
  # the same reason it is off the allowlist: Step 7 upgrades it whole.
  written="$(jq -r '[.. | objects | .command? // empty
      | select(startswith("nohup \"$(if [ -x") or startswith("\"$(if [ -x"))] | .[]' \
      "$SBHOME/.claude/settings.json")"
  [ -n "$written" ] || { echo "FAIL: the install wrote no rekol hook commands"; false; }

  while IFS= read -r cmd; do
    [ -n "$cmd" ] || continue
    tail_text="${cmd#*fi)\" }"
    [ "$tail_text" != "$cmd" ] || {
      echo "FAIL: could not split the invocation off: $cmd"; false; }
    found="$(echo "$allow" | jq -r --arg t "$tail_text" '
        [ .[] | (.argtail + (.suffix // " 2>/dev/null || true")) ] | index($t) // "no"')"
    [ "$found" != "no" ] || {
      echo "FAIL: the installer writes a hook that is NOT on the migration allowlist."
      echo "  command tail: [$tail_text]"
      echo "  allowlist:    $(echo "$allow" | jq -c '[.[] | .argtail]')"
      echo "  Add it to REKOL_OWNED_HOOKS_JSON in install.sh, or an existing"
      echo "  install can never be upgraded off the old invocation."
      false; }
  done <<< "$written"
}

@test "a v0.5.13-shaped upgrade leaves exactly one of every handler, twice (#236)" {
  # #236 was filed as no-hold because the duplicates had only been measured on a
  # hand-edited settings.json. Product asked whether an ORDINARY upgrade produces
  # them, which matters because duplicate memory injection would meet the bar.
  #
  # Measured on a real v0.5.13 install upgraded with this branch (plus #239):
  # one of every handler on the first run and on the second, no old-form text
  # left. This test pins that at the shape level so a regression in the exact
  # matcher shows up as duplicates rather than as a silent no-op.
  #
  # The fixture is the v0.5.13 RENDERING, read out of that tag's snippets, not a
  # hand-edited file: the PATH-first invocation, the loader without its
  # `: rekol:backend=` marker, and the old auto-reindex path.
  SBHOME="$TESTROOT/sandhome-upgrade-dupes"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-upgrade-dupes"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  old_inv="\"\$(command -v rekol || echo '/old/bin/rekol')\""
  loader="HOME_DIR=\"\${REKOL_HOME:-\$MEMORY_HOME}\"; IDX=\"\$HOME_DIR/REKOL.md\"; if [ -n \"\$HOME_DIR\" ] && [ -f \"\$IDX\" ]; then cat \"\$IDX\"; fi; ${old_inv} _hook session-confidence 2>/dev/null || true"
  jq -n --arg loader "$loader" \
        --arg cov "${old_inv} _hook session-coverage 2>/dev/null || true" \
        --arg tasks "${old_inv} _hook session-tasks 2>/dev/null || true" \
        --arg upd "${old_inv} _hook update-check 2>/dev/null || true" \
        --arg reindex '$HOME/.local/share/rekol/hooks/auto-reindex.sh 2>/dev/null || true' \
        --arg notice 'if [ -n "${REKOL_HOME:-$MEMORY_HOME}" ]; then echo '"'"'[rekol] session ended'"'"'; fi 2>/dev/null || true' \
        --arg idx "nohup ${old_inv} session-index --incremental >/dev/null 2>&1 &" \
        --arg nudge "${old_inv} review --nudge 2>/dev/null || true" \
        --arg tc "${old_inv} _hook time-context 2>/dev/null || true" \
        --arg cn "${old_inv} _hook capture-nudge 2>/dev/null || true" \
        --arg rs "${old_inv} _hook record-stop 2>/dev/null || true" \
    '{hooks:{
      SessionStart:[{matcher:"",hooks:[{type:"command",command:$loader},
                                       {type:"command",command:$cov},
                                       {type:"command",command:$tasks},
                                       {type:"command",command:$upd}]}],
      PostToolUse:[{matcher:"Edit|Write|MultiEdit",hooks:[{type:"command",command:$reindex}]}],
      SessionEnd:[{matcher:"",hooks:[{type:"command",command:$notice},
                                      {type:"command",command:$idx},
                                      {type:"command",command:$nudge}]}],
      UserPromptSubmit:[{matcher:"",hooks:[{type:"command",command:$tc},
                                            {type:"command",command:$cn}]}],
      Stop:[{matcher:"",hooks:[{type:"command",command:$rs}]}]}}' \
    > "$SBHOME/.claude/settings.json"
  jq -e . "$SBHOME/.claude/settings.json" >/dev/null || { echo "bad fixture"; false; }

  handler_counts() {
    jq -r '[.. | objects | .command? // empty]
           | map(if test("_hook [a-z-]+") then (capture("_hook (?<h>[a-z-]+)").h)
                 elif test("review --nudge") then "review-nudge"
                 elif test("session-index --incremental") then "session-index"
                 elif test("auto-reindex") then "auto-reindex"
                 else empty end)
           | group_by(.) | map("\(.[0])=\(length)") | sort | join(" ")' \
      "$SBHOME/.claude/settings.json"
  }

  for run in 1 2; do
    run env -u MEMORY_HOME -u TEST_MODE \
      REKOL_HOME="$REKOLH" HOME="$SBHOME" \
      "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
        --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
    [ "$status" -eq 0 ] || { echo "run $run failed"; echo "$output"; false; }

    counts="$(handler_counts)"
    # EXACTLY ONE of each. A duplicated memory loader would cat REKOL.md into
    # every session twice; a duplicated handler runs it twice per event.
    dupes="$(printf '%s\n' "$counts" | tr ' ' '\n' | grep -v '=1$' || true)"
    [ -z "$dupes" ] || {
      echo "FAIL: run $run produced duplicate handlers: $dupes"
      echo "  all: $counts"
      false; }

    # And nothing is left in the old rendering, or a later run would append.
    stale="$(grep -c "command -v rekol || echo" "$SBHOME/.claude/settings.json" || true)"
    [ "${stale:-0}" -eq 0 ] || {
      echo "FAIL: run $run left $stale old-form invocation(s)"
      jq -r '[.. | objects | .command? // empty | select(test("command -v rekol"))] | .[]' \
        "$SBHOME/.claude/settings.json"
      false; }
  done

  # The loader ends marked AND soft-failing, which is what makes the second run
  # recognise it instead of appending a second one.
  after="$(jq -r '[.. | objects | .command? // empty | select(test("REKOL.md"))] | .[0]' \
          "$SBHOME/.claude/settings.json")"
  [[ "$after" == *": rekol:backend=install.sh"* ]] || {
    echo "FAIL: the loader was not upgraded to the marked form"; false; }
  [[ "$after" == *"|| true"* ]] || {
    echo "FAIL: the upgraded loader is not soft-failing"; false; }
}

@test "the UNQUOTED fallback form is upgraded, on every handler (#221)" {
  # QA, 2026-11-07: this form was left behind while the same hook with
  # `echo '<path>'` quoted was upgraded. The quotes were the only difference,
  # and the hooks left behind kept resolving PATH first — the bug #203 exists
  # to fix — on `session-tasks`, `update-check` and `capture-nudge`.
  #
  # MEASURED PROVENANCE: no release tag wrote this form. It was in 21e6e8a and
  # ec2f9db (2026-08-12, untagged); faa834b added the quotes the same day and
  # b0db220 is what v0.5.2/v0.5.4/v0.5.13 contain. So it comes from installing
  # a checkout of main, which is how it reached a real file.
  #
  # GENERIC PATHS ONLY in this fixture (product's instruction): nothing here is
  # from anyone's real settings.json.
  SBHOME="$TESTROOT/sandhome-unquoted"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-unquoted"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  old="/opt/generic/bin/rekol"
  # Three handlers in the unquoted form, one quoted as the control that already
  # worked, and one command of the user's that must not be touched.
  jq -n --arg o "$old" '{hooks:{
      SessionStart:[{hooks:[
        {type:"command",command:("\"$(command -v rekol || echo " + $o + ")\" _hook session-tasks 2>/dev/null || true")},
        {type:"command",command:("\"$(command -v rekol || echo " + $o + ")\" _hook update-check 2>/dev/null || true")}]}],
      UserPromptSubmit:[{hooks:[
        {type:"command",command:("\"$(command -v rekol || echo " + $o + ")\" _hook capture-nudge 2>/dev/null || true")},
        {type:"command",command:("\"$(command -v rekol || echo '"'"'" + $o + "'"'"')\" _hook time-context 2>/dev/null || true")},
        {type:"command",command:"my-own-gate --strict _hook session-tasks"}]}]}}' \
    > "$SBHOME/.claude/settings.json"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ] || { echo "$output"; false; }

  # 1. NOT ONE command may still carry the old PATH-first `command -v` head.
  #    Asserted across the whole file rather than per handler: the defect was
  #    one form slipping through, so a per-handler check could pass on three of
  #    four and still be the same bug.
  left="$(jq -r '[.. | objects | .command? // empty
          | select(test("command -v rekol \\|\\| echo"))] | length' \
          "$SBHOME/.claude/settings.json")"
  [ "$left" -eq 0 ] || {
    echo "FAIL: $left hook(s) still resolve PATH first:"
    jq -r '[.. | objects | .command? // empty
            | select(test("command -v rekol \\|\\| echo"))] | .[]' \
      "$SBHOME/.claude/settings.json"
    false; }

  # 2. All four rekol handlers are still present and now recorded-path-first.
  for h in session-tasks update-check capture-nudge time-context; do
    n="$(jq -r --arg h "$h" '[.. | objects | .command? // empty
          | select(test("_hook " + $h)) | select(test("if \\[ -x"))] | length' \
          "$SBHOME/.claude/settings.json")"
    [ "$n" -ge 1 ] || {
      echo "FAIL: $h was not upgraded to the recorded-path-first form"
      jq -r --arg h "$h" '[.. | objects | .command? // empty
             | select(test("_hook " + $h))] | .[]' "$SBHOME/.claude/settings.json"
      false; }
  done

  # 3. The generic path is gone from every rekol hook — the upgrade replaced the
  #    invocation rather than merely prepending to it.
  stale="$(jq -r --arg o "$old" '[.. | objects | .command? // empty
           | select(test("_hook ")) | select(contains($o))] | length' \
           "$SBHOME/.claude/settings.json")"
  [ "$stale" -eq 0 ] || { echo "FAIL: $stale hook(s) still name the old path"; false; }

  # 4. And the user's own command is byte-identical. It mentions `_hook
  #    session-tasks`, so a handler-keyed rewrite would have eaten it.
  mine="$(jq -r '[.. | objects | .command? // empty
          | select(. == "my-own-gate --strict _hook session-tasks")] | length' \
          "$SBHOME/.claude/settings.json")"
  [ "$mine" -eq 1 ] || { echo "FAIL: rekol rewrote a hook it does not own"; false; }
}

@test "an unquoted fallback that is not a literal path is left alone (#221)" {
  # The allowlist rule from #239, applied here: unquoted means the shell will
  # expand and word-split it, so anything but literal characters changes what
  # runs. Those are not ours and must survive byte-identical.
  SBHOME="$TESTROOT/sandhome-unquoted-notours"
  mkdir -p "$SBHOME/.claude"
  REKOLH="$TESTROOT/rekolhome-unquoted-notours"
  mkdir -p "$REKOLH"
  printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
    > "$REKOLH/rekol.config.yaml"
  printf '# Memory Index\n\n- a pointer\n' > "$REKOLH/REKOL.md"

  a='"$(command -v rekol || echo /opt/generic bin/rekol)" _hook session-tasks'
  b='"$(command -v rekol || echo ${HOME}/bin/rekol)" _hook update-check'
  c='"$(command -v rekol || echo /tmp/x;id)" _hook capture-nudge'
  jq -n --arg a "$a" --arg b "$b" --arg c "$c" \
    '{hooks:{SessionStart:[{hooks:[{type:"command",command:$a},
                                   {type:"command",command:$b},
                                   {type:"command",command:$c}]}]}}' \
    > "$SBHOME/.claude/settings.json"

  run env -u MEMORY_HOME -u TEST_MODE \
    REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/install.sh" --no-skill --no-shellrc \
      --tools-home "$TOOLS_HOME" --bin-dir "$BIN_DIR"
  [ "$status" -eq 0 ] || { echo "$output"; false; }

  # ASSERTED ON THE INVOCATION, not on the whole string, and deliberately so.
  # On this branch a separate step still softens by pattern (`test("rekol|REKOL")`
  # at install.sh:1243), so it appends ` 2>/dev/null || true` to these commands.
  # That is #235, it is fixed on #239's branch by whole-command equality, and it
  # is not #221's to fix here — asserting byte-identity would make this test fail
  # for someone else's defect and pass only once #239 merges. #221's claim is
  # narrower and is what is checked: the MIGRATION does not rewrite an
  # invocation whose unquoted path is not a literal path.
  for want in "$a" "$b" "$c"; do
    head="${want%%\" _hook\"*}"
    got="$(jq -r --arg h "$head" '[.. | objects | .command? // empty
            | select(startswith($h))] | length' "$SBHOME/.claude/settings.json")"
    [ "$got" -eq 1 ] || {
      echo "FAIL: the migration rewrote an invocation that is not ours:"
      echo "  expected this invocation intact: $head"
      jq -r '[.. | objects | .command? // empty] | .[]' "$SBHOME/.claude/settings.json"
      false; }
  done

  # NOT asserting that no recorded-path-first command exists for these handlers:
  # the installer legitimately wires its OWN session-tasks, update-check and
  # capture-nudge hooks in that form. It cannot upgrade a command it does not
  # own, so the correct outcome is the user's command surviving intact AND
  # rekol's own being added alongside — which is what the loop above checks and
  # what my first attempt at this assertion got wrong by counting both.
}
