"""#728: setup advisories lead with a short plain summary and carry a fix button.

The slow-gate / slow-preflight / unwinnable-oracle advisories used to lead with the full
command (often hundreds of characters) and put the fix last, so the banner cut it off. Now
each (project, kind) is a structured entry whose summary is plain words, the joined hint
shows summary then fix before any command, and the preflight setup gap carries the first
entry's plugin_config action."""

from __future__ import annotations

from _plugin_testkit import FakeRegistry  # protoAgent's own testkit, vendored verbatim

from project_board import health
from project_board.setup_check import PREFLIGHT_KEY, GapReporter

LONG_CMD = "uv run pytest " + " ".join(f"tests/test_module_{i:03d}.py" for i in range(30))
LONG_CMD = LONG_CMD[:300]

REQUIRED = {"project", "kind", "summary", "fix_label", "fix_action", "setting_path", "detail"}


def _publish_all():
    health.publish_slow_gates({"board-plugin": {"cmd": LONG_CMD, "timeout_s": 600}})
    health.publish_preflight(
        {"board-plugin": True, "other": True},
        {},
        slow={"other": {"cmd": LONG_CMD, "timeout_s": 600, "sha": "abc"}},
        oracle={"board-plugin": f"its oracle falls back to the gate (`{LONG_CMD}`), which took 900s"},
    )


def _cmd_fragments(cmd: str) -> list[str]:
    # Every 12-character slice of the command: none may appear in the summary.
    return [cmd[i : i + 12] for i in range(0, len(cmd) - 12)]


def test_slow_gate_entry_has_a_short_plain_summary_and_the_fix_comes_before_the_command():
    assert len(LONG_CMD) == 300
    health.publish_slow_gates({"board-plugin": {"cmd": LONG_CMD, "timeout_s": 600}})
    entries = health.advisory_entries()
    assert [e["kind"] for e in entries] == ["slow_gate"]
    entry = entries[0]
    assert entry["project"] == "board-plugin"
    assert len(entry["summary"]) <= 120
    assert entry["summary"] == "board-plugin's pre-PR check takes longer than 10 minutes, so it is skipped."
    assert not any(frag in entry["summary"] for frag in _cmd_fragments(LONG_CMD))
    assert "uv run" not in entry["summary"] and LONG_CMD in entry["detail"]

    hint = health.advisory_hint()
    cmd_at = hint.index(LONG_CMD[:20])
    assert hint.index(entry["summary"]) < cmd_at
    assert hint.index("Fix: raise local_gate_timeout_s") < cmd_at
    # the phrases older tests (#483) assert are still in the line
    assert "timed out" in hint and "local_gate_cmd" in hint


def test_every_kind_yields_a_complete_entry_with_a_plugin_config_fix_action():
    _publish_all()
    entries = health.advisory_entries()
    assert [(e["project"], e["kind"]) for e in entries] == [
        ("board-plugin", "slow_gate"),
        ("board-plugin", "unwinnable_oracle"),
        ("other", "slow_preflight"),
    ]
    for e in entries:
        assert REQUIRED <= set(e)
        assert e["summary"] and len(e["summary"]) <= 120
        assert LONG_CMD[:20] not in e["summary"]
        action = e["fix_action"]
        assert action["kind"] == "plugin_config"
        assert action["label"] == e["fix_label"] and action["label"]
        assert action["fields"] and all(isinstance(f, str) for f in action["fields"])
        assert set(action) <= {"kind", "label", "fields"}  # the host's closed schema
        assert e["setting_path"] in action["fields"]
    by_kind = {e["kind"]: e for e in entries}
    assert by_kind["slow_gate"]["fix_label"] == "Raise the time limit"
    assert by_kind["slow_gate"]["fix_action"]["fields"] == ["local_gate_timeout_s", "local_gate_cmd"]
    assert by_kind["slow_preflight"]["fix_action"]["fields"] == ["preflight_cmd", "preflight_timeout_s"]
    assert "coder_solve_test_timeout_s" in by_kind["unwinnable_oracle"]["fix_action"]["fields"]
    assert "can't finish inside preflight_timeout_s" in by_kind["slow_preflight"]["detail"]
    hint = health.advisory_hint()
    assert "can't finish inside preflight_timeout_s" in hint
    assert hint.count("Fix: ") == 3


def test_a_long_project_name_still_keeps_the_summary_within_120_characters():
    name = "p" * 200
    health.publish_slow_gates({name: {"cmd": LONG_CMD, "timeout_s": 600}})
    summary = health.advisory_entries()[0]["summary"]
    assert len(summary) <= 120 and summary.endswith("so it is skipped.")


def test_no_advisories_means_no_entries_no_hint_and_no_action():
    assert health.advisory_entries() == []
    assert health.advisory_hint() == ""
    assert health.advisory_fix_action() is None


def _status(hint: str) -> dict:
    ok = {"ok": True}
    return {"br": ok, "gh": ok, "coder": ok, "repo": ok, "preflight_hint": hint}


def test_the_preflight_gap_carries_the_first_advisorys_fix_button_on_the_action_seam():
    _publish_all()
    reg = FakeRegistry(plugin_id="project_board")
    rep = GapReporter(reg)
    rep.report(_status(health.advisory_hint()))
    assert reg.setup_gaps[PREFLIGHT_KEY] == health.advisory_hint()
    assert reg.setup_gap_actions[PREFLIGHT_KEY] == {
        "kind": "plugin_config",
        "label": "Raise the time limit",
        "fields": ["local_gate_timeout_s", "local_gate_cmd"],
    }
    # cleared advisory → the clear carries no action
    health._health.pop("preflight", None)
    health._health.pop("slow_gates", None)
    rep.report(_status(""))
    assert PREFLIGHT_KEY not in reg.setup_gaps and PREFLIGHT_KEY not in reg.setup_gap_actions


class _LegacyLabelHost:
    """The prior seam: report_setup_gap(key, message, *, label=None) — no action."""

    def __init__(self):
        self.calls = []

    def report_setup_gap(self, key, message, *, label=None):
        self.calls.append((key, message, label))


def test_the_plain_preflight_message_still_lands_on_a_seam_without_action():
    _publish_all()
    host = _LegacyLabelHost()
    rep = GapReporter(host)
    assert rep.action_supported is False
    hint = health.advisory_hint()
    rep.report(_status(hint))
    assert (PREFLIGHT_KEY, hint, None) in host.calls
