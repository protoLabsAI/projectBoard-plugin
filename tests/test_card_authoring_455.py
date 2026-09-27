"""Card authoring: the Ready gate at authoring time (#453, #455, #458).

Both live boards' first cross-board pilot (2026-09-27) churned the same way: create a
card, get refused at `mark_ready` for an unmarked new file, fix it, get refused again for
breadth (a one-token change is four files once the generated `dist/` output and the
mandatory changeset are counted), cancel, split, recreate. And a batch of five cards on
one `package.json` needed all ten pairwise `depends_on` edges, because only a DIRECT edge
counted as serialising two cards.

The fixes, pinned here:

- `breadth_exclude` globs keep files nobody authors out of the breadth COUNT (they stay
  in the card).
- the Ready gate reports EVERY failure at once, and `board_create_feature` /
  `board_update_feature` dry-run it when the card is written (`ready_check`);
  `board_check_ready` asks without changing anything.
- the shared-file gate accepts any `depends_on` PATH through open cards, reports every
  unserialised pair, and suggests the minimal chain; a project's `hot_files` are chained
  at create.

The unit tier fakes `br`; the tier at the bottom runs the real binary, because the edges
and comments the hot-file chain writes, and the dependency rows the closure reads, are
exactly what beads itself decides (#353).
"""

from __future__ import annotations

import json
import shutil

import pytest

import project_board as pb
from project_board import projects as projects_mod
from project_board import store as store_mod
from project_board.store import (
    DEFAULT_BREADTH_EXCLUDE,
    BeadsBoard,
    BoardError,
    parse_glob_list,
    path_matches_glob,
    split_breadth,
)

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)


class _Br:
    """A fake ``_run`` that records every call; reads return empty."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, want_json=False, with_has_more=False):
        self.calls.append(args)
        val = [] if want_json else ""
        return (val, None) if with_has_more else val

    def writes(self):
        return [c for c in self.calls if c and c[0] in ("update", "dep", "comments", "close", "create")]


def _card(fid, files, *, state="backlog", deps=(), created="", project="", **over):
    base = {
        "id": fid,
        "board_state": state,
        "spec": "s",
        "acceptance_criteria": "- WHEN x THE SYSTEM SHALL y",
        "files_to_modify": list(files),
        "difficulty": "medium",
        "design": "",
        "depends_on": list(deps),
        "created_at": created,
        "project": project,
        "issue_type": "feature",
    }
    base.update(over)
    return base


def _wire(monkeypatch, board, cards):
    by_id = {c["id"]: c for c in cards}
    monkeypatch.setattr(board, "get_feature", lambda fid: by_id.get(fid))
    monkeypatch.setattr(board, "list_features", lambda *a, **k: list(cards))


# ── the glob matcher behind breadth_exclude / hot_files ─────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        ".changeset/type-scale-tokens.md (new)",
        "packages/design-system/dist/tokens.css",
        "dist/tokens.json",
        "pnpm-lock.yaml",
        "apps/web/package-lock.json",
        "uv.lock",
        "src/api.generated.ts",
        "packages/ui/CHANGELOG.md",
        "changelog.d/455.md (new)",
        "./dist/x.js",
    ],
)
def test_default_breadth_exclude_covers_files_nobody_authors(path):
    assert any(path_matches_glob(path, g) for g in DEFAULT_BREADTH_EXCLUDE), path


@pytest.mark.parametrize(
    "path",
    ["packages/design-system/src/tokens.js", "src/distribution/x.ts", "dist.js", "docs/changeset.md", "package.json"],
)
def test_default_breadth_exclude_keeps_authored_files_counted(path):
    assert not any(path_matches_glob(path, g) for g in DEFAULT_BREADTH_EXCLUDE), path


def test_glob_semantics():
    assert path_matches_glob("packages/ui/package.json", "packages/*/package.json")
    assert not path_matches_glob("packages/ui/sub/package.json", "packages/*/package.json")
    assert path_matches_glob("a/b/c/gen/x.py", "**/gen/**")
    assert path_matches_glob("build/out/x.bin", "build/")  # a trailing slash = the whole dir
    assert path_matches_glob("deep/nested/index.ts", "index.ts")  # no slash = any depth
    assert not path_matches_glob("src/index.tsx", "index.ts")


def test_parse_glob_list_defaults_token_and_replacement():
    assert parse_glob_list(None, ("a",)) == ("a",)  # unset → the defaults
    assert parse_glob_list([], ("a",)) == ()  # [] → nothing
    assert parse_glob_list(["defaults", "gen/**"], ("a", "b")) == ("a", "b", "gen/**")
    assert parse_glob_list("x/**, y.lock\nz", ()) == ("x/**", "y.lock", "z")
    assert parse_glob_list(["only/**"], ("a",)) == ("only/**",)  # a list without the token replaces


def test_split_breadth_keeps_order_and_reports_the_excluded():
    counted, excluded = split_breadth(
        ["src/a.ts", "dist/a.css", ".changeset/x.md (new)", "src/b.ts"], DEFAULT_BREADTH_EXCLUDE
    )
    assert counted == ["src/a.ts", "src/b.ts"]
    assert excluded == ["dist/a.css", ".changeset/x.md (new)"]


# ── breadth: the protoContent one-token change is no longer at the cap ────────────


def test_protocontent_token_change_with_tests_and_story_passes_the_breadth_gate(make_board, monkeypatch):
    """The #455 card: tokens.js + dist/tokens.css + dist/tokens.json + a changeset + a test
    + a story is SIX files, but three are authored. It used to be refused (6 > 4)."""
    br = _Br()
    b = make_board(br)
    card = _card(
        "ds-1",
        [
            "src/tokens.js (new)",
            "dist/tokens.css (new)",
            "dist/tokens.json (new)",
            ".changeset/type-scale.md (new)",
            "src/tokens.test.js (new)",
            "src/Tokens.stories.tsx (new)",
        ],
    )
    _wire(monkeypatch, b, [card])
    check = b.ready_check("ds-1")
    assert check["ok"], check["refusals"]
    assert check["breadth"]["counted"] == 3 and len(check["breadth"]["excluded"]) == 3
    b.mark_ready("ds-1")


def test_breadth_refusal_counts_only_authored_files_and_names_the_excluded(make_board, monkeypatch):
    br = _Br()
    b = make_board(br)
    authored = [f"src/f{i}.ts (new)" for i in range(5)]
    _wire(monkeypatch, b, [_card("ds-2", [*authored, "dist/x.css (new)", "pnpm-lock.yaml (new)"])])
    with pytest.raises(BoardError) as exc:
        b.mark_ready("ds-2")
    err = str(exc.value)
    assert "Breadth gate" in err and "names 5 files_to_modify" in err
    assert "dist/x.css (new)" in err and "breadth_exclude" in err
    assert br.writes() == []


def test_a_project_breadth_exclude_overrides_the_defaults(make_board, monkeypatch):
    """`breadth_exclude: []` on the card's project counts everything again."""
    br = _Br()
    b = make_board(br)
    b.projects = {"pc": {"repo": "/repo", "breadth_exclude": []}}
    files = ["src/a.ts (new)", "src/b.ts (new)", "dist/a.css (new)", "dist/b.css (new)", ".changeset/c.md (new)"]
    _wire(monkeypatch, b, [_card("ds-3", files, project="pc")])
    assert [r["gate"] for r in b.ready_check("ds-3")["refusals"]] == ["breadth"]
    b.projects = {"pc": {"repo": "/repo", "breadth_exclude": ["defaults", "src/b.ts"]}}
    check = b.ready_check("ds-3")
    assert check["ok"] and check["breadth"]["counted"] == 1


def test_resolve_projects_inherits_top_level_authoring_policy():
    cfg = {
        "breadth_exclude": ["gen/**"],
        "hot_files": ["package.json"],
        "projects": {"a": {"repo": "/a"}, "b": {"repo": "/b", "breadth_exclude": []}},
    }
    resolved = projects_mod.resolve_projects(cfg)
    assert resolved["a"]["breadth_exclude"] == ["gen/**"] and resolved["a"]["hot_files"] == ["package.json"]
    assert resolved["b"]["breadth_exclude"] == []  # the entry's own value wins
    implicit = projects_mod.resolve_projects({"breadth_exclude": ["x/**"]})
    assert next(iter(implicit.values()))["breadth_exclude"] == ["x/**"]


# ── every failure at once ───────────────────────────────────────────────────────────


def test_mark_ready_names_every_failed_check_in_one_refusal(make_board, monkeypatch, tmp_path):
    br = _Br()
    b = make_board(br, repo=str(tmp_path))
    card = _card(
        "bd-9",
        [".changeset/x.md", *[f"src/f{i}.ts (new)" for i in range(7)]],  # an unmarked new file, 7 > 6
        acceptance_criteria="",  # no acceptance criteria
        difficulty="large",  # …and no design
    )
    _wire(monkeypatch, b, [card])
    with pytest.raises(BoardError) as exc:
        b.mark_ready("bd-9")
    err = str(exc.value)
    assert "fails 4 checks" in err
    for needle in ("acceptance_criteria", "do not exist in the repo", "Breadth gate", "Design gate"):
        assert needle in err, needle
    assert ".changeset/x.md (new)" in err  # #453: the fix shows the marker on THIS path
    assert br.writes() == []


def test_ready_check_reports_without_writing(make_board, monkeypatch):
    br = _Br()
    b = make_board(br)
    _wire(monkeypatch, b, [_card("bd-1", ["a.py"], spec="")])
    check = b.ready_check("bd-1")
    assert check["ok"] is False and check["state"] == "backlog"
    assert {r["gate"] for r in check["refusals"]} == {"required-fields", "phantom-paths"}
    assert all(r["fix"] for r in check["refusals"])
    assert br.writes() == []


def test_ready_check_on_a_card_past_ready_says_so(make_board, monkeypatch):
    b = make_board(_Br())
    _wire(monkeypatch, b, [_card("bd-1", ["a.py (new)"], state="in_progress")])
    check = b.ready_check("bd-1")
    assert check["ok"] and "in_progress" in check["note"]


def test_an_edge_onto_a_cancelled_card_is_an_advisory_not_a_refusal(make_board, monkeypatch):
    b = make_board(_Br())
    cards = [_card("bd-old", ["x.py (new)"], state="cancelled"), _card("bd-new", ["y.py (new)"], deps=["bd-old"])]
    _wire(monkeypatch, b, cards)
    check = b.ready_check("bd-new")
    assert check["ok"]
    assert len(check["advisories"]) == 1 and "CANCELLED" in check["advisories"][0]["message"]
    b.mark_ready("bd-new")


# ── #458: a depends_on PATH serialises a pair ────────────────────────────────────────

_PKG = "packages/ui/package.json"


def _five(chain=True, **over):
    """ds-ruq ← ds-xhb ← ds-07d ← ds-f5i ← ds-xwf, all on one package.json (the live batch)."""
    ids = ["ds-ruq", "ds-xhb", "ds-07d", "ds-f5i", "ds-xwf"]
    cards = []
    for n, fid in enumerate(ids):
        deps = [ids[n - 1]] if chain and n else []
        cards.append(_card(fid, [_PKG], deps=deps, created=f"2026-09-27T10:0{n}:00Z", **over))
    return cards


@pytest.mark.parametrize("fid", ["ds-ruq", "ds-07d", "ds-xwf"])
def test_a_chain_serialises_every_pair_on_it(make_board, monkeypatch, fid, tmp_path):
    (tmp_path / "packages" / "ui").mkdir(parents=True)
    (tmp_path / _PKG).write_text("{}")
    b = make_board(_Br(), repo=str(tmp_path))
    _wire(monkeypatch, b, _five())
    assert b.ready_check(fid)["ok"]
    b.mark_ready(fid)


def test_a_path_through_a_closed_card_does_not_serialise(make_board, monkeypatch):
    """C → B(done) → A: B's edge released when it merged, so C and A can build together."""
    b = make_board(_Br())
    cards = [
        _card("A", ["f.py (new)"]),
        _card("B", ["g.py (new)"], state="done", deps=["A"]),
        _card("C", ["f.py (new)"], deps=["B"]),
    ]
    _wire(monkeypatch, b, cards)
    check = b.ready_check("C")
    assert [r["gate"] for r in check["refusals"]] == ["shared-file"]
    assert check["suggested_edges"] == [{"feature_id": "C", "depends_on": "A", "files": ["f.py"]}]


def test_unchained_batch_reports_every_pair_and_the_minimal_chain(make_board, monkeypatch, tmp_path):
    (tmp_path / "packages" / "ui").mkdir(parents=True)
    (tmp_path / _PKG).write_text("{}")
    b = make_board(_Br(), repo=str(tmp_path))
    cards = _five(chain=False)
    _wire(monkeypatch, b, cards)
    check = b.ready_check("ds-07d")
    assert len(check["unserialised"]) == 10  # every pair, the neighbours' included
    assert check["suggested_edges"] == [
        {"feature_id": "ds-xhb", "depends_on": "ds-ruq", "files": [_PKG]},
        {"feature_id": "ds-07d", "depends_on": "ds-xhb", "files": [_PKG]},
        {"feature_id": "ds-f5i", "depends_on": "ds-07d", "files": [_PKG]},
        {"feature_id": "ds-xwf", "depends_on": "ds-f5i", "files": [_PKG]},
    ]
    assert [r["gate"] for r in check["refusals"]] == ["shared-file"]
    msg = check["refusals"][0]["message"]
    for other in ("ds-ruq", "ds-xhb", "ds-f5i", "ds-xwf"):
        assert other in msg
    # applying the suggestion serialises everything
    by_id = {c["id"]: c for c in cards}
    for e in check["suggested_edges"]:
        by_id[e["feature_id"]]["depends_on"].append(e["depends_on"])
    assert all(b.ready_check(c["id"])["ok"] for c in cards)


def test_the_suggested_chain_respects_existing_edges_and_never_cycles(make_board, monkeypatch):
    """The OLDEST card already depends on the NEWEST, so creation order would close a
    cycle. The suggestion orders by the existing edges first, creation order second."""
    b = make_board(_Br())
    cards = [
        _card("c1", ["x.ts (new)", "y.ts (new)"], created="1", deps=["c3"]),
        _card("c2", ["x.ts (new)"], created="2"),
        _card("c3", ["x.ts (new)", "y.ts (new)"], created="3"),
    ]
    _wire(monkeypatch, b, cards)
    check = b.ready_check("c2")
    by_id = {c["id"]: c for c in cards}
    for e in check["suggested_edges"]:
        by_id[e["feature_id"]]["depends_on"].append(e["depends_on"])
    # acyclic: a topological walk consumes every card
    remaining = {c["id"]: set(c["depends_on"]) for c in cards}
    while remaining:
        free = [i for i, d in remaining.items() if not d & set(remaining)]
        assert free, f"suggested edges closed a cycle: {check['suggested_edges']}"
        for i in free:
            remaining.pop(i)
    assert all(b.ready_check(c["id"])["ok"] for c in cards)
    assert len(check["suggested_edges"]) == 1  # c3 <- c1 exists; only c2 needs placing


def test_the_new_marker_does_not_hide_a_shared_file(make_board, monkeypatch):
    b = make_board(_Br())
    _wire(monkeypatch, b, [_card("A", ["src/x.ts"], state="in_progress"), _card("B", ["src/x.ts (new)"])])
    check = b.ready_check("B")
    assert [r["gate"] for r in check["refusals"]] == ["shared-file"]
    assert "src/x.ts (new)" in check["refusals"][0]["message"]  # named as this card names it


def test_shared_files_across_projects_never_conflict(make_board, monkeypatch):
    b = make_board(_Br())
    _wire(
        monkeypatch,
        b,
        [_card("A", ["PROTO.md (new)"], project="one"), _card("B", ["PROTO.md (new)"], project="two")],
    )
    assert b.ready_check("B")["ok"]


# ── tools: the dry run rides the create/update reply ────────────────────────────────


class _ToolStore:
    """Just enough store for the create/update/check tools."""

    def __init__(self, check):
        self.check = check
        self.created = []

    def list_features(self, *a, **k):
        return []

    def create_feature(self, title, **kw):
        self.created.append(title)
        return {"id": "bd-new", "board_state": "backlog", "title": title}

    def update_feature(self, fid, **kw):
        return {"id": fid, "board_state": "backlog", "title": "t"}

    def ready_check(self, fid):
        return dict(self.check, id=fid)


_FAILING = {
    "ok": False,
    "state": "backlog",
    "refusals": [{"gate": "phantom-paths", "message": "Ready gate: … x.md", "fix": "mark it `x.md (new)`"}],
    "advisories": [],
    "breadth": {"counted": 1, "excluded": [], "cap": 4, "difficulty": "medium"},
    "unserialised": [],
    "suggested_edges": [],
}


def _tools(monkeypatch, store):
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: store)
    return {t.name: t for t in pb._board_tools({})}


def test_create_reply_carries_the_dry_run_and_still_creates(monkeypatch):
    store = _ToolStore(_FAILING)
    out = json.loads(
        _tools(monkeypatch, store)["board_create_feature"].invoke({"title": "t", "files_to_modify": "x.md"})
    )
    assert store.created == ["t"] and out["id"] == "bd-new"
    rc = out["ready_check"]
    assert rc["ok"] is False and "phantom-paths" in rc["summary"]
    assert rc["will_be_refused_at_ready"] == ["Ready gate: … x.md Fix: mark it `x.md (new)`"]


def test_update_reply_carries_the_dry_run(monkeypatch):
    out = json.loads(
        _tools(monkeypatch, _ToolStore(dict(_FAILING, ok=True, refusals=[])))["board_update_feature"].invoke(
            {"feature_id": "bd-1", "spec": "s"}
        )
    )
    assert out["ready_check"] == {"ok": True}


def test_board_check_ready_returns_the_full_report(monkeypatch):
    out = json.loads(_tools(monkeypatch, _ToolStore(_FAILING))["board_check_ready"].invoke({"feature_id": "bd-1"}))
    assert out["id"] == "bd-1" and out["refusals"][0]["gate"] == "phantom-paths"


def test_a_dry_run_that_cannot_read_never_fails_the_create(monkeypatch):
    class _Broken(_ToolStore):
        def ready_check(self, fid):
            raise BoardError("database is locked")

    out = json.loads(_tools(monkeypatch, _Broken(_FAILING))["board_create_feature"].invoke({"title": "t"}))
    assert out["id"] == "bd-new" and out["ready_check"]["ok"] is None and "locked" in out["ready_check"]["error"]


def test_the_create_and_update_docstrings_teach_the_new_marker():
    """#453: the marker was documented only in mark_ready's refusal."""
    tools = {t.name: t for t in pb._board_tools({})}
    for name in ("board_create_feature", "board_update_feature"):
        assert "(new)" in tools[name].description and ".changeset/" in tools[name].description, name


# ── real br: what beads itself stores and reports ───────────────────────────────────


@pytest.fixture
def real_board(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "src").mkdir()
    board = BeadsBoard(
        repo=str(tmp_path),
        actor="test",
        projects={"pc": {"repo": str(tmp_path), "hot_files": ["package.json"]}},
        default_project="pc",
    )
    monkeypatch.setattr(store_mod, "get_store", lambda **_kw: board)
    return board


def _mk(board, title, files, **kw):
    return board.create_feature(
        title, spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=files, **kw
    )


@requires_br
def test_hot_files_chain_lands_through_real_br(real_board):
    """Three cards on the hot package.json form a chain as they are created — edges and
    notes written by the real binary — and all three pass the real Ready gate, the first
    and third serialised only through the second."""
    a = _mk(real_board, "one", ["package.json", "src/a.ts (new)"])
    b = _mk(real_board, "two", ["package.json", "src/b.ts (new)"])
    c = _mk(real_board, "three", ["package.json (new)", "src/c.ts (new)"])  # the marker doesn't hide it
    assert "hot_file_chain" not in a
    assert b["hot_file_chain"] == [{"depends_on": a["id"], "files": ["package.json"]}]
    assert c["hot_file_chain"] == [{"depends_on": b["id"], "files": ["package.json"]}]
    assert real_board.get_feature(c["id"])["depends_on"] == [b["id"]]  # only the direct edge
    notes = real_board.feature_comments(c["id"])
    assert any("hot-file chain" in n and b["id"] in n for n in notes)
    for card in (a, b, c):
        assert real_board.ready_check(card["id"])["ok"]
        real_board.mark_ready(card["id"])


@requires_br
def test_hot_files_skip_a_card_already_ordered_behind_the_holder(real_board):
    a = _mk(real_board, "one", ["package.json"])
    b = _mk(real_board, "two", ["package.json"], depends_on=[a["id"]])
    assert "hot_file_chain" not in b
    assert real_board.get_feature(b["id"])["depends_on"] == [a["id"]]


@requires_br
def test_transitive_serialisation_and_suggested_chain_through_real_br(tmp_path, monkeypatch):
    """No hot files: three cards on one file, unchained. The real dependency rows feed the
    closure; applying the suggested edges through real `br` clears every card."""
    (tmp_path / "shared.py").write_text("x = 1\n")
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    ids = [_mk(board, f"card {n}", ["shared.py"])["id"] for n in range(3)]
    check = board.ready_check(ids[1])
    assert not check["ok"] and len(check["unserialised"]) == 3
    assert [(e["feature_id"], e["depends_on"]) for e in check["suggested_edges"]] == [
        (ids[1], ids[0]),
        (ids[2], ids[1]),
    ]
    with pytest.raises(BoardError, match="Shared-file gate"):
        board.mark_ready(ids[2])
    for e in check["suggested_edges"]:
        board.update_feature(e["feature_id"], depends_on=[e["depends_on"]])
    for fid in ids:
        board.mark_ready(fid)  # ids[2] ↔ ids[0] is ordered only through ids[1]


@requires_br
def test_create_tool_reports_the_dry_run_through_real_br(real_board):
    tools = {t.name: t for t in pb._board_tools({})}
    out = json.loads(
        tools["board_create_feature"].invoke(
            {
                "title": "unmarked changeset",
                "spec": "s",
                "acceptance_criteria": "- WHEN x THE SYSTEM SHALL y",
                "files_to_modify": "src/a.ts (new), .changeset/x.md",
            }
        )
    )
    rc = out["ready_check"]
    assert rc["ok"] is False and ".changeset/x.md (new)" in rc["will_be_refused_at_ready"][0]
    assert real_board.get_feature(out["id"]) is not None  # created anyway
    fixed = json.loads(
        tools["board_update_feature"].invoke(
            {"feature_id": out["id"], "files_to_modify": "src/a.ts (new), .changeset/x.md (new)"}
        )
    )
    assert fixed["ready_check"]["ok"] is True
    assert json.loads(tools["board_check_ready"].invoke({"feature_id": out["id"]}))["ok"] is True
