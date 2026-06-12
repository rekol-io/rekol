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

setup() {
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
    printf 'embedding_model: test-hashing\nsession_search_enabled: false\ngit_track: false\n' \
        > "${REKOLH}/rekol.config.yaml"

    # Pin the login shell so install/uninstall target .zshrc deterministically
    # (the host CI may run bash; the bash path is covered in test_install.bats).
    SHELL="/bin/zsh"
    export COMPONENT_DIR TOOLS_HOME BIN_DIR REKOLH SBHOME TESTROOT SHELL
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
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/uninstall.sh" "$@"
}

# Explicit-flags variant for the few tests that must pin the paths directly
# (e.g. asserting the flag-precedence path, or running against a machine that
# never had an install/manifest).
run_uninstall_with_flags() {
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" \
        "${COMPONENT_DIR}/uninstall.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}" "$@"
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
    [ -f "${SBHOME}/.claude/skills/rekol/skill.md" ]
    [ -f "${SBHOME}/.claude/skills/memory/skill.md" ]
    [ -f "${SBHOME}/.claude/skills/rekol-init/skill.md" ]
    [ -f "${SBHOME}/.claude/skills/rekol-bootstrap/skill.md" ]
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
    settings_before="$(md5 -q "${SBHOME}/.claude/settings.json")"
    zshrc_before="$(md5 -q "${SBHOME}/.zshrc")"

    run_uninstall --dry-run
    [ "$status" -eq 0 ]

    [ "$settings_before" = "$(md5 -q "${SBHOME}/.claude/settings.json")" ]
    [ "$zshrc_before" = "$(md5 -q "${SBHOME}/.zshrc")" ]
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
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" REKOL_ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}" \
        "${COMPONENT_DIR}/uninstall.sh" --yes
    [ "$status" -eq 0 ]
    # --yes must NOT purge the archive (mirrors --yes keeping the index).
    [ -f "${REKOL_ARCHIVE_DIR}/projA/s.jsonl" ]
}

@test "uninstall --purge-archive removes the archive" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" REKOL_ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}" \
        "${COMPONENT_DIR}/uninstall.sh" --yes --purge-archive
    [ "$status" -eq 0 ]
    [ ! -d "${REKOL_ARCHIVE_DIR}" ]
}

@test "uninstall --purge-archive still preserves user markdown" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" REKOL_ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}" \
        "${COMPONENT_DIR}/uninstall.sh" --yes --purge-archive
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
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" REKOL_ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}" \
        "${COMPONENT_DIR}/uninstall.sh" --yes --purge-archive
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
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" REKOL_ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}" \
        "${COMPONENT_DIR}/uninstall.sh" --yes --purge-archive
    [ "$status" -eq 0 ]
    # The markdown subtree under the home must survive (never rm'd).
    [ -f "${REKOLH}/topics/sailing.md" ]
    [ -f "${REKOLH}/always/identity.md" ]
}
