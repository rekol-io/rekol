# The one wrapper, in the one required form.
rekol_bats_run_destructive() {
    assert_destructive_targets_sandboxed || return 1
    local script="$1"; shift
    run env HOME="$SBHOME" "${COMPONENT_DIR}/${script}" "$@"
}

run_uninstall() {
    rekol_bats_run_destructive uninstall.sh -- "$@"
}

@test "something" {
    run_uninstall --dry-run
    [ "$status" -eq 0 ]
}
