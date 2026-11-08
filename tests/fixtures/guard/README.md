# Guard-checker fixtures (#233)

Each file is a shape `assert_invocations_guarded.py` must accept or reject.
They live here rather than inside heredocs in `test_uninstall.bats` because the
checker scans that file line by line: a heredoc body holding a second wrapper
reads as a real second wrapper, and the suite flagged itself. Data belongs in
files.

`accept-*` must produce zero violations; `reject-*` must produce at least one.
Both directions matter — a checker that rejects everything passes every
rejection test.
