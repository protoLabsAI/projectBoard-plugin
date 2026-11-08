"""`br` runs with a history-snapshot interval by default (projectBoard#686).

`br` copied the whole issues.jsonl into .br_history on every write: on protoEngineer (2026-11-07)
that was 1.3-2.8 GB of snapshots within hours, and the disk churn slowed every `br` call."""

import subprocess

from project_board import store as store_mod


def test_br_runs_with_a_history_snapshot_interval_by_default(monkeypatch):
    monkeypatch.delenv("BR_HISTORY_MIN_INTERVAL_SECS", raising=False)
    assert store_mod._br_env()["BR_HISTORY_MIN_INTERVAL_SECS"] == "3600"


def test_an_operator_set_history_interval_wins(monkeypatch):
    monkeypatch.setenv("BR_HISTORY_MIN_INTERVAL_SECS", "60")
    assert store_mod._br_env()["BR_HISTORY_MIN_INTERVAL_SECS"] == "60"


def test_the_br_process_gets_that_environment(monkeypatch, tmp_path):
    seen = {}

    class _P:
        returncode = 0

        def __init__(self, cmd, **kw):
            seen.update(kw)

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.delenv("BR_HISTORY_MIN_INTERVAL_SECS", raising=False)
    monkeypatch.setattr(subprocess, "Popen", _P)
    store_mod._run_br_process(["br", "list"], cwd=str(tmp_path), args=("list",))
    assert seen["env"]["BR_HISTORY_MIN_INTERVAL_SECS"] == "3600"
