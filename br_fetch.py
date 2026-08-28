"""`br` fetched on first run (v0.43.0).

Before this, a host without the beads CLI on PATH was a ``br`` setup gap: the loop
paused, the operator was told to ``cargo install beads_rust`` and restart. A fresh
Project Manager archetype member should not need a Rust toolchain to get its board
store — so when ``br`` is not on PATH (and ``project_board.br_autofetch`` is on, the
default) the plugin fetches the PINNED beads-rust release binary for this platform
into the instance's writable plugin-data dir, verifies its sha256 against the pins
recorded here (the same pin + checksum discipline as ``.github/workflows/ci.yml``),
marks it executable, and switches the store to it — in place, no restart.

Resolution order for the binary the store shells (``store.BR``)::

    explicit BR_BIN env  >  the fetched binary  >  ``br`` on PATH

The fetch runs OFF the event loop (a daemon thread), ONCE per process, bounded by
``FETCH_TIMEOUT_S``; its state lives in a process-stable ``sys.modules`` slot so a
plugin reload neither re-fetches nor loses a fetch in flight. A failure is a ``br``
setup gap with the download error in the hint (never a traceback) and the install
hint as the fallback — ``setup_check`` renders the state; this module never logs
above WARNING.

Egress: the download is a plain HTTPS GET of a GitHub release asset. The host's
in-process egress allowlist (``security.egress``, ADR 0008) is consulted first when
importable, so a deny-by-default deployment gets the allowlist's own message instead
of a socket error — ``github.com`` AND the redirect target
``release-assets.githubusercontent.com`` (verified live; the 302 goes there, not to
``objects.githubusercontent.com``) must be reachable. Every redirect hop is checked
too (``_PinnedRedirects``): the allowlist verdict on each ``Location``, and the hop
must stay on ``*.githubusercontent.com``. A host that exposes ``host.fetch_bytes(url,
timeout=)`` is used in preference to ``urllib``; none does today, so the guard is
a ``getattr``.

The install path is keyed by version — ``<data>/bin/<BR_VERSION>/br`` — so bumping
the pin fetches the new release instead of silently keeping the old binary, and the
version the state reports is the one the path was built for, never the pin over a
stale file. Delete ``<data>/bin`` to force a re-fetch on the next restart.

Windows is NOT supported by the auto-fetch (no pin recorded; the store's ``br``
invocation would need ``.exe`` handling too), and neither is musl (Alpine: the
pinned assets are glibc builds) — the gap hint says so and points at the manual
install.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform as _platform
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import types
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("protoagent.plugins.project_board")

# ── the pin ──────────────────────────────────────────────────────────────────────
# The beads-rust release the plugin fetches. Moved DELIBERATELY, alongside the sha256s
# (from the release's per-asset `.sha256` files / SHA256SUMS) and a CI matrix leg that
# runs the real-br shape tier on exactly this version — never "latest". 0.3.2 is the
# newest tag the plugin's store supports (the `--json` envelope normalization of
# #138/#140 covers 0.1.x bare lists and 0.2.x+ `{"issues": […]}`; the real-br
# integration file passes the same 18/19 on 0.2.16, 0.2.22 and 0.3.2 — the one miss
# being the 0.1.x-quirk pin CI only runs on its 0.1.23 leg).
BR_VERSION = "0.3.2"
BR_RELEASE_URL = (
    "https://github.com/Dicklesworthstone/beads_rust/releases/download/v{version}/br-{version}-{platform}.tar.gz"
)
# platform key → sha256 of `br-<version>-<platform>.tar.gz`. Keys are the release's
# own asset names (the 0.2.x+ assets ship duplicate aliases — darwin_aarch64 ==
# darwin_arm64, darwin_x86_64 == darwin_amd64, linux_aarch64 == linux_arm64).
BR_SHA256: dict[str, str] = {
    "darwin_arm64": "3c605d0423defccbc2a8b02e1aa6e1b8c2183e50afcf2e11d6cfc00fae86aa01",
    "darwin_amd64": "ede8dd7b66aacb009b7ef78c4f0a45d7b69a09268263df9a5f9ab2876932ac32",
    "linux_amd64": "e67c560e77e912490e44a65e3e9c13205210d171e729c5d801072ee508207288",
    "linux_arm64": "55e6060f5ea2367629afc5413bbe262b011c5cf3f234fcfd66d70d437d77edb6",
}
FETCH_TIMEOUT_S = 60.0
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024  # a br release tarball is ~5 MB; refuse anything absurd
ENV_BR_BIN = "BR_BIN"
ENV_DATA_DIR = "PROJECT_BOARD_DATA_DIR"
# Where a redirect hop may land: GitHub serves release assets from
# release-assets.githubusercontent.com (verified live; older docs say objects.…).
REDIRECT_HOST_SUFFIX = ".githubusercontent.com"

# Operator-facing copy (setup_check composes these into the `br` hint).
INSTALL_HINT = "install beads-rust (cargo install beads_rust), not the homebrew `bd`, and restart (or set BR_BIN)"
WINDOWS_HINT = (
    "br auto-fetch does not support Windows — install beads-rust by hand "
    "(cargo install beads_rust, or the windows_amd64 zip from the beads_rust releases page) "
    "and set BR_BIN to the br.exe path"
)
MUSL_HINT = (
    "br auto-fetch has no musl build (Alpine) — the pinned beads-rust assets are glibc binaries; "
    "install beads-rust by hand (cargo install beads_rust) and restart (or set BR_BIN)"
)


@dataclass(frozen=True)
class FetchSpec:
    version: str
    platform: str
    url: str
    sha256: str


# ── platform ──────────────────────────────────────────────────────────────────────
_MACHINES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}


def is_musl() -> bool:
    """A musl libc host (Alpine): the pinned linux assets are glibc builds and would
    fail to exec. ``platform.libc_ver()`` names glibc when it can read the interpreter;
    musl shows as an empty name, so also look for musl's loader."""
    try:
        if _platform.libc_ver()[0] == "glibc":
            return False
    except Exception:  # noqa: BLE001 — an unreadable interpreter is not a verdict
        pass
    try:
        return bool(list(Path("/lib").glob("ld-musl-*.so.1")))
    except OSError:
        return False


def platform_key(system: str | None = None, machine: str | None = None, *, musl: bool | None = None) -> str | None:
    """``darwin_arm64`` / ``darwin_amd64`` / ``linux_amd64`` / ``linux_arm64`` for this
    host, or None when there is no pin for it (Windows, musl/Alpine, riscv, …)."""
    system = (system or _platform.system()).strip().lower()
    machine = _MACHINES.get((machine or _platform.machine()).strip().lower())
    if system not in ("darwin", "linux") or not machine:
        return None
    if system == "linux" and (is_musl() if musl is None else musl):
        return None
    key = f"{system}_{machine}"
    return key if key in BR_SHA256 else None


def fetch_spec(platform: str | None = None) -> FetchSpec | None:
    key = platform or platform_key()
    if key is None or key not in BR_SHA256:
        return None
    return FetchSpec(
        version=BR_VERSION,
        platform=key,
        url=BR_RELEASE_URL.format(version=BR_VERSION, platform=key),
        sha256=BR_SHA256[key],
    )


# ── where it lands ────────────────────────────────────────────────────────────────
def data_dir() -> Path:
    """The per-instance WRITABLE plugin-data dir for this plugin —
    ``PROJECT_BOARD_DATA_DIR`` if set, else the host's
    ``instance_paths().store("plugin-data") / "project_board"`` (ADR 0065 instance
    root — never the plugin SOURCE checkout, which is read-only on desktop), else
    ``~/.protoagent/plugin-data/project_board`` for a host-free run."""
    raw = os.environ.get(ENV_DATA_DIR, "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        from infra.paths import instance_paths

        return Path(instance_paths().store("plugin-data")) / "project_board"
    except Exception:  # noqa: BLE001 — no protoAgent host (tests, standalone)
        return Path.home() / ".protoagent" / "plugin-data" / "project_board"


def fetched_br_path(base: Path | None = None, *, version: str = BR_VERSION) -> Path:
    """``<data>/bin/<version>/br`` — keyed by version, so a pin bump re-fetches (the old
    binary is simply no longer the resolved path) and the path itself says which
    release it holds."""
    return (base or data_dir()) / "bin" / version / "br"


def _is_executable_file(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def resolve_br_bin(*, env: dict | None = None, fetched: Path | None = None) -> str:
    """The binary name/path the store should shell, in precedence order: an explicit
    ``BR_BIN`` env (the operator's call, always wins) > a previously fetched binary
    that is present + executable > plain ``br`` (PATH lookup by the store)."""
    env = os.environ if env is None else env
    explicit = str(env.get(ENV_BR_BIN) or "").strip()
    if explicit:
        return explicit
    candidate = fetched or fetched_br_path()
    if _is_executable_file(candidate):
        return str(candidate)
    return "br"


# ── process-stable fetch state ────────────────────────────────────────────────────
# idle → fetching → done | failed ; or disabled / unsupported (no attempt made).
_SLOT_PREFIX = "project_board.br_fetch::"


def _slot():
    pkg = __name__.rsplit(".", 1)[0] if "." in __name__ else __name__
    name = _SLOT_PREFIX + pkg
    holder = sys.modules.get(name)
    if holder is None:
        holder = types.ModuleType(name)
        holder.__doc__ = "Process-stable holder for project_board's br auto-fetch state — data, not code."
        holder.state = {"state": "idle", "path": "", "error": "", "started": 0.0, "finished": 0.0, "spec": None}
        holder.lock = threading.Lock()
        holder = sys.modules.setdefault(name, holder)  # atomic install — see store._br_lock
    return holder


def fetch_state() -> dict:
    """A copy of the fetch state: ``{state, path, error, started, finished, version,
    platform}`` — ``state`` ∈ idle / fetching / done / failed / disabled / unsupported.
    ``version`` is the release the fetch spec names, or the one a version-keyed fetched
    path was built for — never the bare pin over a binary of unknown provenance (a
    `br` found on PATH reports ``""``; setup_check samples ``br --version`` for it)."""
    st = dict(_slot().state)
    spec = st.pop("spec", None)
    st["version"] = spec.version if spec else _version_from_path(st.get("path") or "")
    st["platform"] = spec.platform if spec else (platform_key() or "")
    return st


def _version_from_path(path: str) -> str:
    """``"0.3.2"`` for ``…/bin/0.3.2/br`` (a path this module laid out), else ``""``."""
    p = Path(path) if path else None
    if p is None or p.name != "br" or p.parent.parent.name != "bin":
        return ""
    return p.parent.name


def reset_state() -> None:
    """Tests only — back to idle."""
    holder = _slot()
    with holder.lock:
        holder.state = {"state": "idle", "path": "", "error": "", "started": 0.0, "finished": 0.0, "spec": None}


def _set(**fields) -> None:
    holder = _slot()
    with holder.lock:
        holder.state.update(fields)


# ── the download ──────────────────────────────────────────────────────────────────
def _egress_check(url: str) -> str | None:
    """The host's in-process egress allowlist verdict for ``url`` (ADR 0008), or None
    when allowed / the host has no such guard."""
    try:
        from security.egress import check_url
    except Exception:  # noqa: BLE001 — host-free, or an older host
        return None
    try:
        return check_url(url)
    except Exception:  # noqa: BLE001 — a guard that errors must not block the fetch
        return None


def check_redirect_target(newurl: str) -> None:
    """A redirect hop is allowed only to HTTPS on ``*.githubusercontent.com`` AND past
    the host's egress allowlist — the initial URL is checked by ``fetch_br``, but urllib
    follows the 302 on its own, so each ``Location`` is checked here. Raises
    ``PermissionError`` otherwise."""
    parts = urllib.parse.urlsplit(newurl)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(REDIRECT_HOST_SUFFIX):
        raise PermissionError(
            f"redirect to {parts.scheme}://{host or '?'} refused — only https *{REDIRECT_HOST_SUFFIX}"
        )
    blocked = _egress_check(newurl)
    if blocked:
        raise PermissionError(f"egress blocked on redirect: {blocked}")


class _PinnedRedirects(urllib.request.HTTPRedirectHandler):
    """urllib's redirect handler with every hop run through ``check_redirect_target``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_redirect_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urllib_download(url: str, timeout: float) -> bytes:
    """Plain HTTPS GET → bytes, bounded by ``timeout`` overall and ``MAX_ARCHIVE_BYTES``.
    The deadline is checked between chunks and the socket timeout is 30 s per
    operation, so the worst case is ``timeout`` + one blocked read — the hint says
    "~60s" for that reason."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={"User-Agent": "protoagent-project-board/br-fetch"})
    buf = io.BytesIO()
    opener = urllib.request.build_opener(_PinnedRedirects())
    with opener.open(req, timeout=max(1.0, min(30.0, timeout))) as resp:  # noqa: S310 — pinned https URL
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"download exceeded {timeout:.0f}s")
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            buf.write(chunk)
            if buf.tell() > MAX_ARCHIVE_BYTES:
                raise ValueError(f"archive larger than {MAX_ARCHIVE_BYTES} bytes — refusing")
    return buf.getvalue()


def _host_downloader(host):
    """``host.fetch_bytes(url, timeout=)`` when the host exposes one (none does today —
    the seam is a guard so an allowlist-aware client can slot in without a plugin
    change), else None."""
    fn = getattr(host, "fetch_bytes", None) if host is not None else None
    return fn if callable(fn) else None


def _extract_br(archive: bytes) -> bytes:
    """The ``br`` member out of the release tarball — ONLY that member (never a path
    traversal, never a symlink), as bytes."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if os.path.basename(member.name) != "br" or not member.isfile():
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            return f.read()
    raise ValueError("release archive has no `br` binary in it")


def fetch_br(spec: FetchSpec, dest: Path, *, downloader=None, timeout: float = FETCH_TIMEOUT_S) -> Path:
    """Download ``spec.url``, verify its sha256 against ``spec.sha256``, extract ``br``
    to ``dest`` (atomic: temp file + rename, mode 0755). Returns ``dest``. Raises on
    any failure — the caller turns that into the fetch state."""
    blocked = _egress_check(spec.url)
    if blocked:
        raise PermissionError(f"egress blocked: {blocked}")
    downloader = downloader or _urllib_download
    archive = downloader(spec.url, timeout=timeout)
    if not isinstance(archive, (bytes, bytearray)):
        raise TypeError(f"downloader returned {type(archive).__name__}, expected bytes")
    digest = hashlib.sha256(archive).hexdigest()
    if digest != spec.sha256:
        raise ValueError(f"sha256 mismatch for {spec.url}: expected {spec.sha256[:12]}…, got {digest[:12]}…")
    binary = _extract_br(bytes(archive))
    dest.parent.mkdir(parents=True, exist_ok=True)
    for stale in dest.parent.glob(".br-*"):  # a temp left by a fetch the process died in
        try:
            stale.unlink()
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=".br-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(binary)
        os.chmod(tmp, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def _activate(path: Path) -> None:
    """Point the store at the fetched binary — in place, no restart. ``store.BR`` is
    read at call time by the store's every ``br`` op and by ``setup_check``."""
    try:
        from . import store as store_mod

        if not str(os.environ.get(ENV_BR_BIN) or "").strip():  # an explicit BR_BIN always wins
            store_mod.BR = str(path)
    except Exception:  # noqa: BLE001 — host-free import shapes
        log.warning("[project_board] br fetched to %s but the store could not be re-pointed", path, exc_info=True)


def _run_fetch(spec: FetchSpec, dest: Path, downloader, timeout: float) -> None:
    try:
        path = fetch_br(spec, dest, downloader=downloader, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — every failure is a setup gap, never a traceback
        _set(state="failed", error=f"{type(exc).__name__}: {exc}", finished=time.time())
        log.warning("[project_board] br v%s auto-fetch failed: %s — %s", spec.version, exc, INSTALL_HINT)
        return
    _activate(path)
    _set(state="done", path=str(path), error="", finished=time.time())
    log.info(
        "[project_board] br v%s (%s) fetched to %s — the board store uses it from now on",
        spec.version,
        spec.platform,
        path,
    )


def ensure_br(
    cfg: dict | None,
    *,
    which=None,
    downloader=None,
    host=None,
    platform: str | None = None,
    dest: Path | None = None,
    background: bool = True,
    timeout: float = FETCH_TIMEOUT_S,
) -> dict:
    """Make sure a ``br`` is available: no-op when one already resolves (``BR_BIN``,
    a previous fetch, PATH); otherwise — once per process, when
    ``project_board.br_autofetch`` is on and the platform has a pin — start the fetch
    (a daemon thread by default; ``background=False`` runs it inline for tests) and
    return immediately. Returns the fetch state. Never raises."""
    which = which or shutil.which
    cfg = cfg or {}
    try:
        current = resolve_br_bin(fetched=dest)
        target = dest or fetched_br_path()
        holder = _slot()
        if which(current):
            with holder.lock:
                if holder.state["state"] == "idle":
                    # `current` is the version-keyed fetched path (a previous run's fetch)
                    # or a PATH `br` (path "", version "" — setup_check samples it).
                    fetched = current == str(target)
                    holder.state.update(
                        state="done",
                        path=str(which(current)) if current != "br" else "",
                        error="",
                        spec=fetch_spec(platform) if fetched else None,
                    )
            return fetch_state()
        explicit = str(os.environ.get(ENV_BR_BIN) or "").strip()
        with holder.lock:
            # The knob / BR_BIN / platform verdicts only ever move idle → {disabled,
            # unsupported}: a fetch in flight, done, or failed is never overwritten (a
            # knob flipped off mid-download must not clobber `fetching`, and flipping it
            # back on must not start a SECOND concurrent download).
            spent = holder.state["state"] in ("fetching", "done", "failed")
            if explicit:
                # BR_BIN is the operator's explicit choice — and it does not resolve.
                # Never download 10 MB that resolve_br_bin would refuse to use anyway.
                if not spent:
                    holder.state.update(state="disabled", error=f"{ENV_BR_BIN}={explicit} is set — not fetching")
                return fetch_state()
            if not _knob_on(cfg.get("br_autofetch", True)):
                if not spent:
                    holder.state.update(state="disabled", error="")
                return fetch_state()
            spec = fetch_spec(platform)
            if spec is None:
                if not spent:
                    holder.state.update(state="unsupported", error=_unsupported_hint(platform))
                return fetch_state()
            if spent:
                return fetch_state()  # once per process — a failure stays reported until a restart
            holder.state.update(state="fetching", spec=spec, started=time.time(), finished=0.0, error="", path="")
        dl = downloader or _host_downloader(host)
        log.info(
            "[project_board] br not on PATH — fetching beads-rust v%s for %s from %s …",
            spec.version,
            spec.platform,
            spec.url,
        )
        if background:
            threading.Thread(
                target=_run_fetch, args=(spec, target, dl, timeout), name="project-board-br-fetch", daemon=True
            ).start()
        else:
            _run_fetch(spec, target, dl, timeout)
    except Exception as exc:  # noqa: BLE001 — belt and braces: a fetch must never break registration
        _set(state="failed", error=f"{type(exc).__name__}: {exc}", finished=time.time())
        log.warning("[project_board] br auto-fetch could not start: %s", exc, exc_info=True)
    return fetch_state()


def _knob_on(raw) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def _unsupported_hint(platform: str | None) -> str:
    """Why there is no pin for this host — Windows / musl get their own copy; anything
    else (riscv, …) leaves ``error`` empty and ``hint_for`` says "no build"."""
    system = (platform or _platform.system()).lower()
    if system.startswith("win"):
        return WINDOWS_HINT
    if system.startswith("linux") and is_musl():
        return MUSL_HINT
    return ""


def hint_for(state: dict, *, br_bin: str = "br") -> str:
    """The operator-facing ``br`` gap hint for a fetch ``state`` (when ``br`` is NOT
    resolvable right now)."""
    s = state.get("state", "idle")
    base = f"beads CLI {br_bin!r} not found on PATH"
    if s == "fetching":
        age = max(0, int(time.time() - float(state.get("started") or time.time())))
        return (
            f"{base} — fetching beads-rust v{state.get('version')} for {state.get('platform')} "
            f"({age}s so far, ~{int(FETCH_TIMEOUT_S)}s cap); the board resumes on its own once it lands"
        )
    if s == "failed":
        # Honest: the fetch runs once per process; a restart starts from idle and retries.
        return f"{base} — br auto-fetch failed: {state.get('error')}; {INSTALL_HINT}, or restart to retry the fetch"
    if s == "disabled":
        why = state.get("error") or "br_autofetch is off"
        return f"{base} — {why}; {INSTALL_HINT}"
    if s == "unsupported":
        return state.get("error") or f"{base} — br auto-fetch has no build for this platform; {INSTALL_HINT}"
    # idle (setup_check routes the plain "no br" case here too — one copy of the hint)
    return f"{base} — {INSTALL_HINT}; the board is paused until then"
