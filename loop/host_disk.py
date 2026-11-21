"""Hold dispatch while the host disk is nearly full, with one self-clearing page (#801).

A full disk is the HOST's condition, not a card's. On 2026-11-20 three cards (bd-o7oyw,
bd-tndqq, bd-0vvum) each claimed, failed at ``git worktree add`` with "No space left on
device", blocked ``terminal`` and paged the operator separately, and none of them came back
when the disk was cleared.

Now the loop treats it the way it treats a project whose gate preflight is red:

- before a claim, the free space on the volume holding the card's project checkout (where
  its worktrees are made) is read. Under ``min_free_disk_gb`` nothing on that volume is
  claimed, and the skip says ``host-disk``; cards on other volumes go ahead;
- a drive that fails with the ``host-disk`` class (or while preparing its tree on a volume
  under the threshold) puts its card back to ready with a ``host-disk`` hold, never a
  terminal block;
- the operator gets ONE inbox page per volume (dedup key ``host-disk:<volume>``), in plain
  words, listing the waiting cards;
- each tick, once the space is back, the held cards are released and the page is retired.

A failure the probe cannot see (a quota, exhausted inodes, no temp directory, or the check
switched off) still holds its volume. With no reading to wait for, the hold is retried
after ``RETRY_S``; the page stays up until a retry goes that long without failing again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

from .. import block_pages
from ..failures import HOST_DISK_CLASS, classify

log = logging.getLogger("protoagent.plugins.project_board")

GB = 1024**3
#: Free space under which a volume takes no new work, in GB. The `min_free_disk_gb` cfg
#: key; 0 switches the check off.
MIN_FREE_DISK_GB_DEFAULT = 5.0
#: How long a hold the probe cannot confirm waits before its cards are tried again, and
#: how long its page then stays up waiting to see that the retry did not fail the same way.
RETRY_S = 300.0
PAGE_KEY_PREFIX = "host-disk:"
#: What moves a card held for disk space — for the board's own "what is held" answers.
HOLD_STEP = "free up disk space on this computer; the cards start again on their own"
_PAGE_CARDS_MAX = 10


def free_bytes(path: str, usage=None) -> int | None:
    """Bytes free on the volume holding ``path``, or None when it cannot be read. A path
    that does not exist yet is answered by its nearest existing parent (the same volume).
    ``usage`` replaces ``shutil.disk_usage`` in tests."""
    probe = usage or shutil.disk_usage
    path = os.path.abspath(path or ".")
    while True:
        try:
            return int(probe(path).free)
        except FileNotFoundError:
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent
        except Exception:  # noqa: BLE001 — an unreadable volume is unknown, never "full"
            return None


def min_free_bytes(cfg: dict | None) -> int:
    """The `min_free_disk_gb` threshold in bytes — 0 when the check is switched off (0 or
    less). A value that is not a number falls back to the default. Read on every use, so a
    config reload takes effect without a restart."""
    raw = (cfg or {}).get("min_free_disk_gb", MIN_FREE_DISK_GB_DEFAULT)
    try:
        gb = float(MIN_FREE_DISK_GB_DEFAULT if raw is None or isinstance(raw, bool) else raw)
    except (TypeError, ValueError):
        gb = MIN_FREE_DISK_GB_DEFAULT
    if gb != gb:  # NaN
        gb = MIN_FREE_DISK_GB_DEFAULT
    return int(gb * GB) if gb > 0 else 0


def volume_of(path: str) -> str:
    """The mount point of the volume holding ``path`` — what a host-disk hold and its page
    are keyed on, so every project on one disk shares one hold. Falls back to the path
    itself when the filesystem cannot be read."""
    path = os.path.abspath(path or ".")
    try:
        while not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                return path
            path = parent
        path = os.path.realpath(path)
        while not os.path.ismount(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    except OSError:
        pass
    return path


def page_key(volume: str) -> str:
    return f"{PAGE_KEY_PREFIX}{volume}"


def page_text(free: int | None, waiting: dict[str, str]) -> str:
    """The one page for a volume that is out of room: the cause and the fix in plain words,
    then the waiting cards. No card id, class or git error in the lead. ``free`` is None
    when the space was not measured as low (the failure itself was the evidence)."""
    k = len(waiting)
    room = f" ({free / GB:.1f} GB free)" if free is not None else ""
    if k == 1:
        cards, again = "1 card can't start", "it will start again on its own"
    else:
        cards, again = f"{k} cards can't start", "they will start again on their own"
    lead = (
        f"This computer is almost out of disk space{room}. {cards} until there is more room. Free up space and {again}."
    )
    lines = []
    for fid, title in list(waiting.items())[:_PAGE_CARDS_MAX]:
        title = " ".join(str(title or "").split())
        if len(title) > 55:
            title = title[:54].rstrip() + "…"
        lines.append(f"- {fid}: {title}" if title else f"- {fid}")
    if k > _PAGE_CARDS_MAX:
        lines.append(f"- and {k - _PAGE_CARDS_MAX} more")
    return "\n".join([lead + (" Waiting:" if lines else ""), *lines])


def _pending_pages(inbox, volume: str) -> list:
    key = page_key(volume)
    return [
        r["id"] for r in block_pages.pending(inbox) if r.get("source") == "project_board" and r.get("dedup_key") == key
    ]


def send_page(volume: str, free: int | None, waiting: dict[str, str]) -> bool:
    """Put the volume's page in the operator's inbox unless one is already waiting there.
    True when the operator has it (sent now or before); False when no inbox took it."""
    inbox = block_pages.inbox_store()
    if inbox is None:
        raise RuntimeError("no resolvable inbox store for this instance")
    if hasattr(inbox, "list") and _pending_pages(inbox, volume):
        return True
    inbox.add(page_text(free, waiting), priority="now", source="project_board", dedup_key=page_key(volume))
    return True


def retire_page(volume: str) -> bool:
    """Mark the volume's page delivered: the space is back, nothing is left to ask. True
    when no page is left pending (or the host has no inbox); False when the inbox could not
    be read or written, so the caller tries again. Never raises."""
    try:
        inbox = block_pages.inbox_store()
        if inbox is None:
            return True
        ids = _pending_pages(inbox, volume)
        if ids:
            inbox.mark_delivered(ids)
            log.info("[project_board] host-disk: retired the disk-space page for %s", volume)
        return True
    except ImportError:
        return True  # Host without an inbox: nothing was ever sent.
    except Exception:  # noqa: BLE001
        log.warning("[project_board] host-disk: could not retire the page for %s; retrying", volume, exc_info=True)
        return False


def _new_volume(path: str) -> dict:
    return {
        "path": path,  # a checkout on the volume: what the probe reads
        "held": {},  # fid → title: cards this loop holds blocked for this volume
        "waiting": {},  # fid → title: ready cards the last claim scan passed over
        "seen_low": False,  # the probe itself has measured this volume under the threshold
        "failed_at": 0.0,  # monotonic time of the newest host-disk failure on it
        "probation_until": 0.0,  # an unconfirmed hold was retried: keep the page until then
        "free": None,
        "paged": False,
        "page_warned": False,
    }


class HostDiskMixin:
    """The loop's host-disk hold. State is per volume (``_new_volume``) and lives in the
    process; a restart re-adopts held cards from their ``host-disk`` class (the blocked
    sweep calls ``_host_disk_adopt``)."""

    # ── seams (tests replace these on the instance) ───────────────────────────────
    def _disk_free(self, path: str) -> int | None:
        return free_bytes(path)

    def _disk_volume(self, path: str) -> str:
        return volume_of(path)

    def _host_disk_now(self) -> float:
        return getattr(self, "_monotonic_clock", time.monotonic)()

    def _host_disk_state(self) -> dict[str, dict]:
        vols = getattr(self, "_host_disk_volumes", None)
        if vols is None:
            vols = self._host_disk_volumes = {}
        return vols

    def _host_disk_probe(self, path: str) -> tuple[str, bool, int | None]:
        """``(volume, low, free)`` for the volume holding ``path``. With the check off
        (``min_free_disk_gb`` 0) the space is not read at all and nothing is low."""
        volume = self._disk_volume(path)
        threshold = min_free_bytes(getattr(self, "cfg", None))
        if not threshold:
            return volume, False, None
        free = self._disk_free(path)
        return volume, free is not None and free < threshold, free

    async def _host_disk_probe_safe(self, path: str) -> tuple[str, bool, int | None]:
        """The probe off the event loop (a stat on a slow mount must not stall it). A probe
        that raises reads as "not low": an unknown volume never holds work."""
        try:
            return await asyncio.to_thread(self._host_disk_probe, path)
        except Exception:  # noqa: BLE001
            log.debug("[project_board] host-disk: could not read free space for %s", path, exc_info=True)
            return "", False, None

    # ── the claim scan ────────────────────────────────────────────────────────────
    def _host_disk_scan_start(self) -> None:
        """A claim scan rebuilds each volume's list of passed-over ready cards."""
        for st in self._host_disk_state().values():
            st["waiting"] = {}

    async def _host_disk_claim_skip(self, candidate: dict, probed: dict) -> dict | None:
        """The ``host-disk`` skip record for a ready candidate whose volume takes no new
        work — it is under the threshold, or it holds cards that failed for disk space —
        else None. ``probed`` caches one reading per checkout for the scan."""
        repo = self._repo_for(candidate)
        if repo not in probed:
            probed[repo] = await self._host_disk_probe_safe(repo)
        volume, low, free = probed[repo]
        vols = self._host_disk_state()
        st = vols.get(volume)
        if not low and not (st and st["held"]):
            return None
        if st is None:
            st = vols[volume] = _new_volume(repo)
        if low:
            st["seen_low"] = True
            st["free"] = free
        st["waiting"][candidate["id"]] = str(candidate.get("title") or "")
        skip = {"fid": candidate["id"], "reason": HOST_DISK_CLASS, "volume": volume}
        if free is not None:
            skip["free_gb"] = round(free / GB, 1)
        return skip

    # ── a drive that failed for disk space ────────────────────────────────────────
    async def _host_disk_failure(self, exc: BaseException, repo: str) -> bool:
        """Whether a drive's failure is the host running out of room: its error is the
        ``host-disk`` class, or it could not prepare its tree while the volume is under the
        threshold. Never for a failure whose text is the coder's own words (no commits, a
        refusal) or for stranded work, which keeps its own class and its tree."""
        from .. import worktree
        from .. import coder_seam

        if isinstance(exc, (worktree.NoChangesError, worktree.StrandedWorkError, coder_seam.SolveRefused)):
            return False
        if classify(str(exc)).category == HOST_DISK_CLASS:
            return True
        if isinstance(exc, worktree.EnvironmentPreparationError):
            return (await self._host_disk_probe_safe(repo))[1]
        return False

    async def _host_disk_hold(self, store, feature: dict, repo: str) -> None:
        """Record a card the drive just blocked ``host-disk`` as held for its volume, and
        put it back to ready under that hold (the shape of a preflight hold: ready, flagged
        blocked, released by the loop). A requeue that fails leaves the card blocked and
        tracked; the release requeues it then."""
        fid = feature["id"]
        try:
            await asyncio.to_thread(store.requeue, fid)
        except Exception:  # noqa: BLE001 — the hold stands; the release finishes the move
            log.warning("[project_board] host-disk: could not return %s to ready under its hold", fid, exc_info=True)
        volume, low, free = await self._host_disk_probe_safe(repo)
        st = self._host_disk_state().setdefault(volume, _new_volume(repo))
        st["held"][fid] = str(feature.get("title") or "")
        st["failed_at"] = self._host_disk_now()
        st["probation_until"] = 0.0
        if low:
            st["seen_low"] = True
            st["free"] = free
        log.warning(
            "[project_board] %s held (%s): the volume %s is out of room — it starts again once space is freed",
            fid,
            HOST_DISK_CLASS,
            volume or repo,
        )

    def _host_disk_adopt(self, feature: dict) -> None:
        """Track a card found blocked ``host-disk`` that this process is not holding yet —
        one a previous run held, or one blocked on a disk-full error outside a drive. It is
        released on the next tick that finds its volume with room."""
        fid = feature.get("id")
        if not fid:
            return
        vols = self._host_disk_state()
        if any(fid in st["held"] for st in vols.values()):
            return
        try:
            repo = self._repo_for(feature)
            volume = self._disk_volume(repo)
        except Exception:  # noqa: BLE001 — adoption is best-effort; the next sweep retries
            log.debug("[project_board] host-disk: could not place %s on a volume", fid, exc_info=True)
            return
        vols.setdefault(volume, _new_volume(repo))["held"][fid] = str(feature.get("title") or "")
        log.info("[project_board] host-disk: tracking held card %s on %s", fid, volume)

    # ── every tick: page while it lasts, release when it clears ───────────────────
    async def _host_disk_tick(self) -> None:
        vols = self._host_disk_state()
        for volume, st in list(vols.items()):
            _volume, low, free = await self._host_disk_probe_safe(st["path"])
            now = self._host_disk_now()
            st["free"] = free
            if low:
                st["seen_low"] = True
            # A hold the probe never confirmed has no reading to wait for: give it RETRY_S.
            cooling = bool(st["held"]) and not st["seen_low"] and now - st["failed_at"] < RETRY_S
            if low or cooling:
                waiting = {**st["waiting"], **st["held"]}
                if waiting and not st["paged"]:
                    st["paged"] = await asyncio.to_thread(self._host_disk_page, volume, st, free if low else None)
                continue
            if st["held"]:
                confirmed = st["seen_low"]
                fids = list(st["held"])
                await asyncio.to_thread(self._host_disk_release, volume, fids)
                for fid in fids:
                    st["held"].pop(fid, None)
                st["waiting"] = {}
                st["seen_low"] = False
                if not confirmed:
                    st["probation_until"] = now + RETRY_S
            if st["held"] or st["probation_until"] > now:
                continue  # held again meanwhile, or a retry still has to prove itself
            if await asyncio.to_thread(retire_page, volume) and not st["held"]:
                vols.pop(volume, None)

    def _host_disk_page(self, volume: str, st: dict, free: int | None) -> bool:
        """Send the volume's one page. Feature-detected like every inbox write: a host
        without an inbox gets one WARNING per hold instead."""
        waiting = {**st["waiting"], **st["held"]}
        try:
            sent = send_page(volume, free, waiting)
            log.warning(
                "[project_board] host-disk: %s is almost out of disk space — %d card(s) waiting; operator notified",
                volume,
                len(waiting),
            )
            return sent
        except Exception:  # noqa: BLE001 — no inbox seam, or it refused; say so loudly, once
            if not st["page_warned"]:
                st["page_warned"] = True
                log.warning(
                    "[project_board] host-disk: %s is almost out of disk space and %d card(s) are waiting "
                    "(no operator inbox reachable): %s",
                    volume,
                    len(waiting),
                    ", ".join(waiting),
                    exc_info=True,
                )
            return False

    def _host_disk_release(self, volume: str, fids: list[str]) -> None:
        """Lift this loop's host-disk hold on ``fids`` so they are ready again. Only a card
        still carrying the ``host-disk`` class is touched: an operator or agent hold that
        replaced it stays. A card whose return to ready failed at hold time (still
        in_progress, with no drive) is requeued here."""
        store = self._store()
        for fid in fids:
            try:
                feature = store.get_feature(fid)
                if not feature or feature.get("blocked_class") != HOST_DISK_CLASS:
                    continue
                cleared = store.clear_blocked(fid) or {}
                if cleared.get("board_state") == "in_progress" and fid not in getattr(self, "_inflight_files", {}):
                    store.requeue(fid)
                log.info("[project_board] host-disk: %s has room again — released %s to ready", volume, fid)
            except Exception:  # noqa: BLE001 — best-effort per card; the sweep re-adopts a card left held
                log.warning("[project_board] host-disk release: could not release %s", fid, exc_info=True)
