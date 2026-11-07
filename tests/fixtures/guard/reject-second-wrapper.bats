# A SECOND invocation site, correctly guarded. The previous rule allowed this,
# which is how the suite came to have six of them and six copies of the guard --
# six places one line could be deleted while the checker stayed happy.
rekol_bats_run_destructive() {
    assert_destructive_targets_sandboxed || return 1
    local script="$1"; shift
    run env HOME="$SBHOME" "${COMPONENT_DIR}/${script}" "$@"
}

run_uninstall_with_flags() {
    assert_destructive_targets_sandboxed || return 1
    run env HOME="$SBHOME" "${COMPONENT_DIR}/uninstall.sh" --tools-home "$1"
}
