"""#572: host-free store boundary tests for both hold setters."""

import json

import pytest

from project_board import store, work_locations as ledger

SHA = "abc1234" + "d" * 33


@pytest.fixture(params=["flag_blocked", "set_merge_hold"])
def hold(request, make_board):
    bead = {"id": "bd-1", "title": "Work", "status": "open", "labels": [], "comments": []}
    calls = []
    failures = set()

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] in failures:
            raise store.BoardError("write failed")
        if args[0] == "show":
            return [bead]
        if args[0] == "comments":
            bead["comments"].append({"text": args[3]})
        elif args[0] == "update":
            for i, arg in enumerate(args):
                if arg == "--add-label":
                    bead["labels"].append(args[i + 1])
                elif arg == "--remove-label" and args[i + 1] in bead["labels"]:
                    bead["labels"].remove(args[i + 1])
        else:
            pytest.fail(f"unexpected external operation: {args}")

    b = make_board(run)
    return b, lambda reason: getattr(b, request.param)("bd-1", reason), bead, calls, failures


def record(b, **changes):
    return b.record_work_location(
        "bd-1",
        {
            "id": "clone",
            "path": "/untrusted/$(do-not-execute)",
            "purpose": "save work",
            "sha": SHA,
            "branch": "feat/rescue",
            "pushed": False,
            **changes,
        },
    )


@pytest.mark.parametrize(
    "reason", [f"preserve {SHA}", "commit abc1234", "branch=feat/rescue", "branch `rescue`", "refs/heads/rescue"]
)
def test_missing_identity_refused_before_writes(hold, reason):
    _, set_hold, bead, calls, _ = hold
    with pytest.raises(store.BoardError, match="board_record_work_location"):
        set_hold(reason)
    assert bead["labels"] == bead["comments"] == []
    assert all(c[0] == "show" for c in calls)


@pytest.mark.parametrize("pushed", [False, None, True])
def test_recorded_identity_context_survives_in_audit(hold, pushed):
    b, set_hold, bead, _, _ = hold
    record(b, pushed=pushed)
    result = set_hold(f"preserve commit {SHA} on branch=feat/rescue")
    assert result["labels"]
    context = json.loads(bead["comments"][-1]["text"].split("work-location context: ")[1])
    assert {c["kind"] for c in context} == {"sha", "branch"}
    for item in context:
        location = item["locations"][0]
        assert location["path"] == "/untrusted/$(do-not-execute)"
        assert location["branch"] == "feat/rescue"
        assert location["sha"] == SHA
        assert location["pushed"] is pushed


def test_remote_report_needs_no_path(hold):
    b, set_hold, bead, _, _ = hold
    record(b, path="", pushed=True, remote_ref="refs/heads/feat/rescue")
    set_hold(f"hold {SHA}")
    assert '"pushed": true' in bead["comments"][-1]["text"]
    assert '"path": ""' in bead["comments"][-1]["text"]


@pytest.mark.parametrize("pushed", [None, False, "error"])
def test_remote_url_or_verification_failure_is_not_pushed(hold, pushed):
    b, set_hold, bead, _, _ = hold
    with pytest.raises(store.BoardError):
        record(b, path="", pushed=pushed, remote_ref="refs/heads/feat/rescue")
    with pytest.raises(store.BoardError, match="record_work_location"):
        set_hold(SHA)
    assert bead["labels"] == bead["comments"] == []


@pytest.mark.parametrize(
    "reason",
    [
        "feedback needed; defaced facade; deadbeef is an ambiguous word",
        "Refs #572 and #12345678; issue 12345678",
        "wait on abc1234xyz and ticket-abc1234",
        "QA UI/API behavior; branch of the decision tree",
        "manual QA before release",
        "commit feedback before release",
    ],
)
def test_ordinary_prose_unchanged(hold, reason):
    _, set_hold, bead, _, _ = hold
    set_hold(reason)
    assert bead["comments"][-1]["text"].endswith(reason)
    assert "work-location context" not in bead["comments"][-1]["text"]


@pytest.mark.parametrize("command", ["comments", "update"])
def test_partial_write_failure_never_reports_success(hold, command):
    b, set_hold, bead, _, failures = hold
    record(b)
    failures.add(command)
    with pytest.raises(store.BoardError, match="write failed"):
        set_hold(SHA)
    assert bead["labels"] == []
    assert len(bead["comments"]) == (1 if command == "comments" else 2)


def test_only_latest_matching_entry_counts(hold):
    b, set_hold, bead, _, _ = hold
    record(b)
    record(b, sha="987abcd" + "e" * 33)
    with pytest.raises(store.BoardError):
        set_hold(SHA)
    assert bead["labels"] == []


def test_ambiguous_short_hash_and_other_missing_identity_refused(hold):
    b, set_hold, bead, _, _ = hold
    record(b)
    record(b, id="other", path="/other", sha="abc1234" + "e" * 33)
    for reason in ("commit abc1234", f"{SHA} and branch=missing"):
        with pytest.raises(store.BoardError):
            set_hold(reason)
    assert bead["labels"] == []


def test_legacy_holds_remain_readable_and_merge_set_is_idempotent(make_board):
    bead = {
        "id": "bd-1",
        "title": "old",
        "status": "open",
        "labels": ["merge-hold"],
        "comments": [{"text": f"merge-hold set: {SHA}"}],
    }

    def run(*args, **kwargs):
        assert args[0] == "show"
        return [bead]

    b = make_board(run)
    assert b.get_feature("bd-1")["merge_hold"]
    assert b.set_merge_hold("bd-1", SHA)["merge_hold"]


def test_token_boundaries_and_explicit_letter_only_hashes():
    assert ledger.hold_identities("sha=deadbeef branch='rescue'") == [("sha", "deadbeef"), ("branch", "rescue")]
    assert ledger.hold_identities("https://host/abc1234 abc1234-long #abc1234") == []


def test_corrupt_ledger_refuses_git_hold_but_not_prose(hold):
    b, set_hold, bead, _, _ = hold
    record(b)
    bead["comments"].append({"text": ledger.PREFIX + "invalid"})
    with pytest.raises(store.BoardError, match="malformed"):
        set_hold(SHA)
    assert bead["labels"] == []
    set_hold("manual QA")


def test_failed_ledger_write_cannot_authorize_hold(hold):
    b, set_hold, bead, _, failures = hold
    failures.add("comments")
    with pytest.raises(store.BoardError, match="write failed"):
        record(b)
    failures.clear()
    with pytest.raises(store.BoardError, match="record_work_location"):
        set_hold(SHA)
    assert bead["labels"] == bead["comments"] == []
