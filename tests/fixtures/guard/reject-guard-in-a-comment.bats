# The original defect: a proximity check satisfied by prose.
rekol_bats_run_destructive() {
    # assert_destructive_targets_sandboxed
    local script="$1"; shift
    run env HOME="$SBHOME" "${COMPONENT_DIR}/${script}" "$@"
}
