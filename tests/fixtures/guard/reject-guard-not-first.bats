# The guard is present, but not the OPENING statement. "Somewhere above the
# invocation" is not a position: a guard after a `local` is harmless, a guard
# after an `rm` is not, and the checker cannot tell those apart.
rekol_bats_run_destructive() {
    local script="$1"; shift
    assert_destructive_targets_sandboxed || return 1
    run env HOME="$SBHOME" "${COMPONENT_DIR}/${script}" "$@"
}
