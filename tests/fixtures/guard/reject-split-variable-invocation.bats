# Codex's shape: the invocation assembled across two assignments, so no single
# line carries both COMPONENT_DIR and the script name. The checker recognised
# nothing and reported zero violations -- which is what it prints when all is
# well. A destructive suite with no recognised invocation means the checker is
# blind, not satisfied, so zero is now a violation in itself.
setup() {
    rekol_bats_clear_path_overrides
}

@test "sneaks past by assembling the path" {
    exe="$COMPONENT_DIR"
    exe="$exe/uninstall.sh"
    run "$exe" --purge-archive
}
