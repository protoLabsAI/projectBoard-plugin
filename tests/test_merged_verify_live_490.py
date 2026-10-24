"""The merged-state verification budget reloads on the existing loop (#490)."""

from types import SimpleNamespace

import pytest

from project_board.loop import BoardLoop


@pytest.mark.parametrize("value", [9, 2, 0, "7"])
def test_reload_applies_merged_verify_max_without_restart(value):
    loop = BoardLoop({"merged_verify_max": 5})
    config = SimpleNamespace(plugin_config={"project_board": {"merged_verify_max": value}})

    assert loop.reload(config) == {"merged_verify_max": (5, int(value))}
    assert loop.merged_verify_max == int(value)
    assert loop.cfg["merged_verify_max"] == int(value)
    assert loop.reload(config) == {}


@pytest.mark.parametrize("value", [-1, -10, "-3"])
def test_reload_clamps_merged_verify_max_to_zero(value):
    loop = BoardLoop({"merged_verify_max": 5})

    assert loop.reload({"merged_verify_max": value}) == {"merged_verify_max": (5, 0)}
    assert loop.merged_verify_max == 0
    assert loop.cfg["merged_verify_max"] == 0


def test_reload_keeps_merged_verify_max_when_missing_or_malformed():
    loop = BoardLoop({"merged_verify_max": 8})

    assert loop.reload({}) == {}
    assert loop.reload({"merged_verify_max": "invalid"}) == {}
    assert loop.merged_verify_max == 8
    assert loop.cfg["merged_verify_max"] == 8
