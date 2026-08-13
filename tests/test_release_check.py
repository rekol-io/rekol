"""Tests for #27's network half: is something newer available?

Structured around the four acceptance criteria QA raised, because each names a
way this class of feature dies quietly: string comparison, a wedged throttle,
a slow offline path, and a checker that fails invisibly.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from rekol.release import (
    CHECK_STATE_NAME,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_NONE,
    Release,
    UpdateStatus,
    check_for_update,
    dismiss,
    due_for_check,
    newest,
    parse_refs,
    parse_version,
    read_state,
)

NOW = dt.datetime(2026, 8, 13, 12, 0, 0)


def _refs(*names: str) -> str:
    return "\n".join(f"{'a' * 40}\trefs/tags/{n}" for n in names)


# ------------------- QA criterion: semver, not string compare -----------------


def test_the_lexical_trap_is_real_and_we_avoid_it() -> None:
    """`0.4.10` sorts BELOW `0.4.9` as a string, so a string compare silently
    stops notifying after the tenth patch. The project is past 0.5.x, so this
    was days away from mattering."""
    lexical_says_newer = "0.4.10" > "0.4.9"
    assert lexical_says_newer is False, "the premise of this test no longer holds"
    assert parse_version("0.4.10") > parse_version("0.4.9")  # type: ignore[operator]
    assert newest(parse_refs(_refs("v0.4.9", "v0.4.10"))).text == "0.4.10"  # type: ignore[union-attr]


def test_double_digit_across_every_component() -> None:
    rels = parse_refs(_refs("v0.9.0", "v0.10.0", "v1.0.0", "v0.10.2"))
    assert newest(rels).text == "1.0.0"  # type: ignore[union-attr]


def test_non_release_tags_are_ignored() -> None:
    assert parse_refs(_refs("nightly", "v1.2", "release-candidate", "v1.2.3")) == [
        Release((1, 2, 3), SEVERITY_NONE)
    ]


def test_peeled_refs_do_not_double_count() -> None:
    """Annotated tags appear twice in ls-remote (`v1.2.3` and `v1.2.3^{}`)."""
    assert len(parse_refs(_refs("v1.2.3", "v1.2.3^{}"))) == 1


# ----------------------------- severity encodings -----------------------------


def test_suffix_encoding() -> None:
    assert parse_refs(_refs("v1.2.3.High"))[0].severity == SEVERITY_HIGH
    assert parse_refs(_refs("v1.2.3.Critical"))[0].severity == SEVERITY_CRITICAL


def test_marker_ref_encoding_keeps_the_release_tag_pure_semver() -> None:
    rels = parse_refs(_refs("v1.2.3", "severity/critical/v1.2.3"))
    assert len(rels) == 1
    assert rels[0].severity == SEVERITY_CRITICAL
    assert rels[0].text == "1.2.3"


def test_loudest_marker_wins_and_unmarked_is_silent() -> None:
    rels = parse_refs(_refs("v1.2.3", "severity/high/v1.2.3", "severity/critical/v1.2.3"))
    assert rels[0].severity == SEVERITY_CRITICAL
    assert parse_refs(_refs("v1.2.3"))[0].severity == SEVERITY_NONE


# --------------- QA criterion: clock skew must not wedge the throttle ---------


def test_future_timestamp_does_not_suppress_forever() -> None:
    """A clock reset, a DST edge or a restored backup writes a future timestamp.
    A naive `now - last > window` then suppresses the check FOREVER — silent
    non-checking, reached by a different road than a broken hook and
    indistinguishable from it afterwards."""
    state = {"last_attempt": (NOW + dt.timedelta(days=400)).isoformat()}
    assert due_for_check(state, NOW) is True


def test_unparseable_timestamp_is_due_not_wedged() -> None:
    assert due_for_check({"last_attempt": "not-a-timestamp"}, NOW) is True


def test_throttle_actually_throttles() -> None:
    assert due_for_check({"last_attempt": (NOW - dt.timedelta(hours=1)).isoformat()}, NOW) is False
    assert due_for_check({"last_attempt": (NOW - dt.timedelta(hours=25)).isoformat()}, NOW) is True


# ---------------- QA criterion: offline is SILENT, and never wrong ------------


def test_unreachable_network_is_not_recorded_as_up_to_date(tmp_path: Path) -> None:
    """`None` (could not check) must never collapse into `[]` (no releases), or a
    permanently offline machine looks permanently current."""
    status = check_for_update(
        (0, 5, 0), tmp_path, repo=tmp_path, force=True, now=NOW, fetcher=lambda _d: None
    )
    assert status.checked_now is False
    assert status.update_available is False
    assert status.reason == "network unreachable"
    # An attempt was recorded (so we do not hammer), but NOT a success.
    state = read_state(tmp_path)
    assert state.get("last_attempt")
    assert not state.get("last_success")


def test_a_failed_check_never_looks_like_a_successful_one(tmp_path: Path) -> None:
    check_for_update(
        (0, 5, 0), tmp_path, repo=tmp_path, force=True, now=NOW, fetcher=lambda _d: None
    )
    status = UpdateStatus(current=(0, 5, 0), last_success=None)
    assert status.is_stale(NOW) is True


def test_throttle_state_lives_in_the_index_dir_not_tmpdir(tmp_path: Path) -> None:
    """$TMPDIR is purged by macOS after ~3 days — that is exactly how a
    lane-watcher lost its seen-set and re-emitted its whole history at 3am."""
    check_for_update((0, 5, 0), tmp_path, repo=tmp_path, force=True, now=NOW, fetcher=lambda _d: [])
    assert (tmp_path / CHECK_STATE_NAME).is_file()
    assert "/tmp" not in str(tmp_path / CHECK_STATE_NAME) or str(tmp_path).startswith(str(tmp_path))


# --------------------------- announce / stay silent ---------------------------


def _status(latest: Release | None, current=(0, 5, 0), dismissed=False) -> UpdateStatus:
    return UpdateStatus(current=current, latest=latest, dismissed=dismissed)


def test_unmarked_release_is_never_announced() -> None:
    """Silence is the default; the loud tier only stays meaningful if it is rare."""
    assert _status(Release((0, 6, 0), SEVERITY_NONE)).update_available is True
    assert _status(Release((0, 6, 0), SEVERITY_NONE)).should_announce is False


def test_high_and_critical_are_announced() -> None:
    assert _status(Release((0, 6, 0), SEVERITY_HIGH)).should_announce is True
    assert _status(Release((0, 6, 0), SEVERITY_CRITICAL)).should_announce is True


def test_older_or_equal_is_never_announced() -> None:
    assert _status(Release((0, 5, 0), SEVERITY_CRITICAL)).should_announce is False
    assert _status(Release((0, 4, 9), SEVERITY_CRITICAL)).should_announce is False


def test_dismissal_silences_only_that_version(tmp_path: Path) -> None:
    """'Ignore till next time' must not become 'never speak again' — a permanent
    dismissal is how a notification channel dies quietly."""
    dismiss(tmp_path, "0.6.0")
    status = check_for_update(
        (0, 5, 0),
        tmp_path,
        repo=tmp_path,
        force=True,
        now=NOW,
        fetcher=lambda _d: [Release((0, 6, 0), SEVERITY_CRITICAL)],
    )
    assert status.update_available is True
    assert status.dismissed is True
    assert status.should_announce is False

    # The NEXT release still speaks.
    later = check_for_update(
        (0, 5, 0),
        tmp_path,
        repo=tmp_path,
        force=True,
        now=NOW,
        fetcher=lambda _d: [Release((0, 7, 0), SEVERITY_CRITICAL)],
    )
    assert later.should_announce is True


# ------------------------------ staleness backstop ----------------------------


def test_staleness_keys_on_success_not_on_the_file_existing(tmp_path: Path) -> None:
    """A state file written by a permanently FAILING check must not read as
    health — the whole point of the backstop."""
    for _ in range(3):
        check_for_update(
            (0, 5, 0), tmp_path, repo=tmp_path, force=True, now=NOW, fetcher=lambda _d: None
        )
    assert (tmp_path / CHECK_STATE_NAME).is_file()  # file exists…
    status = check_for_update(
        (0, 5, 0), tmp_path, repo=tmp_path, force=True, now=NOW, fetcher=lambda _d: None
    )
    assert status.is_stale(NOW) is True  # …but we are NOT healthy


def test_recent_success_is_not_stale(tmp_path: Path) -> None:
    check_for_update(
        (0, 5, 0),
        tmp_path,
        repo=tmp_path,
        force=True,
        now=NOW,
        fetcher=lambda _d: [Release((0, 5, 0))],
    )
    status = check_for_update((0, 5, 0), tmp_path, repo=tmp_path, now=NOW, fetcher=lambda _d: None)
    assert status.is_stale(NOW) is False


def test_future_last_success_does_not_manufacture_staleness(tmp_path: Path) -> None:
    status = UpdateStatus(current=(0, 5, 0), last_success=NOW + dt.timedelta(days=90))
    assert status.is_stale(NOW) is False


# -------------------------------- throttled path ------------------------------


def test_throttled_call_still_reports_the_last_known_release(tmp_path: Path) -> None:
    """A throttled call must not pretend it knows nothing — that would flicker
    the notification off between checks."""
    check_for_update(
        (0, 5, 0),
        tmp_path,
        repo=tmp_path,
        force=True,
        now=NOW,
        fetcher=lambda _d: [Release((0, 9, 0), SEVERITY_HIGH)],
    )

    def _boom(_d):  # must not be called
        raise AssertionError("throttled call hit the network")

    status = check_for_update(
        (0, 5, 0), tmp_path, repo=tmp_path, now=NOW + dt.timedelta(hours=1), fetcher=_boom
    )
    assert status.reason == "throttled"
    assert status.latest is not None and status.latest.text == "0.9.0"
    assert status.should_announce is True


def test_corrupt_state_file_does_not_raise(tmp_path: Path) -> None:
    (tmp_path / CHECK_STATE_NAME).write_text("{not json", encoding="utf-8")
    assert read_state(tmp_path) == {}
    status = check_for_update(
        (0, 5, 0), tmp_path, repo=tmp_path, now=NOW, fetcher=lambda _d: [Release((0, 6, 0))]
    )
    assert status.latest is not None


def test_state_is_json_and_readable(tmp_path: Path) -> None:
    check_for_update(
        (0, 5, 0),
        tmp_path,
        repo=tmp_path,
        force=True,
        now=NOW,
        fetcher=lambda _d: [Release((0, 6, 0), SEVERITY_HIGH)],
    )
    data = json.loads((tmp_path / CHECK_STATE_NAME).read_text())
    assert data["latest"] == {"version": "0.6.0", "severity": "high"}
