# Shared sandbox guard for the bats suites (#223).
#
# WHY THIS FILE EXISTS
# -------------------
# The bats suites run `uninstall.sh`, which legitimately deletes whatever it is
# pointed at. They protected themselves by unsetting the environment overrides
# in setup(). QA measured (2026-11-06) that removing that one `unset` line makes
# the suite delete the developer's REAL tools home -- venv and hooks included --
# because an inherited REKOL_TOOLS_HOME points the uninstaller at the live
# install. Every rekol hook on the machine then fails.
#
# Two guards existed and neither stopped it. Traced: `assert_sandboxed` was
# called at 2 of 20 destructive sites, and both calls passed the *index dir* --
# nothing ever asked it about the tools home. The guard was not defeated, it was
# never consulted about the path that got deleted.
#
# So the safety margin was one `unset` statement continuing to exist, in a file
# that gets reformatted and rebased like any other, and a reviewer reading a
# diff has no reason to notice its deletion. A test suite is the worst place to
# rely on a remembered convention: it runs unattended, on machines with real
# data.
#
# WHERE THE CHECK HAS TO SIT, AND WHY NOT IN setup()
# --------------------------------------------------
# Refusing in setup() when an override is merely *set* looks stronger and is
# useless: REKOL_INDEX_DIR is exported into every Claude Code session (#164) and
# REKOL_HOME from the developer's shell rc, so the suite would refuse to start
# on an ordinary machine. A guard that cries wolf on every run gets deleted,
# which leaves things worse than before.
#
# So the two halves are split:
#   setup() CLEARS the overrides (rekol_bats_clear_path_overrides), as before;
#   every destructive call REFUSES if one is still set to an outside path
#   (rekol_bats_require_sandboxed_env).
#
# In the healthy case the clearing has happened, the destructive call sees them
# unset, and nothing is printed. If the clearing is ever lost -- deleted in a
# rebase, reordered below a destructive call, dropped by a reformat -- the
# inherited value is still there when the destructive call checks, so the suite
# stops with the offending variable named instead of deleting the live install.
# That is the inversion worth having: the check fires exactly when the
# convention has been forgotten, and never otherwise.
#
# `tests/conftest.py` has done the equivalent for pytest since the 2026-08-17
# incident, when a test resolved the developer's real index and deleted it
# (~36k indexed messages). The bats suites never got it, and hardening one
# harness is what made the gap in the other dangerous.

# Every variable that can redirect install.sh/uninstall.sh at a path of its own
# choosing. REKOL_INDEX_DIR is first for a reason: it is resolution order #1 and
# is used verbatim, overriding the XDG sandboxing the suites otherwise rely on.
REKOL_BATS_PATH_OVERRIDES=(
    REKOL_INDEX_DIR
    REKOL_ARCHIVE_DIR
    REKOL_TOOLS_HOME
    MEMORY_TOOLS_HOME
    REKOL_HOME
    MEMORY_HOME
    CLAUDE_CONFIG_DIR
    CLAUDE_SETTINGS_PATH
    XDG_CACHE_HOME
    XDG_DATA_HOME
)

# True when $1 lies inside this run's sandbox.
#
# The allowlist is this run's TESTROOT plus bats' own temp dirs, and nothing
# else. Note what is deliberately NOT here: a blanket `/tmp/*` and
# `/private/tmp/*`, which the previous copies allowed. `mktemp -d` already lands
# under /tmp on Linux and /var/folders on macOS, so both were matched by the
# TESTROOT branch anyway -- they added no reach while widening the allowlist to
# any path under /tmp, including another suite's sandbox or a real thing someone
# parked there.
#
# /private is accepted as an alternative prefix because macOS reaches /tmp and
# /var/folders through a symlink, so the same directory arrives in either form
# depending on whether a tool resolved it.
#
# CONTAINMENT, NOT SPELLING (Codex, 2026-11-06). The first version compared
# strings, so `$TESTROOT/../../outside` passed the check: it carries the right
# prefix and resolves somewhere else entirely. Both sides are now canonicalized
# first, and the comparison requires a component boundary, so a sibling named
# `${TESTROOT}-evil` cannot pass as inside `${TESTROOT}` either.
# True when the path contains a `..` component. Checked BEFORE resolution, and
# refused outright (Codex, 2026-11-06).
#
# Canonicalisation already resolves `..`, so containment catches an escape. This
# is a stronger rule for a different reason: resolution reads the filesystem, so
# its answer is only true for the instant it was asked. A `..` traversal passing
# through a symlink that is replaced between the check and the delete is a real
# TOCTOU, and no destructive target in these suites has any business containing
# `..` in the first place. Refusing the SPELLING needs no filesystem at all, so
# there is no window.
#
# Matched on the component, not the substring: a directory legitimately named
# `..foo` or `foo..bar` is not a traversal.
rekol_bats_path_has_parent_component() {
    local padded="/$1/"
    [[ "$padded" == */../* ]]
}

rekol_bats_canonical_path() {
    # Resolves `.`, `..` and symlinks. The path need NOT exist yet: the deepest
    # existing ancestor is resolved with `pwd -P` and the remainder appended,
    # which is what a destructive call against a not-yet-created path needs.
    local path="$1" existing trailing="" resolved parent target hops=0
    [[ -n "$path" ]] || return 1
    [[ "$path" == /* ]] || path="${PWD}/${path}"
    existing="$path"
    while true; do
        # Climb to the deepest component that is there. `-L` as well as `-e`, so
        # a DANGLING symlink stops the climb and gets resolved below rather than
        # being treated as a missing name.
        while [[ ! -e "$existing" && ! -L "$existing" ]]; do
            trailing="$(basename -- "$existing")${trailing:+/${trailing}}"
            parent="$(dirname -- "$existing")"
            [[ "$parent" != "$existing" ]] || return 1
            existing="$parent"
        done
        # RESOLVE A SYMLINKED LEAF (Codex, 2026-11-07). `cd -P` resolves every
        # component of a DIRECTORY path, so an escaping directory symlink was
        # caught -- but the final component was never resolved at all. So
        # `$TESTROOT/external-link.json`, a link to a file outside the sandbox,
        # passed: the name is inside, and only the name was ever examined. The
        # destructive call then writes or deletes the target.
        [[ -L "$existing" ]] || break
        hops=$(( hops + 1 ))
        # Fail closed on a loop or an absurd chain rather than guessing. There is
        # no readlink -f on macOS bash 3.2, so this walks the chain by hand.
        [[ "$hops" -le 40 ]] || return 1
        target="$(readlink -- "$existing")" || return 1
        if [[ "$target" == /* ]]; then
            existing="$target"
        else
            existing="$(dirname -- "$existing")/${target}"
        fi
    done
    # AN EXISTING FILE IS NOT A DIRECTORY TO cd INTO (Codex, 2026-11-06). The
    # climb above stops as soon as the path exists, so a target that is a regular
    # file -- `$REKOL_HOME/REKOL.md`, a manifest, a recorded binary -- left
    # `existing` pointing at the file, `cd` failed, and this returned 1. The
    # caller reads that as "cannot canonicalise", which `assert_sandboxed`
    # reports as OUTSIDE the sandbox: a false REFUSAL on a perfectly contained
    # file. Resolve through the containing directory instead.
    if [[ ! -d "$existing" ]]; then
        trailing="$(basename -- "$existing")${trailing:+/${trailing}}"
        existing="$(dirname -- "$existing")"
    fi
    resolved="$(cd -- "$existing" 2>/dev/null && pwd -P)" || return 1
    printf '%s' "${resolved}${trailing:+/${trailing}}"
}

rekol_bats_path_is_sandboxed() {
    local candidate="$1" root canonical_candidate canonical_root
    [[ -n "$candidate" ]] || return 1
    canonical_candidate="$(rekol_bats_canonical_path "$candidate")" || return 1
    for root in "${TESTROOT:-}" "${BATS_TEST_TMPDIR:-}" \
                "${BATS_FILE_TMPDIR:-}" "${BATS_RUN_TMPDIR:-}"; do
        [[ -n "$root" ]] || continue
        canonical_root="$(rekol_bats_canonical_path "$root")" || continue
        # Equal, or genuinely inside: the trailing slash is the component
        # boundary that stops `${root}-evil` matching.
        if [[ "$canonical_candidate" == "$canonical_root" ]] ||
           [[ "$canonical_candidate" == "$canonical_root"/* ]]; then
            return 0
        fi
    done
    return 1
}

# Clears every path override so only the sandboxed paths decide. Call FIRST in
# setup(), before anything derives a path. Safe to call when they are already
# unset.
rekol_bats_clear_path_overrides() {
    unset "${REKOL_BATS_PATH_OVERRIDES[@]}" 2>/dev/null || true
}

# Refuses to let a destructive test run against a path outside the sandbox.
# Call before anything that deletes. Returns non-zero so bats aborts the test.
assert_sandboxed() {
    local resolved="$1" label="$2"
    # An empty value means "nothing resolved", not "resolved to /" -- the caller
    # has nothing to delete, so there is nothing to refuse.
    [[ -n "$resolved" ]] || return 0
    # FIRST, before resolving anything: a `..` component is refused on sight.
    if rekol_bats_path_has_parent_component "$resolved"; then
        printf 'REFUSING: %s contains a `..` component, which is never a\n' "$label" >&2
        printf '  legitimate destructive target here and cannot be checked\n' >&2
        printf '  without reading the filesystem: %s\n' "$resolved" >&2
        return 1
    fi
    if rekol_bats_path_is_sandboxed "$resolved"; then
        return 0
    fi
    printf 'REFUSING: %s resolved OUTSIDE the sandbox: %s\n' "$label" "$resolved" >&2
    printf '  TESTROOT=%s\n' "${TESTROOT:-<unset>}" >&2
    return 1
}

# The fail-closed half. Refuses when any override is still set to a path outside
# the sandbox -- i.e. when setup()'s clearing has been lost. Returns non-zero so
# bats aborts the test instead of running it unsandboxed.
rekol_bats_require_sandboxed_env() {
    local var value offenders=0
    for var in "${REKOL_BATS_PATH_OVERRIDES[@]}"; do
        eval "value=\${$var:-}"
        [[ -n "$value" ]] || continue
        # A `..` component is refused here too, before resolution, for the same
        # reason: the override decides where a delete lands.
        if ! rekol_bats_path_has_parent_component "$value"; then
            # A sandbox-internal value is legitimate: a test may point an
            # override at its own TESTROOT on purpose (the archive tests do
            # exactly that).
            rekol_bats_path_is_sandboxed "$value" && continue
        fi
        if [[ "$offenders" -eq 0 ]]; then
            printf '\n' >&2
            printf 'REFUSING TO RUN: this suite deletes what it is pointed at, and\n' >&2
            printf 'an environment override points OUTSIDE its sandbox:\n' >&2
        fi
        printf '  %s=%s\n' "$var" "$value" >&2
        offenders=$(( offenders + 1 ))
    done
    if [[ "$offenders" -gt 0 ]]; then
        printf '\n' >&2
        printf 'This means setup() no longer clears that variable. Running anyway\n' >&2
        printf 'could delete the real install -- QA measured exactly that: the\n' >&2
        printf 'tools home, venv and hooks included.\n' >&2
        printf 'Fix: restore the rekol_bats_clear_path_overrides call in setup(),\n' >&2
        printf 'or run the suite in a container.\n' >&2
        printf '  TESTROOT=%s\n' "${TESTROOT:-<unset>}" >&2
        return 1
    fi
    return 0
}
