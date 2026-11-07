#!/usr/bin/env bats
# Uninstaller tests for rekol/uninstall.sh.
#
# Run with:   bats rekol/tests/test_uninstall.bats
# Requires:   bats-core (brew install bats-core) and jq.
#
# SAFETY: every test runs against a sandboxed HOME under a throwaway mktemp dir.
# Nothing here ever touches the real ~/.claude/settings.json, ~/.zshrc, or
# ~/.local/share/rekol — HOME, TOOLS_HOME, BIN_DIR and REKOL_HOME are all
# redirected into ${TESTROOT}. install.sh is run with --no-shellrc removed only
# inside the sandbox (HOME points at the sandbox), so .zshrc edits land on the
# sandboxed file. The teardown rm -rf's the whole sandbox.

# Sandbox guard (#223): shared with test_install.bats so the two cannot drift.
# Provides rekol_bats_clear_path_overrides, rekol_bats_require_sandboxed_env and
# assert_sandboxed. Read the header there for why the refusal sits at the
# destructive call rather than in setup().
load sandbox_guard

setup() {

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
    #
    # Losing this call no longer costs the machine its install: every destructive
    # invocation calls rekol_bats_require_sandboxed_env, which refuses when an
    # override survived (#223). That is why the clearing lives in the shared
    # guard -- so the refusal and the thing it checks for cannot drift apart.
    rekol_bats_clear_path_overrides

    command -v jq >/dev/null 2>&1 || skip "jq required for uninstall tests"
    TESTROOT="$(mktemp -d)"
    COMPONENT_DIR="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"

    # Sandboxed HOME — every ~ in install.sh/uninstall.sh resolves under here.
    SBHOME="${TESTROOT}/home"
    mkdir -p "${SBHOME}/.claude"

    TOOLS_HOME="${TESTROOT}/tools"
    BIN_DIR="${SBHOME}/bin"
    REKOLH="${TESTROOT}/rekolhome"
    mkdir -p "${REKOLH}"
    # Fast hashing embedder; no model download. git_track off so install Step 8.5
    # doesn't init a repo. Non-empty config means template seeding is skipped, so
    # we seed our own markdown below to assert it survives uninstall.
    printf 'embedding_model: test-hashing\nupdate_check: false\nsession_search_enabled: false\ngit_track: false\n' \
        > "${REKOLH}/rekol.config.yaml"

    # Pin the login shell so install/uninstall target .zshrc deterministically
    # (the host CI may run bash; the bash path is covered in test_install.bats).
    SHELL="/bin/zsh"
    export COMPONENT_DIR TOOLS_HOME BIN_DIR REKOLH SBHOME TESTROOT SHELL
}

# assert_sandboxed now lives in tests/sandbox_guard.bash (#223). The copy that
# stood here allowed a blanket /tmp/* and was only ever called about the index
# dir, at 2 of this file's 20 destructive sites.

# Everything this suite points the uninstaller at, asserted before it deletes.
# Call from any destructive invocation. The index dir is read from the manifest
# because install.sh, not the test, decides where it goes; an absent manifest
# yields an empty string, which assert_sandboxed treats as "nothing to delete".
assert_destructive_targets_sandboxed() {
    rekol_bats_require_sandboxed_env || return 1
    assert_sandboxed "${TOOLS_HOME:-}" "tools home" || return 1
    assert_sandboxed "${BIN_DIR:-}" "bin dir" || return 1
    assert_sandboxed "${REKOLH:-}" "rekol home" || return 1
    assert_sandboxed "$(manifest_index_dir)" "index dir" || return 1
    # REKOL_ARCHIVE_DIR is covered by rekol_bats_require_sandboxed_env above --
    # it refuses an outside value and permits a sandbox-internal one, which is
    # what the archive tests deliberately set.
}

teardown() {
    rm -rf "${TESTROOT}"
}

# Runs install.sh into the sandbox with hooks + skill + shellrc all ON, so the
# uninstaller has a complete install to reverse. Seeds a marker .zshrc line and
# user markdown first.
do_full_install() {
    # A pre-existing user .zshrc line that must survive uninstall untouched.
    printf '# my own setup\nexport EDITOR=vim\n' > "${SBHOME}/.zshrc"
    # User markdown memory that uninstall must NEVER delete.
    mkdir -p "${REKOLH}/always" "${REKOLH}/topics"
    printf -- '---\nname: id\ndescription: d\ntype: always\n---\nmy precious memory\n' \
        > "${REKOLH}/always/identity.md"
    printf 'a topic\n' > "${REKOLH}/topics/sailing.md"

    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"
    [ "$status" -eq 0 ]
}

# Default uninstall helper — deliberately passes NO --tools-home/--bin-dir, so it
# exercises the real-world auto-discovery path: install wrote a manifest under
# $REKOL_HOME/.install-logs/, and uninstall must read it to find the (custom)
# TOOLS_HOME/BIN_DIR on its own. This is what a user gets running `./uninstall.sh`
# with no flags after a custom install.
run_uninstall() {
    assert_destructive_targets_sandboxed || return 1
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/uninstall.sh" "$@"
}

# Explicit-flags variant for the few tests that must pin the paths directly
# (e.g. asserting the flag-precedence path, or running against a machine that
# never had an install/manifest).
run_uninstall_with_flags() {
    assert_destructive_targets_sandboxed || return 1
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/uninstall.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}" "$@"
}

# Archive variant — forwards REKOL_ARCHIVE_DIR so uninstall resolves the fake
# archive the test built. Exists so the archive tests go through the same guard
# as everything else: they were seven of this file's hand-rolled `run env …
# uninstall.sh` invocations, and --purge-archive deletes irreversibly.
run_uninstall_with_archive() {
    assert_destructive_targets_sandboxed || return 1
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        REKOL_ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}" \
        "${COMPONENT_DIR}/uninstall.sh" "$@"
}

# Reads the local-only INDEX_DIR install recorded in the manifest. The index now
# lives in a cache OUTSIDE $REKOL_HOME (under the sandbox HOME's .cache), so the
# tests resolve it from the same path install wrote rather than hard-coding the
# hash. Echoes the path (empty if no manifest / no key).
manifest_index_dir() {
    local f="${REKOLH}/.install-logs/manifest.env" line
    [[ -f "$f" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            INDEX_DIR=*) printf '%s' "${line#INDEX_DIR=}" ;;
        esac
    done < "$f"
}

# ---------------------------------------------------------------------------
# --help / unknown-arg basics
# ---------------------------------------------------------------------------
@test "uninstall --help prints usage and exits 0" {
    run_uninstall --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"uninstall"* ]]
    [[ "$output" == *"--dry-run"* ]]
    [[ "$output" == *"--purge-index"* ]]
}

@test "uninstall rejects an unknown flag" {
    run_uninstall --bogus
    [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Core happy path — full install then uninstall
# ---------------------------------------------------------------------------
@test "uninstall removes shim, skills, venv; preserves user markdown" {
    do_full_install
    # Sanity: the install actually created the things we expect to remove.
    [ -L "${BIN_DIR}/rekol" ] || [ -e "${BIN_DIR}/rekol" ]
    [ -f "${SBHOME}/.claude/skills/rekol/SKILL.md" ]
    [ -f "${SBHOME}/.claude/skills/memory/SKILL.md" ]
    [ -f "${SBHOME}/.claude/skills/rekol-init/SKILL.md" ]
    [ -f "${SBHOME}/.claude/skills/rekol-bootstrap/SKILL.md" ]
    [ -d "${TOOLS_HOME}/.venv" ]

    run_uninstall --yes
    [ "$status" -eq 0 ]

    # Shim, skills, venv home are gone.
    [ ! -e "${BIN_DIR}/rekol" ]
    [ ! -d "${SBHOME}/.claude/skills/rekol" ]
    [ ! -d "${SBHOME}/.claude/skills/memory" ]
    [ ! -d "${SBHOME}/.claude/skills/rekol-init" ]
    [ ! -d "${SBHOME}/.claude/skills/rekol-bootstrap" ]
    [ ! -d "${TOOLS_HOME}" ]

    # User markdown memory is intact.
    [ -f "${REKOLH}/always/identity.md" ]
    [ -f "${REKOLH}/topics/sailing.md" ]
    grep -q "my precious memory" "${REKOLH}/always/identity.md"
    [ -f "${REKOLH}/rekol.config.yaml" ]
}

# ---------------------------------------------------------------------------
# Manifest auto-discovery — uninstall finds a CUSTOM tools-home with NO flags
# ---------------------------------------------------------------------------
# do_full_install installs into the custom ${TOOLS_HOME}/${BIN_DIR}, and the
# default run_uninstall passes NO --tools-home/--bin-dir. uninstall must read the
# manifest install wrote under ${REKOLH}/.install-logs/ to locate that custom
# tools-home — the whole point of the manifest. This is the real-world path.
@test "uninstall (no flags) discovers a custom tools-home via the manifest" {
    do_full_install
    # The manifest install wrote records the custom paths.
    [ -f "${REKOLH}/.install-logs/manifest.env" ]
    grep -q "^TOOLS_HOME=${TOOLS_HOME}$" "${REKOLH}/.install-logs/manifest.env"
    [ -d "${TOOLS_HOME}/.venv" ]

    # No --tools-home/--bin-dir here: discovery must come from the manifest alone.
    run_uninstall --yes
    [ "$status" -eq 0 ]
    [[ "$output" == *"manifest"* ]]

    # The CUSTOM tools-home and shim are gone — proves the manifest was honoured.
    [ ! -d "${TOOLS_HOME}" ]
    [ ! -e "${BIN_DIR}/rekol" ]
    # And no leftover was reported (we confirmed the removal via the manifest).
    [[ "$output" != *"could not confirm"* ]]
}

# ---------------------------------------------------------------------------
# Manifest missing — uninstall removes what it can AND reports the leftover
# ---------------------------------------------------------------------------
# If the manifest is gone (user deleted .install-logs/, or installed before the
# manifest existed) AND the custom tools-home is not at the built-in default,
# uninstall must NOT silently succeed. It removes what it can find (shim, skills,
# hooks via the flagless defaults), and reports the unconfirmed tools-home so the
# user can delete it by hand — never a silent miss.
@test "manifest deleted: uninstall reports the unconfirmed custom tools-home" {
    do_full_install
    # Remove the manifest to simulate a pre-manifest / hand-cleaned install.
    rm -f "${REKOLH}/.install-logs/manifest.env"
    [ ! -f "${REKOLH}/.install-logs/manifest.env" ]
    # The custom tools-home still physically exists on disk.
    [ -d "${TOOLS_HOME}/.venv" ]

    # No flags: with no manifest, uninstall falls back to the built-in default
    # tools-home (~/.local/share/rekol under the sandbox HOME), which is NOT our
    # custom ${TOOLS_HOME} — so it cannot see/remove it and must report it.
    run_uninstall --yes
    [ "$status" -eq 0 ]

    # The custom tools-home is UNTOUCHED (uninstall could not find it)...
    [ -d "${TOOLS_HOME}/.venv" ]
    # ...and the report names a possible leftover so it is never a silent miss.
    [[ "$output" == *"could not confirm"* ]]
    [[ "$output" == *"tools home"* ]]

    # The default-discoverable bits (skills, hooks) were still cleaned.
    [ ! -d "${SBHOME}/.claude/skills/rekol" ]
    run jq -e '[.. | objects | .command? // empty] | any(. | test("rekol"))' \
        "${SBHOME}/.claude/settings.json"
    [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Manifest missing but --tools-home/--bin-dir passed: no false leftover report
# ---------------------------------------------------------------------------
# Explicit flags are the highest-precedence source. Even with no manifest, an
# explicit --tools-home removes the custom dir and reports NO leftover.
@test "manifest deleted: explicit --tools-home still removes the custom dir cleanly" {
    do_full_install
    rm -f "${REKOLH}/.install-logs/manifest.env"
    [ -d "${TOOLS_HOME}/.venv" ]

    run_uninstall_with_flags --yes
    [ "$status" -eq 0 ]
    [ ! -d "${TOOLS_HOME}" ]
    [[ "$output" != *"could not confirm"* ]]
}

@test "uninstall strips every rekol hook from settings.json" {
    do_full_install
    # Pre: rekol hooks are present.
    run jq -e '[.. | objects | .command? // empty] | any(. | test("rekol"))' \
        "${SBHOME}/.claude/settings.json"
    [ "$status" -eq 0 ]

    run_uninstall --yes
    [ "$status" -eq 0 ]

    # No remaining command anywhere references rekol.
    run jq -e '[.. | objects | .command? // empty] | any(. | test("rekol"))' \
        "${SBHOME}/.claude/settings.json"
    [ "$status" -ne 0 ]

    # REKOL_HOME removed from the env block.
    run jq -e '.env.REKOL_HOME' "${SBHOME}/.claude/settings.json"
    [ "$status" -ne 0 ]
}

@test "uninstall removes rekol lines from .zshrc but keeps user lines" {
    do_full_install
    # Pre: install added the rekol PATH + REKOL_HOME export.
    grep -q "# rekol" "${SBHOME}/.zshrc"
    grep -q "^export REKOL_HOME=" "${SBHOME}/.zshrc"

    run_uninstall --yes
    [ "$status" -eq 0 ]

    # rekol lines gone.
    ! grep -q "# rekol" "${SBHOME}/.zshrc"
    ! grep -q "^export REKOL_HOME=" "${SBHOME}/.zshrc"
    ! grep -q "${BIN_DIR}" "${SBHOME}/.zshrc"
    # user lines survive.
    grep -q "export EDITOR=vim" "${SBHOME}/.zshrc"
    grep -q "# my own setup" "${SBHOME}/.zshrc"
}

# ---------------------------------------------------------------------------
# Non-rekol hooks and settings survive
# ---------------------------------------------------------------------------
@test "a non-rekol hook and non-rekol env var survive uninstall" {
    do_full_install
    # Add a user-owned hook + env var to settings.json.
    tmp="${SBHOME}/.claude/settings.json.t"
    jq '.hooks.PostToolUse += [{"matcher":"Bash","hooks":[{"type":"command","command":"echo my-own-hook"}]}]
        | .env.MY_OWN_VAR = "keepme"' \
        "${SBHOME}/.claude/settings.json" > "$tmp" && mv "$tmp" "${SBHOME}/.claude/settings.json"

    run_uninstall --yes
    [ "$status" -eq 0 ]

    run jq -e '[.. | objects | .command? // empty] | any(. == "echo my-own-hook")' \
        "${SBHOME}/.claude/settings.json"
    [ "$status" -eq 0 ]
    run jq -e '.env.MY_OWN_VAR == "keepme"' "${SBHOME}/.claude/settings.json"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Index handling — the index lives in a local cache OUTSIDE $REKOL_HOME
# ---------------------------------------------------------------------------
@test "default uninstall preserves the index cache without --purge-index" {
    do_full_install
    cache="$(manifest_index_dir)"
    [ -n "$cache" ]
    [ -f "${cache}/index.db" ]

    run_uninstall --yes
    [ "$status" -eq 0 ]

    # Without --purge-index, the derived index cache is preserved.
    [ -d "${cache}" ]
}

@test "--purge-index removes the index cache but keeps markdown" {
    do_full_install
    cache="$(manifest_index_dir)"
    [ -n "$cache" ]
    [ -f "${cache}/index.db" ]

    # Never purge a path outside the sandbox (incident 2026-08-17).
    assert_sandboxed "$(manifest_index_dir)" "index dir"
    run_uninstall --yes --purge-index
    [ "$status" -eq 0 ]

    # The local cache is gone...
    [ ! -d "${cache}" ]
    # ...and the markdown memory is untouched.
    [ -f "${REKOLH}/always/identity.md" ]
    grep -q "my precious memory" "${REKOLH}/always/identity.md"
}

# ---------------------------------------------------------------------------
# SECURITY — a fresh install leaves NO derived/secrets-bearing state in
# $REKOL_HOME: no *.db anywhere, and no in-tree .index/.
# ---------------------------------------------------------------------------
@test "fresh install keeps all DBs out of \$REKOL_HOME (security)" {
    do_full_install
    # No SQLite DB of any kind may sit under the (possibly-synced) memory home.
    run find "${REKOLH}" -name '*.db'
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    # And there must be no in-tree .index/ directory at all.
    [ ! -d "${REKOLH}/.index" ]
    # The DBs DO exist — in the local cache outside $REKOL_HOME.
    cache="$(manifest_index_dir)"
    [ -n "$cache" ]
    [ -f "${cache}/index.db" ]
    case "$cache" in
        "${REKOLH}"/*) printf 'cache must be outside REKOL_HOME: %s\n' "$cache" >&2; return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# MIGRATION — an old in-tree $REKOL_HOME/.index/ is rebuilt into the cache and
# the secrets-bearing in-tree copy is deleted; markdown is untouched; search
# works after relocation; uninstall then removes the cache.
# ---------------------------------------------------------------------------
@test "install migrates a legacy in-tree .index/ into the cache and deletes it" {
    # Seed user markdown the install must preserve.
    mkdir -p "${REKOLH}/always"
    printf -- '---\nname: id\ndescription: d\ntype: always\n---\nmy precious memory\n' \
        > "${REKOLH}/always/identity.md"
    # Pre-seed an OLD in-tree index (as a pre-relocation install left it),
    # including a sessions.db that stands in for the secrets-bearing transcript DB.
    mkdir -p "${REKOLH}/.index"
    printf 'legacy-index-db\n'    > "${REKOLH}/.index/index.db"
    printf 'legacy-sessions-db\n' > "${REKOLH}/.index/sessions.db"
    md_hash_before="$(shasum "${REKOLH}/always/identity.md" | awk '{print $1}')"

    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/install.sh" \
        --no-skill --no-shellrc \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"
    [ "$status" -eq 0 ]

    # The legacy in-tree index (and its sessions.db) is GONE from $REKOL_HOME.
    [ ! -d "${REKOLH}/.index" ]
    run find "${REKOLH}" -name '*.db'
    [ "$status" -eq 0 ]
    [ -z "$output" ]

    # It was rebuilt into the cache outside $REKOL_HOME.
    cache="$(manifest_index_dir)"
    [ -n "$cache" ]
    [ -f "${cache}/index.db" ]
    # And it's a freshly built DB, not the moved legacy placeholder file.
    ! grep -q "legacy-index-db" "${cache}/index.db"

    # Markdown is byte-for-byte untouched.
    md_hash_after="$(shasum "${REKOLH}/always/identity.md" | awk '{print $1}')"
    [ "$md_hash_before" = "$md_hash_after" ]

    # Search works after relocation.
    run env -u MEMORY_HOME REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${TOOLS_HOME}/.venv/bin/rekol" search "identity" --top 3
    [ "$status" -eq 0 ]

    # Uninstall removes the relocated cache.
    # Never purge a path outside the sandbox (incident 2026-08-17).
    assert_sandboxed "$(manifest_index_dir)" "index dir"
    run_uninstall --yes --purge-index
    [ "$status" -eq 0 ]
    [ ! -d "${cache}" ]
    # Markdown still preserved through uninstall.
    [ -f "${REKOLH}/always/identity.md" ]
}

# ---------------------------------------------------------------------------
# --dry-run changes nothing
# ---------------------------------------------------------------------------
@test "--dry-run mutates nothing" {
    do_full_install
    settings_before="$(shasum "${SBHOME}/.claude/settings.json" | awk '{print $1}')"
    zshrc_before="$(shasum "${SBHOME}/.zshrc" | awk '{print $1}')"

    run_uninstall --dry-run
    [ "$status" -eq 0 ]

    [ "$settings_before" = "$(shasum "${SBHOME}/.claude/settings.json" | awk '{print $1}')" ]
    [ "$zshrc_before" = "$(shasum "${SBHOME}/.zshrc" | awk '{print $1}')" ]
    # Everything still present.
    [ -e "${BIN_DIR}/rekol" ]
    [ -d "${SBHOME}/.claude/skills/rekol" ]
    [ -d "${TOOLS_HOME}/.venv" ]
    # The local index cache is untouched by a dry-run.
    cache="$(manifest_index_dir)"
    [ -n "$cache" ]
    [ -d "${cache}" ]
}

# ---------------------------------------------------------------------------
# Idempotency — second run is a clean no-op
# ---------------------------------------------------------------------------
@test "uninstall is idempotent (second run is a clean no-op)" {
    do_full_install
    run_uninstall --yes
    [ "$status" -eq 0 ]

    run_uninstall --yes
    [ "$status" -eq 0 ]

    # Still gone; user markdown still present.
    [ ! -e "${BIN_DIR}/rekol" ]
    [ ! -d "${SBHOME}/.claude/skills/rekol" ]
    [ -f "${REKOLH}/always/identity.md" ]
}

# ---------------------------------------------------------------------------
# Partial install tolerance — uninstall against a near-empty machine
# ---------------------------------------------------------------------------
@test "uninstall tolerates a machine with nothing installed" {
    # No install ran. An empty settings.json and no .zshrc.
    printf '{}' > "${SBHOME}/.claude/settings.json"
    run_uninstall --yes
    [ "$status" -eq 0 ]
    # settings.json still valid JSON.
    run jq -e '.' "${SBHOME}/.claude/settings.json"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Backups are written before mutation
# ---------------------------------------------------------------------------
@test "uninstall backs up settings.json and .zshrc before mutating" {
    do_full_install
    run_uninstall --yes
    [ "$status" -eq 0 ]

    # A timestamped .bak of each exists.
    run bash -c "ls '${SBHOME}/.claude/'settings.json.bak-* >/dev/null 2>&1"
    [ "$status" -eq 0 ]
    run bash -c "ls '${SBHOME}/'.zshrc.bak-* >/dev/null 2>&1"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Legacy MEMORY_HOME export removal (only when it points at the rekol home)
# ---------------------------------------------------------------------------
@test "uninstall removes a legacy MEMORY_HOME export pointing at the rekol home" {
    do_full_install
    # Simulate a legacy install that also exported MEMORY_HOME at the same path.
    printf 'export MEMORY_HOME="%s"\n' "${REKOLH}" >> "${SBHOME}/.zshrc"

    run_uninstall --yes
    [ "$status" -eq 0 ]

    ! grep -q "^export MEMORY_HOME=" "${SBHOME}/.zshrc"
}

@test "uninstall keeps a MEMORY_HOME export that points elsewhere" {
    do_full_install
    printf 'export MEMORY_HOME="/some/other/place"\n' >> "${SBHOME}/.zshrc"

    run_uninstall --yes
    [ "$status" -eq 0 ]

    grep -q 'export MEMORY_HOME="/some/other/place"' "${SBHOME}/.zshrc"
}

# ---------------------------------------------------------------------------
# Shim ownership safety — a non-rekol ~/bin/rekol is left alone
# ---------------------------------------------------------------------------
@test "uninstall does not remove a ~/bin/rekol that is not the rekol shim" {
    mkdir -p "${BIN_DIR}"
    printf '#!/bin/sh\necho not rekol\n' > "${BIN_DIR}/rekol"
    chmod +x "${BIN_DIR}/rekol"
    printf '{}' > "${SBHOME}/.claude/settings.json"

    run_uninstall --yes
    [ "$status" -eq 0 ]

    # The unrelated binary survives.
    [ -f "${BIN_DIR}/rekol" ]
    grep -q "not rekol" "${BIN_DIR}/rekol"
}

# ---------------------------------------------------------------------------
# Re-install works from a clean uninstalled state (acceptance criterion)
# ---------------------------------------------------------------------------
@test "install works cleanly after an uninstall" {
    do_full_install
    run_uninstall --yes
    [ "$status" -eq 0 ]

    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"
    [ "$status" -eq 0 ]
    [ -L "${BIN_DIR}/rekol" ] || [ -e "${BIN_DIR}/rekol" ]
    run jq -e '[.. | objects | .command? // empty] | any(. | test("rekol"))' \
        "${SBHOME}/.claude/settings.json"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Phase 8 — the durable transcript archive is preserved by default
# ---------------------------------------------------------------------------
# Like the index cache: --yes alone keeps it; only --purge-archive (or an
# interactive prompt) removes it. The archive holds verbatim transcripts and
# there is no export in v1, so deletion is irreversible — never silent. The
# fake archive dir is pointed at via REKOL_ARCHIVE_DIR so uninstall resolves it.
@test "uninstall preserves the archive by default" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"
    run_uninstall_with_archive --yes
    [ "$status" -eq 0 ]
    # --yes must NOT purge the archive (mirrors --yes keeping the index).
    [ -f "${REKOL_ARCHIVE_DIR}/projA/s.jsonl" ]
}

# The archive inside the tools home is the DEFAULT layout, not an edge case:
# TOOLS_HOME defaults to ~/.local/share/rekol and the archive to
# ${XDG_DATA_HOME:-~/.local/share}/rekol/archive. Step 3 used to `rm -rf` the
# tools home wholesale, taking the archive with it — no --purge-archive, no
# prompt, nothing in the summary, while --help promised it was preserved. That
# destroyed 759 archive-only sessions on a real machine (2026-08-18).
# The venv must still go; only the archive is spared.
@test "default uninstall preserves an archive that lives INSIDE the tools home" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TOOLS_HOME}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"
    [ -d "${TOOLS_HOME}/.venv" ]

    run_uninstall_with_archive --yes
    [ "$status" -eq 0 ]

    # The archive survives a default uninstall, as documented.
    [ -f "${REKOL_ARCHIVE_DIR}/projA/s.jsonl" ]
    # ...and the rest of the tools home is still removed.
    [ ! -d "${TOOLS_HOME}/.venv" ]
}

# --purge-archive must still work when the archive is inside the tools home:
# preserving it in Step 3 must not make it unreachable to the explicit request.
@test "--purge-archive still removes an archive inside the tools home" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TOOLS_HOME}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"

    run_uninstall_with_archive --yes --purge-archive
    [ "$status" -eq 0 ]
    [ ! -d "${REKOL_ARCHIVE_DIR}" ]
}

@test "uninstall --purge-archive removes the archive" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"
    run_uninstall_with_archive --yes --purge-archive
    [ "$status" -eq 0 ]
    [ ! -d "${REKOL_ARCHIVE_DIR}" ]
}

@test "uninstall --purge-archive still preserves user markdown" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"
    run_uninstall_with_archive --yes --purge-archive
    [ "$status" -eq 0 ]
    [ ! -d "${REKOL_ARCHIVE_DIR}" ]
    # Markdown under $REKOL_HOME is never touched, even with --purge-archive.
    [ -f "${REKOLH}/always/identity.md" ]
    grep -q "my precious memory" "${REKOLH}/always/identity.md"
}

# SECURITY: a misconfigured REKOL_ARCHIVE_DIR that EQUALS $REKOL_HOME would make
# `rm -rf "${ARCHIVE_DIR}"` delete the entire markdown memory home. --purge-archive
# must REFUSE to purge when the resolved archive overlaps the resolved home, and
# report it as a leftover instead. Markdown must survive.
@test "uninstall --purge-archive refuses when archive equals REKOL_HOME" {
    do_full_install
    # Point the archive AT the markdown home itself (the worst overlap).
    export REKOL_ARCHIVE_DIR="${REKOLH}"
    run_uninstall_with_archive --yes --purge-archive
    [ "$status" -eq 0 ]
    # The markdown home (and its contents) MUST still exist.
    [ -d "${REKOLH}" ]
    [ -f "${REKOLH}/always/identity.md" ]
    grep -q "my precious memory" "${REKOLH}/always/identity.md"
}

# A second overlap shape: the archive sits INSIDE the markdown home, so
# `rm -rf` would delete a subtree of the user's markdown. Same refusal.
@test "uninstall --purge-archive refuses when archive is inside REKOL_HOME" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${REKOLH}/topics"
    run_uninstall_with_archive --yes --purge-archive
    [ "$status" -eq 0 ]
    # The markdown subtree under the home must survive (never rm'd).
    [ -f "${REKOLH}/topics/sailing.md" ]
    [ -f "${REKOLH}/always/identity.md" ]
}

# ------------- the remover's scope must match the detector's (#171 A6) --------
# `strip_event` covered SessionStart/PostToolUse/SessionEnd/UserPromptSubmit/Stop
# but NOT StopFailure — which is exactly where `rekol resume enable` registers. So
# uninstall reported "stripped rekol hooks" while leaving a hook wired to a venv
# it had just deleted. Worse, the HAS_REKOL gate walks EVERY event, so it saw the
# survivor and each subsequent uninstall re-backed-up, re-claimed removal, and
# stripped nothing: a detector wider than its remover can never confirm its claim.
@test "uninstall strips StopFailure and every rekol env key, preserving others" {
  SBHOME="$TESTROOT/sandhome-scope"
  mkdir -p "$SBHOME/.claude"
  cat > "$SBHOME/.claude/settings.json" <<'JSON'
{
  "hooks": {
    "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "rekol _hook session-tasks"}]}],
    "StopFailure": [{"matcher": "", "hooks": [{"type": "command", "command": "rekol _hook stop-failure-record"}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "other-tool run"}]}]
  },
  "env": {"REKOL_HOME": "/x", "REKOL_TOOLS_HOME": "/y", "REKOL_INDEX_DIR": "/i",
          "REKOL_ARCHIVE_DIR": "/a", "MEMORY_HOME": "/legacy", "KEEP_ME": "1"}
}
JSON
  REKOLH="$TESTROOT/rekolhome-scope"
  mkdir -p "$REKOLH"

  assert_destructive_targets_sandboxed
  run env -u MEMORY_HOME REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/uninstall.sh" --yes --tools-home "$TESTROOT/nope" --bin-dir "$TESTROOT/nobin"
  [ "$status" -eq 0 ]

  # No rekol hook survives, in ANY event.
  left="$(jq -r '[.hooks // {} | .. | objects | .command? // empty] | map(select(test("rekol"))) | length' \
          "$SBHOME/.claude/settings.json")"
  [ "$left" -eq 0 ] || { echo "$left rekol hook(s) survived"; jq '.hooks' "$SBHOME/.claude/settings.json"; false; }

  # EVERY rekol-namespace env key gone (asserted by prefix, not by an enumerated
  # list — a list here would pass while a newly-added key leaked, which is the
  # exact drift this predicate exists to prevent). The user's own key kept.
  run jq -e '(.env // {} | keys
              | map(startswith("REKOL_") or . == "MEMORY_HOME" or . == "MEMORY_TOOLS_HOME")
              | any) | not' "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]
  run jq -e '.env.KEEP_ME == "1"' "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]

  # The other tool's hook is untouched.
  run jq -e '.hooks.Stop[0].hooks[0].command == "other-tool run"' "$SBHOME/.claude/settings.json"
  [ "$status" -eq 0 ]

  # And the claim is now confirmable: a SECOND uninstall must find nothing to strip.
  assert_destructive_targets_sandboxed
  run env -u MEMORY_HOME REKOL_HOME="$REKOLH" HOME="$SBHOME" \
    "$COMPONENT_DIR/uninstall.sh" --yes --tools-home "$TESTROOT/nope" --bin-dir "$TESTROOT/nobin"
  [ "$status" -eq 0 ]
  [[ "$output" == *"nothing to strip"* ]] || { echo "second run did not report a clean no-op"; echo "$output" | grep -i strip; false; }
}

# ---------------------------------------------------------------------------
# Orphaned `rekol` console scripts on other interpreters (#203)
# ---------------------------------------------------------------------------
# A pip install through a DIFFERENT interpreter leaves its own console script
# behind. Step 1 only considers ${BIN_DIR}/rekol, so those survive an uninstall
# entirely — and because an interactive shell resolves the working wrapper
# first, `command -v rekol` stays green while a hook (different PATH order)
# resolves the orphan and starts a session with no memory.
#
# HERMETIC AND STATIC. The uninstaller never executes a program it found on
# PATH, so these fixtures are inert files, and PATH is built from fixture
# directories plus a minimal system set rather than inheriting $PATH — an
# earlier version appended $PATH and so inspected (and, before the rework,
# executed) the real `rekol` on this machine.

# A PATH with the tools uninstall.sh needs and nothing of the developer's.
#
# The fixed system directories are asserted to hold no `rekol` before being used,
# so a machine that happens to have one installed in /usr/bin cannot make these
# tests inspect it. Without that assertion "hermetic" would be an assumption
# about the host rather than a property of the test.
_hermetic_path() {
    for _sysdir in /usr/bin /bin /usr/sbin /sbin; do
        if [ -e "$_sysdir/rekol" ]; then
            echo "refusing to run: $_sysdir/rekol exists, so PATH would not be hermetic" >&2
            return 1
        fi
    done
    printf '%s:/usr/bin:/bin:/usr/sbin:/sbin' "$1"
}

@test "uninstall names a stale rekol whose shebang interpreter is gone" {
    orphan_dir="$TESTROOT/other-interpreter/bin"
    mkdir -p "$orphan_dir"
    # Exact evidence, no execution: the interpreter in the shebang does not exist.
    printf '#!%s/deleted/bin/python3.11
from rekol.cli import main
' "$TESTROOT"         > "$orphan_dir/rekol"
    chmod 755 "$orphan_dir/rekol"
    assert_destructive_targets_sandboxed

    run env -u MEMORY_HOME -u TEST_MODE -u REKOL_INDEX_DIR         PATH="$(_hermetic_path "$orphan_dir")"         REKOL_HOME="${REKOLH}" HOME="${SBHOME}"         "${COMPONENT_DIR}/uninstall.sh" --dry-run         --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"

    [ "$status" -eq 0 ]
    [[ "$output" == *"stale 'rekol' on PATH"* ]]
    [[ "$output" == *"$orphan_dir/rekol"* ]]
    [[ "$output" == *"no longer exists"* ]]
}

@test "uninstall never advises removing a bash-shebang rekol shim" {
    # The shipped shim is `#!/usr/bin/env bash`. bash has no site-packages, so a
    # naive "absent means orphan" rule fires on a working shim and tells the user
    # to delete it. This asserts that cannot happen.
    shim_dir="$TESTROOT/foreign-shim/bin"
    mkdir -p "$shim_dir"
    printf '#!/usr/bin/env bash
exec rekol-real "$@"
' > "$shim_dir/rekol"
    chmod 755 "$shim_dir/rekol"
    assert_destructive_targets_sandboxed

    run env -u MEMORY_HOME -u TEST_MODE -u REKOL_INDEX_DIR         PATH="$(_hermetic_path "$shim_dir")"         REKOL_HOME="${REKOLH}" HOME="${SBHOME}"         "${COMPONENT_DIR}/uninstall.sh" --dry-run         --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"

    [ "$status" -eq 0 ]
    [[ "$output" != *"stale 'rekol' on PATH"* ]]
    [[ "$output" != *"orphaned console script"* ]]
}

@test "uninstall reports what it actually checked when nothing on PATH is stale" {
    empty_dir="$TESTROOT/no-rekol/bin"
    mkdir -p "$empty_dir"

    assert_destructive_targets_sandboxed
    run env -u MEMORY_HOME -u TEST_MODE -u REKOL_INDEX_DIR \
        PATH="$(_hermetic_path "$empty_dir")" \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/uninstall.sh" --dry-run \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"

    [ "$status" -eq 0 ]
    [[ "$output" == *"no other 'rekol' found on PATH"* ]]
}

@test "uninstall does not claim all-clear when a rekol on PATH was left undecided" {
    # A script whose interpreter EXISTS but may or may not be able to import
    # rekol. The shebang-only pass cannot tell, so it must say so and point at
    # doctor — not print a reassuring all-clear. A line nothing proved is the
    # exact shape of the defect this whole check exists for.
    undecided_dir="$TESTROOT/undecided/bin"
    mkdir -p "$undecided_dir"
    # A fixture-local interpreter, so this does not depend on the host having
    # /usr/bin/python3 — if that were absent the script would classify as an
    # orphan and the test would pass for the wrong reason.
    fake_interp="$TESTROOT/undecided/python3.11"
    printf '#!/bin/sh\nexit 0\n' > "$fake_interp"
    chmod 755 "$fake_interp"
    printf '#!%s\nfrom rekol.cli import main\n' "$fake_interp" > "$undecided_dir/rekol"
    chmod 755 "$undecided_dir/rekol"

    assert_destructive_targets_sandboxed
    run env -u MEMORY_HOME -u TEST_MODE -u REKOL_INDEX_DIR \
        PATH="$(_hermetic_path "$undecided_dir")" \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/uninstall.sh" --dry-run \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}"

    [ "$status" -eq 0 ]
    [[ "$output" != *"no other 'rekol' found on PATH"* ]]
    [[ "$output" == *"not assessed here"* ]]
    [[ "$output" == *"rekol doctor"* ]]
}

# ---------------------------------------------------------------------------
# The guard itself (#223)
# ---------------------------------------------------------------------------
# These exist because the failure they protect against is SILENT. QA measured
# that deleting setup()'s one `unset` line makes this suite delete the real
# tools home — venv and hooks included — and the suite still reports pass. A
# guard verified only by "the suite is green" would be indistinguishable from no
# guard at all, which is the same reports-healthy-while-dead shape as #203 and
# #143.
#
# Each runs the guard in a SUBSHELL and never invokes the uninstaller: the point
# is that the refusal happens before anything destructive is reached.

_guard_in_subshell() {
    # $1 = shell code to run after sourcing the guard with this run's TESTROOT.
    run bash -c "
        TESTROOT=$(printf '%q' "$TESTROOT")
        source $(printf '%q' "${BATS_TEST_DIRNAME}/sandbox_guard.bash")
        $1
    "
}

@test "guard REFUSES when a cleared override survived, and names the variable" {
    # Exactly the shape QA measured: setup()'s clearing is gone, so the
    # inherited value still points at the live install.
    _guard_in_subshell '
        export REKOL_TOOLS_HOME="$HOME/.local/share/rekol"
        rekol_bats_require_sandboxed_env
    '
    [ "$status" -ne 0 ]
    [[ "$output" == *"REFUSING TO RUN"* ]] \
        || { echo "no refusal printed; got: $output"; false; }
    # Naming the variable is the whole remedy — a refusal that does not say
    # WHICH override leaked leaves the reader to guess.
    [[ "$output" == *"REKOL_TOOLS_HOME"* ]] \
        || { echo "refusal did not name the variable: $output"; false; }
}

@test "guard REFUSES a leaked index dir, the variable that caused the 2026-08 loss" {
    _guard_in_subshell '
        export REKOL_INDEX_DIR="$HOME/.cache/rekol/index"
        rekol_bats_require_sandboxed_env
    '
    [ "$status" -ne 0 ]
    [[ "$output" == *"REKOL_INDEX_DIR"* ]]
}

@test "guard reports EVERY leaked override, not just the first" {
    # A guard that stops at the first offender teaches you to fix one variable,
    # re-run, and meet the next one.
    _guard_in_subshell '
        export REKOL_TOOLS_HOME="$HOME/.local/share/rekol"
        export REKOL_INDEX_DIR="$HOME/.cache/rekol/index"
        rekol_bats_require_sandboxed_env
    '
    [ "$status" -ne 0 ]
    [[ "$output" == *"REKOL_TOOLS_HOME"* ]]
    [[ "$output" == *"REKOL_INDEX_DIR"* ]]
}

@test "guard PERMITS a sandbox-internal override (the archive tests rely on it)" {
    # The negative control. Without this, a guard that refused unconditionally
    # would pass every test above while making the suite unrunnable.
    _guard_in_subshell '
        export REKOL_ARCHIVE_DIR="$TESTROOT/archive"
        rekol_bats_require_sandboxed_env
    '
    [ "$status" -eq 0 ] \
        || { echo "guard refused a sandbox path: $output"; false; }
    [[ "$output" != *"REFUSING"* ]]
}

@test "guard PERMITS a cleared environment, so an ordinary run prints nothing" {
    # The other half of the control: on a healthy machine the guard must be
    # silent, or it gets deleted for crying wolf.
    _guard_in_subshell 'rekol_bats_clear_path_overrides; rekol_bats_require_sandboxed_env'
    [ "$status" -eq 0 ]
    [ -z "$output" ] || { echo "guard was not silent: $output"; false; }
}

@test "assert_sandboxed no longer accepts a bare /tmp path" {
    # The old copies allowed a blanket /tmp/* and /private/tmp/*, which reached
    # any other suite's sandbox or anything parked there. TESTROOT already
    # covers mktemp's output on both platforms, so the allowance bought nothing.
    _guard_in_subshell 'assert_sandboxed /tmp/somebody-elses-sandbox "index dir"'
    [ "$status" -ne 0 ]
    [[ "$output" == *"OUTSIDE the sandbox"* ]]
}

@test "assert_sandboxed accepts this run's TESTROOT and bats' own tmpdirs" {
    _guard_in_subshell 'assert_sandboxed "$TESTROOT/tools" "tools home"'
    [ "$status" -eq 0 ]
    _guard_in_subshell "BATS_TEST_TMPDIR=$(printf '%q' "${BATS_TEST_TMPDIR}")
        assert_sandboxed \"\$BATS_TEST_TMPDIR/x\" 'tools home'"
    [ "$status" -eq 0 ]
}

@test "assert_sandboxed treats an empty value as nothing-to-delete, not as /" {
    # manifest_index_dir echoes nothing when there is no manifest; that must not
    # read as a path outside the sandbox, or every pre-install test fails.
    _guard_in_subshell 'assert_sandboxed "" "index dir"'
    [ "$status" -eq 0 ]
}

@test "setup() still clears the path overrides" {
    # QA's exact scenario was the deletion of this one call. The refusal at each
    # destructive site already catches it, but only on a machine that HAS a
    # leaked override -- in clean CI the suite would stay green while the
    # protection was gone. So assert the call itself is present: this fails on
    # the deletion everywhere, including CI.
    run grep -c '^\s*rekol_bats_clear_path_overrides\s*$' \
        "${BATS_TEST_DIRNAME}/test_uninstall.bats"
    [ "$status" -eq 0 ] \
        || { echo "setup() no longer clears the path overrides (#223)"; false; }
    [ "$output" -ge 1 ]
}

@test "every uninstall.sh invocation in this file is guarded" {
    # The property, asserted against the file itself: adding a 21st destructive
    # test must not be able to skip the check. Every line that invokes the
    # uninstaller must have assert_destructive_targets_sandboxed within the few
    # lines above it -- either because it sits in one of the run_uninstall*
    # helpers, or because the test calls the assertion directly.
    #
    # This is the check that survives a refactor. The individual guard calls can
    # each be deleted without any other test noticing; this one notices.
    run python3 "${BATS_TEST_DIRNAME}/assert_invocations_guarded.py" \
        "${BATS_TEST_DIRNAME}/test_uninstall.bats"
    echo "$output"
    [ "$status" -eq 0 ] \
        || { echo "an uninstall.sh invocation is not preceded by the guard"; false; }
}
