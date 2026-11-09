# REJECT: a valid wrapper AND one @test that bypasses it via a split variable.
#
# The MIXED case, which `reject-split-variable-invocation.bats` does not cover.
# That fixture has no guarded invocation at all, so the anti-vacuity rule catches
# it — which means it proved the checker notices an empty inventory, not that it
# notices a bypass. A review (2026-11-08) found this shape reporting
# `guard violations: 0`: `COMPONENT_DIR` never appears on the `run` line, so the
# "prose, not an invocation" skip dropped it while the wrapper above satisfied
# every other rule.
#
# The checker keys on the RUNNER now, not on how the path was spelled.

rekol_bats_run_destructive() {
    assert_destructive_targets_sandboxed || return 1
    local script="$1"; shift
    run "${COMPONENT_DIR}/${script}" "$@"
}

@test "a properly guarded call, so the inventory is not empty" {
    rekol_bats_run_destructive uninstall.sh --yes
}

@test "the bypass: the path is built one line earlier" {
    d="$COMPONENT_DIR"
    run "$d/uninstall.sh" --yes
}
