"""#585 slice 2: a release chore or a decompose task is never auto-decomposed.

On 2026-10-21 `chore: release v0.8.1` was parked too-wide and a decompose task filed for
it; the decompose task then timed out and was about to be decomposed again. Neither card
has buildable slices — a version bump has none, and splitting a split is a loop. The park
path now recognises both via one pure predicate (``no_decompose_reason``) and blocks them
with a plain timeout reason instead of filing a split; the fix is a longer coder budget.
An ordinary feature card still parks and files its split exactly as before (#378).
"""

from __future__ import annotations

import pytest

from project_board.loop import BoardLoop
from project_board.store import RELEASE_FILE_GLOBS, no_decompose_reason

from test_timeout_decompose_378 import _SplitStore, _blocks


# ── the pure predicate ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    ["chore: release v0.8.1", "chore: release v1.0.0 (lockfile bump)", "chore: release"],
)
def test_release_chore_title_is_never_decomposed(title):
    assert no_decompose_reason(title, ["lots_of_code.py"]) == "release chore (title starts with 'chore: release')"


@pytest.mark.parametrize(
    "title",
    ["Decompose bd-67b1 — timed out 2x, too wide to build", "Decompose bd-8 — timed out 3x"],
)
def test_decompose_task_title_is_never_decomposed(title):
    assert no_decompose_reason(title, []) == "decompose task (never split a split)"


@pytest.mark.parametrize(
    "files",
    [
        ["pyproject.toml"],
        ["protoagent.plugin.yaml", "pyproject.toml"],
        ["package.json", "package-lock.json", "pnpm-lock.yaml"],
        ["uv.lock"],
        ["Cargo.lock"],  # any *.lock
        ["CHANGELOG.md"],
        ["changelog.d/585-no-decompose.md", "changelog.d/584-foo.md"],
        ["pyproject.toml (new)", "uv.lock (new)"],  # (new) markers stripped
        ["./pyproject.toml"],  # leading ./ dropped
        ["pyproject.toml", "CHANGELOG.md", "changelog.d/585-x.md", "uv.lock"],
    ],
)
def test_version_or_lockfile_only_is_never_decomposed(files):
    assert no_decompose_reason("chore: bump deps", files) == "only version/lockfile files touched"


@pytest.mark.parametrize(
    "title,files",
    [
        ("Add a feature", ["app/menu.tsx"]),
        ("Bump and refactor", ["pyproject.toml", "app/menu.tsx"]),  # one code file ⇒ splittable
        ("Fix the parser", []),  # no files declared ⇒ left splittable
        ("Edit the changelog logic", ["loop/changelog.py"]),  # not the fragment dir
        ("chore: tidy up", ["package.json.bak"]),  # not a manifest/lockfile
    ],
)
def test_ordinary_cards_are_splittable(title, files):
    assert no_decompose_reason(title, files) == ""


def test_release_file_globs_cover_the_spec_names():
    for name in ("pyproject.toml", "uv.lock", "CHANGELOG.md", "changelog.d/**", "*.lock"):
        assert name in RELEASE_FILE_GLOBS


# ── the park path short-circuits for these cards ─────────────────────────────────────


async def _park(feature):
    store = _SplitStore()
    loop = BoardLoop({"coder": "proto"})
    await loop._park_for_split(store, feature["id"], 2, RuntimeError("coder timed out after 1800s"), feature=feature)
    return store


def _assert_blocked_not_split(store):
    assert store.asked == [], "a split was requested for a card that must never be decomposed"
    names = store.names()
    assert "request_decomposition" not in names and "mark_ready" not in names
    (_, _, reason, category) = _blocks(store)[-1]
    assert category == "too-wide"
    assert "not split" in reason and "timed out 2x" in reason and "coder_timeout_s" in reason
    return reason


async def test_release_chore_card_is_blocked_not_split():
    store = await _park({"id": "bd-1", "title": "chore: release v0.8.1", "files_to_modify": ["pyproject.toml"]})
    assert "release chore" in _assert_blocked_not_split(store)


async def test_version_lockfile_only_card_is_blocked_not_split():
    store = await _park(
        {"id": "bd-1", "title": "Bump versions", "files_to_modify": ["pyproject.toml", "uv.lock", "CHANGELOG.md"]}
    )
    assert "version/lockfile" in _assert_blocked_not_split(store)


async def test_decompose_task_card_is_blocked_not_split():
    store = await _park(
        {"id": "bd-67b1", "title": "Decompose bd-tg9f — timed out 2x, too wide to build", "files_to_modify": []}
    )
    assert "decompose task" in _assert_blocked_not_split(store)


async def test_ordinary_feature_still_parks_and_files_its_split(monkeypatch):
    store = _SplitStore()
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_timeout_escalation_context", lambda fid: "")
    feature = {"id": "bd-1", "title": "Add a feature", "files_to_modify": ["app/menu.tsx"]}
    await loop._park_for_split(store, "bd-1", 2, RuntimeError("coder timed out"), feature=feature)

    assert store.asked == [("bd-1", 2)]
    assert ("mark_ready", "bd-split") in store.calls
    (_, _, reason, category) = _blocks(store)[-1]
    assert category == "too-wide" and "bd-split" in reason


async def test_the_feature_is_read_from_the_store_when_not_passed(monkeypatch):
    """The park path falls back to a store read when the caller passes no feature, so an
    out-of-band caller still gets the same no-split treatment for a chore."""
    store = _SplitStore()
    monkeypatch.setattr(
        store, "get_feature", lambda fid: {"id": fid, "title": "chore: release v9.9.9", "files_to_modify": []}
    )
    loop = BoardLoop({"coder": "proto"})
    await loop._park_for_split(store, "bd-1", 2, RuntimeError("coder timed out"))
    _assert_blocked_not_split(store)
