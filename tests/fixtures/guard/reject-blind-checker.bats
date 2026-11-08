# The wrapper defined, but nothing in it the checker recognises as an
# invocation. Every rule passes vacuously and the file reads clean -- which is
# how the indirect `${script}` form slipped past the name-keyed version.
rekol_bats_run_destructive() {
    assert_destructive_targets_sandboxed || return 1
    run some_other_thing "$@"
}
