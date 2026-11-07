# The shape a rebase produces: the guard line simply gone.
rekol_bats_run_destructive() {
    local script="$1"; shift
    run env HOME="$SBHOME" "${COMPONENT_DIR}/${script}" "$@"
}
