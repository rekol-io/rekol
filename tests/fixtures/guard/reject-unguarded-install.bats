# `do_full_install` ran install.sh unguarded and was invisible to the version of
# the checker that keyed on "uninstall.sh" alone. A leaked REKOL_INDEX_DIR then
# passed into install.sh, which tried to rebuild the developer's live index.
rekol_bats_run_destructive() {
    assert_destructive_targets_sandboxed || return 1
    local script="$1"; shift
    run env HOME="$SBHOME" "${COMPONENT_DIR}/${script}" "$@"
}

do_full_install() {
    run env HOME="$SBHOME" "${COMPONENT_DIR}/install.sh" --no-skill
}
