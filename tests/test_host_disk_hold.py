"""A nearly full host disk holds dispatch, pages the operator ONCE, and clears itself (#801).

The incident (2026-11-20): bd-o7oyw, bd-tndqq and bd-0vvum each claimed, failed at
``git worktree add`` with "No space left on device", blocked ``terminal`` and paged the
operator separately. Freeing the disk brought none of them back.

Now the claim scan reads the free space on the volume holding each card's checkout and
claims nothing on one under ``min_free_disk_gb``; a drive that still fails for disk space
puts its card back to ready under a ``host-disk`` hold; the operator gets one page per
volume; and the tick releases the cards and retires the page once there is room again.

The drive cases use real git (a bare origin plus a clone, as in
test_shutdown_resume_stranded.py) and the store's real claim / block / unblock / requeue
transitions over a minimal `br` command seam (as in test_block_pages_735.py). The disk is
not really filled: a `git` wrapper on PATH fails `worktree add` with git's own ENOSPC text
and passes every other command through, and the host's own answers (the free space, and
whether a small write is refused for lack of room) are set per volume on the loop.

The error text alone holds nothing: the last cases fail the same way on a volume the host
says has room, and get the ordinary block and the card's own page.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board import loop as loop_mod
from project_board import worktree
from project_board.failures import HOST_DISK_CLASS
from project_board.loop import BoardLoop, host_disk
from project_board.loop.host_disk import GB
from project_board.store import BeadsBoard

posix_only = pytest.mark.skipif(os.name == "nt", reason="the git wrapper is a POSIX shell script")

VOL_A, VOL_B = "/Volumes/a", "/Volumes/b"
HELD_REASON = f"{host_disk.HOLD_REASON_PREFIX}worktree add failed: No space left on device"
TITLES = {"bd-1": "Trim the retry log", "bd-2": "Paginate the audit feed", "bd-3": "Cache the roster read"}


# ── fixtures ──────────────────────────────────────────────────────────────────────────


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    """A bare origin and a clone of it, so `origin/main` resolves as in production."""
    origin, seed, clone = (str(tmp_path / n) for n in ("origin.git", "seed", "clone"))
    _git("init", "--bare", origin)
    _git("init", "-b", "main", seed)
    for key, value in (
        ("user.email", "board-test@localhost"),
        ("user.name", "Board Test"),
        ("commit.gpgsign", "false"),
    ):
        _git("-C", seed, "config", key, value)
    Path(seed, "README.md").write_text("base\n")
    _git("-C", seed, "add", "-A")
    _git("-C", seed, "commit", "-m", "base commit")
    _git("-C", seed, "remote", "add", "origin", origin)
    _git("-C", seed, "push", "-u", "origin", "main")
    _git("-C", origin, "symbolic-ref", "HEAD", "refs/heads/main")
    _git("clone", origin, clone)
    return clone


@pytest.fixture
def full_disk_git(monkeypatch, tmp_path):
    """Real git, except `worktree add`, which fails the way git does on a full disk."""
    real = shutil.which("git")
    bin_dir = tmp_path / "full-disk-bin"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'prev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "worktree" ] && [ "$arg" = "add" ]; then\n'
        "    echo \"fatal: Unable to create '$PWD/.git/refs/heads/feat/card.lock': No space left on device\" >&2\n"
        "    exit 128\n"
        "  fi\n"
        '  prev="$arg"\n'
        "done\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def inbox(monkeypatch, tmp_path):
    """The host inbox: one pending row per dedup key, retired by ``mark_delivered``."""
    rows = []

    class Inbox:
        def __init__(self, path, **kwargs):
            pass

        def add(self, text, *, priority="next", source="", dedup_key="", origin_session=""):
            if any(r["dedup_key"] == dedup_key and not r["delivered_at"] for r in rows):
                return None
            row = dict(id=len(rows) + 1, text=text, source=source, dedup_key=dedup_key, delivered_at=None)
            rows.append(row)
            return row.copy()

        def list(self, *, priority_floor, include_recovery_claimed, limit):
            return [r.copy() for r in rows if not r["delivered_at"]][:limit]

        def mark_delivered(self, ids):
            for row in rows:
                if row["id"] in ids:
                    row["delivered_at"] = "now"

    monkeypatch.setitem(sys.modules, "inbox", SimpleNamespace(InboxStore=Inbox))
    monkeypatch.setattr(loop_mod, "_inbox_db_path", lambda: tmp_path / "agent.db")
    return rows


class Board(BeadsBoard):
    """The store's real transitions (claim, flag_blocked, clear_blocked, requeue) over an
    in-memory `br`: each card is a bead with a status, labels and comments."""

    def __init__(self, cards: dict[str, str]):
        self.repo, self.db, self.actor = ".", None, "board"
        self.beads = {
            fid: dict(id=fid, title=TITLES.get(fid, fid), status="open", labels=["ready"], issue_type="feature")
            for fid in cards
        }
        self.repos = dict(cards)
        self.comments: dict[str, list[str]] = {fid: [] for fid in cards}

    def get_feature(self, fid):
        bead = self.beads.get(fid)
        if bead is None:
            return None
        labels = list(bead["labels"])
        reasons = [c[len("blocked: ") :] for c in self.comments[fid] if c.startswith("blocked: ")]
        return dict(
            bead,
            labels=labels,
            bead_status=bead["status"],
            board_state=self.board_state(bead),
            blocked="blocked" in labels,
            blocked_reason=reasons[-1] if reasons and "blocked" in labels else "",
            blocked_class=next((l.split(":", 1)[1] for l in labels if l.startswith("blocked-class:")), ""),
            repo=self.repos[fid],
            base_branch="main",
            project="",
            spec="do it",
            acceptance_criteria="",  # no oracle → the single-dispatch path
            files_to_modify=[],
        )

    def _run(self, *args, **kwargs):
        if args[:2] == ("comments", "add"):
            self.comments[args[2]].append(args[3])
        elif args[0] == "update":
            bead = self.beads[args[1]]
            flags = list(args[2:])
            pairs = [(flag, flags[i + 1]) for i, flag in enumerate(flags[:-1]) if flag.startswith("--")]
            if "--claim" in flags:
                bead["status"] = "in_progress"
            for flag, value in pairs:  # `br` applies adds, then removes
                if flag == "--add-label" and value not in bead["labels"]:
                    bead["labels"].append(value)
                elif flag == "--status":
                    bead["status"] = value
            for flag, value in pairs:
                if flag == "--remove-label":
                    bead["labels"] = [label for label in bead["labels"] if label != value]
        return ""

    def comment(self, fid, text):
        self.comments[fid].append(text)

    def list_features(self, state=None, include_archived=False):
        feats = [self.get_feature(fid) for fid in self.beads]
        return [f for f in feats if state is None or f["board_state"] == state]

    def ready_queue(self, relaxed=False):
        return [f for f in self.list_features() if "ready" in f["labels"] and f["bead_status"] == "open"]

    def current_tier(self, fid):
        return ""

    def record_attempt(self, fid, **kwargs):
        pass

    def record_budget(self, fid, kind, n):
        pass

    def clear_budgets(self, fid, kinds=None):
        pass

    def states(self) -> dict[str, str]:
        return {fid: self.get_feature(fid)["board_state"] for fid in self.beads}


class _Disk:
    """A free-space probe the test sets per volume, by the checkout path asked about."""

    def __init__(self, volumes: dict[str, str]):
        self.volumes = volumes  # checkout path → volume
        self.free = {volume: 100 * GB for volume in volumes.values()}
        self.refuses_writes: set[str] = set()  # volumes where a small write gets ENOSPC
        self.asked: list[str] = []

    def install(self, loop: BoardLoop) -> None:
        loop._disk_volume = lambda path: self.volumes[path]
        loop._disk_free = self._free
        loop._disk_full = lambda path: self.volumes[path] in self.refuses_writes

    def _free(self, path):
        self.asked.append(path)
        return self.free[self.volumes[path]]


def _loop(monkeypatch, board: Board, disk: _Disk | None = None, **cfg) -> BoardLoop:
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    loop = BoardLoop({"coder": "proto", "local_gate_cmd": "", "max_concurrent": 5, **cfg})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: object())

    async def _nothing():
        return None

    # A real `_tick`, minus the phases that need a whole board (PR reconcile, the health
    # sweep, the gate preflight): the host-disk phase and the claim scan run as shipped.
    for phase in ("_maybe_reconcile", "_maybe_sweep", "_maybe_preflight"):
        monkeypatch.setattr(loop, phase, _nothing)
    if disk is not None:
        disk.install(loop)
    return loop


def _stub_drives(monkeypatch, loop: BoardLoop) -> list[str]:
    """Claimed cards are recorded instead of driven: these cases are about the claim."""
    driven: list[str] = []

    async def _drive(feature):
        driven.append(feature["id"])

    monkeypatch.setattr(loop, "_drive", _drive)
    return driven


async def _scan(loop: BoardLoop) -> dict:
    await loop._spawn_ready()
    if loop._drives:
        await asyncio.gather(*list(loop._drives))
        await asyncio.sleep(0)  # let the done-callbacks release the drive slots
    return loop._last_claim_decision


def _pending(rows: list) -> list:
    return [r for r in rows if not r["delivered_at"]]


# ── the pieces ────────────────────────────────────────────────────────────────────────


def test_free_bytes_reads_disk_usage_and_answers_for_a_path_not_made_yet(tmp_path):
    asked = []

    def usage(path):
        asked.append(path)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return SimpleNamespace(free=7 * GB)

    assert host_disk.free_bytes(str(tmp_path / "not" / "there"), usage) == 7 * GB
    assert asked[-1] == str(tmp_path)  # the nearest existing parent: the same volume
    assert host_disk.free_bytes(str(tmp_path)) > 0  # the real probe

    def broken(path):
        raise PermissionError(path)

    assert host_disk.free_bytes(str(tmp_path), broken) is None  # unknown, never "full"


@pytest.mark.parametrize(
    ("cfg", "gb"),
    [({}, 5), ({"min_free_disk_gb": 12}, 12), ({"min_free_disk_gb": "2.5"}, 2.5), ({"min_free_disk_gb": 0}, 0)]
    + [({"min_free_disk_gb": bad}, 5) for bad in ("lots", None, True, float("nan"))]
    + [({"min_free_disk_gb": -3}, 0)],
)
def test_the_threshold_knob_defaults_to_five_and_zero_switches_it_off(cfg, gb):
    assert host_disk.min_free_bytes(cfg) == int(gb * GB)


def test_the_knob_is_read_live():
    loop = BoardLoop({})
    loop._disk_volume, loop._disk_free = (lambda path: VOL_A), (lambda path: 3 * GB)
    assert loop._host_disk_probe("/repo") == (VOL_A, True, 3 * GB)
    loop.cfg["min_free_disk_gb"] = 2  # a reload: no restart
    assert loop._host_disk_probe("/repo") == (VOL_A, False, 3 * GB)


def test_volume_of_is_a_mount_point_shared_by_everything_under_it(tmp_path):
    volume = host_disk.volume_of(str(tmp_path / "repo" / "not-made-yet"))
    assert os.path.ismount(volume)
    assert host_disk.volume_of(str(tmp_path)) == volume


def test_the_page_leads_with_the_cause_and_the_fix_then_lists_the_cards():
    text = host_disk.page_text(int(1.26 * GB), {"bd-1": TITLES["bd-1"], "bd-2": TITLES["bd-2"]})
    lead, *cards = text.splitlines()
    assert lead == (
        "This computer is almost out of disk space (1.3 GB free). 2 cards can't start until there is more "
        "room. Free up space and they will start again on their own. Waiting:"
    )
    assert "bd-" not in lead and HOST_DISK_CLASS not in lead  # no card id or class in the lead
    assert cards == ["- bd-1: Trim the retry log", "- bd-2: Paginate the audit feed"]

    one = host_disk.page_text(None, {"bd-1": ""}).splitlines()
    assert one[0].startswith("This computer is almost out of disk space. 1 card can't start until")
    assert "it will start again on its own" in one[0] and one[1] == "- bd-1"

    many = host_disk.page_text(GB, {f"bd-{n}": "t" for n in range(14)}).splitlines()
    assert len(many) == 12 and many[-1] == "- and 4 more"


# ── r1: the claim scan ────────────────────────────────────────────────────────────────


async def test_a_volume_under_the_threshold_claims_nothing_and_other_volumes_go_ahead(monkeypatch, inbox):
    board = Board({"bd-1": "/a/one", "bd-2": "/a/two", "bd-3": "/b/three"})
    disk = _Disk({"/a/one": VOL_A, "/a/two": VOL_A, "/b/three": VOL_B})
    disk.free[VOL_A] = 1 * GB
    loop = _loop(monkeypatch, board, disk, ready_skip_max=2)
    driven = _stub_drives(monkeypatch, loop)

    decision = await _scan(loop)

    assert decision["selected"] == ["bd-3"] and driven == ["bd-3"]  # the other volume was scanned and claimed
    assert [(s["fid"], s["reason"], s["volume"]) for s in decision["skipped"]] == [
        ("bd-1", "host-disk", VOL_A),
        ("bd-2", "host-disk", VOL_A),
    ]
    assert decision["skipped"][0]["free_gb"] == 1.0
    assert board.states() == {"bd-1": "ready", "bd-2": "ready", "bd-3": "in_progress"}

    # One page for the volume, naming the two cards that wait on it.
    await loop._tick()
    (page,) = _pending(inbox)
    assert page["dedup_key"] == f"host-disk:{VOL_A}" and page["source"] == "project_board"
    assert "disk space (1.0 GB free). 2 cards can't start" in page["text"]
    assert "- bd-1: Trim the retry log" in page["text"] and "bd-3" not in page["text"]

    # Waiting for room is not a livelock: well past `ready_skip_max`, nothing is blocked,
    # and the operator is not paged again.
    for _ in range(4):
        await loop._tick()
    assert board.states()["bd-1"] == board.states()["bd-2"] == "ready"
    assert len(inbox) == 1

    # Room again: the page is retired and the same tick's scan claims the two cards.
    disk.free[VOL_A] = 40 * GB
    await loop._tick()
    await asyncio.gather(*list(loop._drives))
    assert _pending(inbox) == [] and loop._host_disk_state() == {}
    assert sorted(driven) == ["bd-1", "bd-2", "bd-3"]


# ── r4: the switch ────────────────────────────────────────────────────────────────────


async def test_min_free_disk_gb_zero_disables_the_check(monkeypatch, inbox):
    board = Board({"bd-1": "/a/one", "bd-2": "/a/two"})
    disk = _Disk({"/a/one": VOL_A, "/a/two": VOL_A})
    disk.free[VOL_A] = 0  # completely full, by the probe
    loop = _loop(monkeypatch, board, disk, min_free_disk_gb=0)
    driven = _stub_drives(monkeypatch, loop)

    decision = await _scan(loop)
    await loop._host_disk_tick()

    assert sorted(driven) == ["bd-1", "bd-2"] and decision["skipped"] == []
    assert disk.asked == []  # switched off means the space is not even read
    assert inbox == [] and loop._host_disk_state() == {}


# ── r2 + r3: real git, three cards, one page, and the release ─────────────────────────


@posix_only
async def test_three_disk_full_drives_hold_their_cards_page_once_and_come_back(
    monkeypatch, inbox, clone, full_disk_git
):
    board = Board({fid: clone for fid in TITLES})
    # The free-space reading still shows room (inodes, a quota): the host confirms the
    # condition by refusing a small write, as git's own failed.
    disk = _Disk({clone: VOL_A})
    disk.refuses_writes.add(VOL_A)
    loop = _loop(monkeypatch, board, disk)
    dispatched = []

    async def _dispatch(coder, tree, prompt, *, timeout=None, env_passthrough=()):
        dispatched.append(tree)

    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)

    decision = await _scan(loop)  # claims all three; each fails at `git worktree add`
    assert sorted(decision["selected"]) == sorted(TITLES) and dispatched == []

    # Held, not blocked terminal: back to ready under the `host-disk` hold.
    for fid in TITLES:
        card = board.get_feature(fid)
        assert card["blocked_class"] == HOST_DISK_CLASS, card
        assert card["bead_status"] == "open" and "ready" in card["labels"]
        assert card["blocked_reason"].startswith(host_disk.HOLD_REASON_PREFIX)
        assert "No space left on device" in card["blocked_reason"]
    assert not loop._inflight_files and not loop._inflight

    # Exactly ONE page, keyed on the host condition and in plain words.
    await loop._tick()
    (page,) = inbox
    assert page["dedup_key"] == f"host-disk:{VOL_A}"
    lead = page["text"].splitlines()[0]
    assert "disk space" in lead and "3 cards can't start" in lead
    assert not any(fid in lead for fid in TITLES) and HOST_DISK_CLASS not in lead and "terminal" not in page["text"]
    assert all(f"- {fid}: {title}" in page["text"] for fid, title in TITLES.items())

    # Neither the blocked sweep nor a direct block alert adds a per-card page, and the
    # held volume takes no new claim.
    await loop._recover_blocked(board)
    loop._notify_operator("bd-1", "host-disk: full", incident="host-disk|full|0", store=board)
    assert (await _scan(loop))["selected"] == []
    assert len(inbox) == 1 and dispatched == []
    disk.refuses_writes.clear()

    # The probe now measures the volume under the threshold: still held, still one page.
    disk.free[VOL_A] = 2 * GB
    await loop._tick()
    assert set(board.states().values()) == {"blocked"} and len(_pending(inbox)) == 1

    # Space is back. No manual unblock: the tick releases the cards and retires the page.
    disk.free[VOL_A] = 60 * GB
    stopped = _stub_drives(monkeypatch, loop)  # the released cards are claimed at once
    await loop._tick()
    await asyncio.gather(*list(loop._drives))
    assert sorted(stopped) == sorted(TITLES)
    for fid in TITLES:
        card = board.get_feature(fid)
        assert not card["blocked"] and card["blocked_class"] == ""
        assert any(c.startswith("unblocked — cleared") for c in board.comments[fid])
    assert _pending(inbox) == [] and inbox[0]["delivered_at"] == "now"
    assert loop._host_disk_state() == {}


async def test_the_release_returns_held_cards_to_ready(monkeypatch, inbox):
    """r3 on its own, with nothing claiming behind the release: held → ready."""
    board = Board({"bd-1": "/a/one", "bd-2": "/a/two"})
    disk = _Disk({"/a/one": VOL_A, "/a/two": VOL_A})
    disk.free[VOL_A] = GB
    loop = _loop(monkeypatch, board, disk)
    for fid in board.beads:
        board.claim(fid, assignee="proto")
        board.flag_blocked(fid, HELD_REASON, automatic=True)
        await loop._host_disk_hold(board, board.get_feature(fid), board.repos[fid])
    assert set(board.states().values()) == {"blocked"}

    await loop._host_disk_tick()
    assert len(_pending(inbox)) == 1 and set(board.states().values()) == {"blocked"}

    # An operator's own hold that replaced the loop's is not the loop's to lift.
    board.flag_blocked("bd-2", "waiting on the design review", category="agent-hold", automatic=True)
    disk.free[VOL_A] = 50 * GB
    await loop._host_disk_tick()
    assert board.states() == {"bd-1": "ready", "bd-2": "blocked"}
    assert board.get_feature("bd-2")["blocked_class"] == "agent-hold"
    assert _pending(inbox) == [] and loop._host_disk_state() == {}


async def test_a_hold_the_probe_cannot_confirm_is_retried_and_its_page_waits_for_proof(monkeypatch, inbox):
    """A quota or an exhausted inode table fails `worktree add` with room to spare by the
    probe. The hold still stands, is retried after RETRY_S, and the page is retired only
    once a retry has gone RETRY_S without failing again — never re-sent every cycle."""
    board = Board({"bd-1": "/a/one"})
    disk = _Disk({"/a/one": VOL_A})
    now = [1000.0]
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    loop = BoardLoop({"coder": "proto"}, monotonic_clock=lambda: now[0])
    disk.install(loop)

    async def _hold():
        board.claim("bd-1", assignee="proto")
        board.flag_blocked("bd-1", f"{host_disk.HOLD_REASON_PREFIX}Disk quota exceeded", automatic=True)
        await loop._host_disk_hold(board, board.get_feature("bd-1"), "/a/one")

    await _hold()
    await loop._host_disk_tick()
    (page,) = _pending(inbox)
    assert "disk space. 1 card can't start" in page["text"]  # no free-space figure: none was low
    assert board.states() == {"bd-1": "blocked"}

    now[0] += host_disk.RETRY_S + 1
    await loop._host_disk_tick()
    assert board.states() == {"bd-1": "ready"} and len(_pending(inbox)) == 1  # retried; page kept

    await _hold()  # the retry failed the same way
    await loop._host_disk_tick()
    assert board.states() == {"bd-1": "blocked"} and len(inbox) == 1  # the same page, not a second

    now[0] += host_disk.RETRY_S + 1
    await loop._host_disk_tick()  # retried again…
    now[0] += host_disk.RETRY_S + 1
    await loop._host_disk_tick()  # …and this time nothing failed
    assert board.states() == {"bd-1": "ready"} and _pending(inbox) == [] and loop._host_disk_state() == {}


async def test_a_card_held_before_a_restart_is_adopted_by_the_sweep_and_released(monkeypatch, inbox):
    board = Board({"bd-1": "/a/one"})
    board.claim("bd-1", assignee="proto")
    board.flag_blocked("bd-1", HELD_REASON, automatic=True)
    board.requeue("bd-1")
    disk = _Disk({"/a/one": VOL_A})
    loop = _loop(monkeypatch, board, disk)  # a fresh process: nothing tracked, and room by now
    await loop._recover_blocked(board)  # known as the loop's hold by its reason alone
    assert loop._host_disk_state()[VOL_A]["held"] == {"bd-1": TITLES["bd-1"]} and inbox == []

    disk.free[VOL_A] = GB
    loop = _loop(monkeypatch, board, disk)

    await loop._recover_blocked(board)
    assert inbox == []  # the sweep sends no per-card page for it
    assert loop._host_disk_state()[VOL_A]["held"] == {"bd-1": TITLES["bd-1"]}
    await loop._host_disk_tick()
    assert [r["dedup_key"] for r in _pending(inbox)] == [f"host-disk:{VOL_A}"]

    disk.free[VOL_A] = 30 * GB
    await loop._host_disk_tick()
    assert board.states() == {"bd-1": "ready"} and _pending(inbox) == []


async def test_a_failure_is_the_hosts_only_when_the_host_confirms_it(monkeypatch):
    disk = _Disk({"/a/one": VOL_A})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: None)
    loop = BoardLoop({})
    disk.install(loop)
    prep = worktree.EnvironmentPreparationError("setup_cmd failed: exit 1")
    enospc = worktree.WorktreeError("worktree add failed: ENOSPC")

    # Room to spare and writes go through: the words alone hold nothing.
    assert not await loop._host_disk_failure(prep, "/a/one")
    assert not await loop._host_disk_failure(enospc, "/a/one")
    quoting = worktree.WorktreeError("gate failed: test_enospc expected 'No space left on device' in stderr")
    assert not await loop._host_disk_failure(quoting, "/a/one")

    disk.free[VOL_A] = GB  # the free space reads under the threshold
    assert await loop._host_disk_failure(prep, "/a/one")
    assert await loop._host_disk_failure(enospc, "/a/one")
    # A failure that does not point at the disk stays its own, however full the volume.
    assert not await loop._host_disk_failure(worktree.WorktreeError("gate failed: 2 tests red"), "/a/one")
    # A coder's own words are never read as the host's condition.
    quoted = worktree.NoChangesError("no commits. Coder said: No space left on device")
    assert not await loop._host_disk_failure(quoted, "/a/one")

    disk.free[VOL_A] = 100 * GB
    disk.refuses_writes.add(VOL_A)  # room by the reading, but the OS refuses the write
    assert await loop._host_disk_failure(prep, "/a/one") and await loop._host_disk_failure(enospc, "/a/one")

    def _broken(path):
        raise RuntimeError("cannot test a write")

    loop._disk_full = _broken  # a check that cannot be made confirms nothing
    assert not await loop._host_disk_failure(enospc, "/a/one")


def test_out_of_room_is_the_os_refusing_a_write_for_lack_of_space(tmp_path):
    checkout = tmp_path / "clone"
    (checkout / ".git").mkdir(parents=True)
    wrote: list[str] = []

    def refuse(code):
        def _write(directory):
            wrote.append(directory)
            raise OSError(code, os.strerror(code))

        return _write

    assert host_disk.out_of_room(str(checkout), write=refuse(errno.ENOSPC))
    assert wrote == [str(checkout / ".git")]  # in the admin directory, never the working tree
    if hasattr(errno, "EDQUOT"):
        assert host_disk.out_of_room(str(checkout), write=refuse(errno.EDQUOT))
    # Refused for another reason, or not refused at all: no evidence of a full disk.
    assert not host_disk.out_of_room(str(checkout), write=refuse(errno.EACCES))
    assert not host_disk.out_of_room(str(checkout), write=wrote.append)
    # A path not made yet is answered by its nearest existing parent.
    assert host_disk.out_of_room(str(tmp_path / "later" / "wt"), write=refuse(errno.ENOSPC))
    assert wrote[-1] == str(tmp_path)
    # The real write, on a disk with room, leaves nothing behind.
    assert not host_disk.out_of_room(str(checkout))
    assert list((checkout / ".git").iterdir()) == []


@posix_only
async def test_a_disk_full_error_on_a_disk_with_room_keeps_its_ordinary_block_and_page(
    monkeypatch, inbox, clone, full_disk_git
):
    """The review case: the failure's text says "No space left on device", the host says
    there is room (by the reading and by a write). No hold, no retry loop, no host-disk
    page: the card stays blocked for a human and gets its own page, as before the hold."""
    board = Board({"bd-1": clone})
    disk = _Disk({clone: VOL_A})
    loop = _loop(monkeypatch, board, disk)

    decision = await _scan(loop)
    assert decision["selected"] == ["bd-1"]

    card = board.get_feature("bd-1")
    assert card["board_state"] == "blocked" and card["bead_status"] == "in_progress"  # not back to ready
    assert not card["blocked_reason"].startswith(host_disk.HOLD_REASON_PREFIX)
    assert loop._host_disk_state() == {} and not loop._host_disk_holds(card)

    # The sweep does not adopt it into a hold, and the operator is paged for the card.
    now = [1000.0]
    loop._monotonic_clock = lambda: now[0]
    await loop._recover_blocked(board)
    await loop._tick()
    assert loop._host_disk_state() == {}
    assert [r["dedup_key"].split(":")[0] for r in inbox] == ["blocked"] and "bd-1" in inbox[0]["dedup_key"]

    # Nothing retries it, however long it waits, and it is not paged again.
    now[0] += 10 * host_disk.RETRY_S
    await loop._tick()
    await loop._recover_blocked(board)
    assert board.get_feature("bd-1")["bead_status"] == "in_progress" and len(inbox) == 1


@pytest.mark.parametrize("missing_reason", [False, True])
async def test_a_block_that_only_quotes_a_disk_error_is_not_adopted_as_a_hold(monkeypatch, inbox, missing_reason):
    """Text-only blocks keep their pages even beside a genuine hold on a full volume."""
    board = Board({"bd-1": "/a/one", "bd-2": "/a/two", "bd-3": "/a/three"})
    for fid in board.beads:
        board.claim(fid, assignee="proto")
        reason = HELD_REASON if fid == "bd-3" else "gate output: No space left on device"
        board.flag_blocked(fid, reason, automatic=True)
        assert board.get_feature(fid)["blocked_class"] == HOST_DISK_CLASS
    board.requeue("bd-3")
    if missing_reason:
        list_features = board.list_features
        monkeypatch.setattr(
            board, "list_features",
            lambda *args, **kwargs: [{**f, "blocked_reason": ""} for f in list_features(*args, **kwargs)],
        )
    disk = _Disk({"/a/one": VOL_A, "/a/two": VOL_B, "/a/three": VOL_B})
    disk.free[VOL_B] = GB
    loop = _loop(monkeypatch, board, disk)

    await loop._recover_blocked(board)
    await loop._host_disk_tick()

    assert set(loop._host_disk_state()) == {VOL_B}
    assert loop._host_disk_state()[VOL_B]["held"].keys() == {"bd-3"}
    pending = _pending(inbox)
    card_pages = [r for r in pending if r["dedup_key"].startswith("blocked:")]
    assert len(card_pages) == 2
    for fid in ("bd-1", "bd-2"):
        assert any(fid in r["dedup_key"] for r in card_pages)
        assert not loop._host_disk_holds(board.get_feature(fid))
    assert [r["dedup_key"] for r in pending if r not in card_pages] == [f"host-disk:{VOL_B}"]
    assert loop._host_disk_holds(board.get_feature("bd-3"))

    disk.free[VOL_B] = 50 * GB
    await loop._host_disk_tick()
    await loop._recover_blocked(board)
    assert board.states() == {"bd-1": "blocked", "bd-2": "blocked", "bd-3": "ready"}
    assert _pending(inbox) == card_pages
    assert loop._host_disk_state() == {}
