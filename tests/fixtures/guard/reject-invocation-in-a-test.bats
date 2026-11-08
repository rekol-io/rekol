# No wrapper can stand in front of an invocation written inline.
rekol_bats_run_destructive() {
    assert_destructive_targets_sandboxed || return 1
    local script="$1"; shift
    run env HOME="$SBHOME" "${COMPONENT_DIR}/${script}" "$@"
}

@test "sneaks past" {
    run env HOME="$SBHOME" "${COMPONENT_DIR}/uninstall.sh" --purge-archive
}
