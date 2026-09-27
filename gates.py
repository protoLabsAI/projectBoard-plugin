"""Publish gates — ``waits_for`` conditions outside the board.

``depends_on`` / ``foundation`` release a dependent card when its blocker MERGES. For a
cross-repo chain that is too early: a design-system change merges in one repo, a
changesets "Version Packages" PR is opened and merged, and only THEN is the new version
on npm. A consumer card released at the first merge makes its coder try to install a
version that does not exist yet. A publish gate names the external fact the card really
waits for, and the loop leaves the card out of the claimable set until it holds.

Grammar (one spec; a card carries a comma-separated list, ALL must hold)::

    npm:<package>[@<semver-range>]       a published npm version satisfies the range
                                         (no range = any published version)
    release:<owner>/<repo>@<tag>         that git tag exists on GitHub
    release:<owner>/<repo>@<semver-range>
                                         a published (non-draft) GitHub release whose tag
                                         is a version satisfying the range
    pr:<owner>/<repo>#<n>                that PR is merged

A spec is decided ``release:…@<range>`` rather than ``@<tag>`` when the text after the
last ``@`` starts with a range operator (``< > = ^ ~``), is ``*``, contains a space or
``||``, or has an ``x``/``X`` wildcard part; anything else is an exact tag name.

``card:<id>`` is deliberately NOT a gate kind: a card waiting on another card's merge is
exactly ``depends_on``, which the board already enforces in ``br ready`` itself.

Evaluation is fail CLOSED: a registry/GitHub error reads as UNMET, with the error shown
on the card, and the spec is backed off before it is asked again. Results are cached
per spec in process-stable state, shared by every card naming the same spec, so a board
of twenty consumer cards waiting on one package asks the registry once per TTL — never
once per card per tick.

Semver: a small stdlib implementation of node-semver's range semantics (comparators,
X-ranges, ``~``, ``^``, hyphen ranges, ``||``), including its PRERELEASE rule: a
prerelease version satisfies a range only when some comparator in the matching set
names a prerelease on the same ``major.minor.patch``. So ``>=0.63.0`` is NOT satisfied
by ``0.64.0-next.1`` — a snapshot publish must not release a consumer card.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("protoagent.plugins.project_board")

# ── spec grammar ────────────────────────────────────────────────────────────────────
KIND_NPM = "npm"
KIND_RELEASE = "release"
KIND_PR = "pr"
KINDS = (KIND_NPM, KIND_RELEASE, KIND_PR)

# npm's own name rules (lowercase, url-safe; an optional @scope/). Validated so a spec
# can never smuggle a path or query into the registry URL.
_NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9~-][a-z0-9._~-]*/)?[a-z0-9~-][a-z0-9._~-]*$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG_RE = re.compile(r"^[A-Za-z0-9._/@+-]+$")
# A version-ish token with an x/X/* wildcard part: `1.x`, `1.2.X`, `*`.
_WILDCARD_PART_RE = re.compile(r"(?:^|\.)[xX*](?:\.|$)")

# The prefix the card's `next_action` carries while a gate is unmet. Shared with the
# listing, the console chip and the claim-scan skip reason so they read identically.
NEXT_ACTION_PREFIX = "waiting on publish: "


class GateSpecError(ValueError):
    """A `waits_for` spec that does not parse — named, so the tool can refuse the write."""


@dataclass(frozen=True)
class GateSpec:
    raw: str  # the canonical spec text (what is persisted)
    kind: str
    target: str  # npm package, or owner/repo
    constraint: str  # semver range / tag / PR number ("" = any published version)
    is_range: bool = False

    def describe(self) -> str:
        """Human text for the card: ``npm @protolabsai/ui >=0.63.0``."""
        if self.kind == KIND_NPM:
            return f"npm {self.target} {self.constraint or '(any version)'}"
        if self.kind == KIND_RELEASE:
            return f"release {self.target} {self.constraint}"
        return f"pr {self.target}#{self.constraint}"


def _looks_like_range(text: str) -> bool:
    t = text.strip()
    return (
        not t or t[0] in "<>=^~" or t == "*" or " " in t or "||" in t or bool(_WILDCARD_PART_RE.search(t.lstrip("vV")))
    )


def parse_spec(raw) -> GateSpec:
    """Parse ONE spec, or raise :class:`GateSpecError` naming what is wrong."""
    text = " ".join(str(raw or "").split())  # collapse internal whitespace runs
    try:
        return _parse_spec(text)
    except GateSpecError as exc:
        msg = str(exc)
        raise GateSpecError(msg if msg.startswith("waits_for spec") else f"waits_for spec {text!r}: {msg}") from None


def _parse_spec(text: str) -> GateSpec:
    kind, sep, rest = text.partition(":")
    kind = kind.strip().lower()
    rest = rest.strip()
    if not sep or kind not in KINDS or not rest:
        raise GateSpecError(
            f"waits_for spec {text!r} is not one of npm:<package>[@<range>], "
            "release:<owner>/<repo>@<tag-or-range>, pr:<owner>/<repo>#<n>"
        )
    if kind == KIND_NPM:
        # The package may itself start with `@` (a scope), so the range separator is
        # the LAST `@` past index 0.
        at = rest.rfind("@")
        if at > 0:
            package, rng = rest[:at].strip(), rest[at + 1 :].strip()
        else:
            package, rng = rest, ""
        package = package.lower()
        if not _NPM_NAME_RE.match(package):
            raise GateSpecError(f"waits_for spec {text!r}: {package!r} is not a valid npm package name")
        if rng:
            parse_range(rng)  # validate now: a bad range must refuse the write, not sit unmet forever
        return GateSpec(f"npm:{package}@{rng}" if rng else f"npm:{package}", KIND_NPM, package, rng, True)
    if kind == KIND_RELEASE:
        slug, at, ref = rest.partition("@")
        slug, ref = slug.strip(), ref.strip()
        if not at or not ref or not _SLUG_RE.match(slug):
            raise GateSpecError(f"waits_for spec {text!r}: expected release:<owner>/<repo>@<tag-or-range>")
        if _looks_like_range(ref):
            parse_range(ref)
            return GateSpec(f"release:{slug}@{ref}", KIND_RELEASE, slug, ref, True)
        if not _TAG_RE.match(ref):
            raise GateSpecError(f"waits_for spec {text!r}: {ref!r} is not a valid tag name")
        return GateSpec(f"release:{slug}@{ref}", KIND_RELEASE, slug, ref, False)
    slug, hsh, num = rest.partition("#")
    slug, num = slug.strip(), num.strip()
    if not hsh or not _SLUG_RE.match(slug) or not num.isdigit() or int(num) <= 0:
        raise GateSpecError(f"waits_for spec {text!r}: expected pr:<owner>/<repo>#<number>")
    return GateSpec(f"pr:{slug}#{int(num)}", KIND_PR, slug, str(int(num)))


def parse_specs(value) -> list[GateSpec]:
    """A comma-separated string (the tool surface) or a list → specs, de-duplicated in
    order. Commas never occur inside a spec (ranges use spaces and ``||``)."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[GateSpec] = []
    seen: set[str] = set()
    for item in items:
        if not str(item or "").strip():
            continue
        spec = parse_spec(item)
        if spec.raw not in seen:
            seen.add(spec.raw)
            out.append(spec)
    return out


def normalize_specs(value) -> list[str]:
    """The canonical persisted text of every spec in ``value`` (raises on a bad one)."""
    return [s.raw for s in parse_specs(value)]


# ── semver ──────────────────────────────────────────────────────────────────────────
_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple = ()

    @classmethod
    def parse(cls, text) -> SemVer | None:
        """A strict semver 2.0 version (a leading ``v`` tolerated, build metadata
        ignored), or None."""
        m = _SEMVER_RE.match(str(text or "").strip())
        if not m:
            return None
        pre = tuple(int(p) if p.isdigit() else p for p in m.group(4).split(".")) if m.group(4) else ()
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)

    def _key(self):
        # A release sorts after every prerelease of the same triple; within a prerelease,
        # numeric identifiers sort before alphanumeric ones, and a shorter identifier list
        # sorts first when every shared identifier is equal (semver §11).
        pre = tuple((0, p, "") if isinstance(p, int) else (1, 0, p) for p in self.prerelease)
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, pre)

    def __lt__(self, other):
        return self._key() < other._key()

    def __le__(self, other):
        return self._key() <= other._key()

    def __gt__(self, other):
        return self._key() > other._key()

    def __ge__(self, other):
        return self._key() >= other._key()

    def __str__(self):
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base + ("-" + ".".join(str(p) for p in self.prerelease) if self.prerelease else "")


_PARTIAL_RE = re.compile(
    r"^v?(\*|[xX]|0|[1-9]\d*)(?:\.(\*|[xX]|0|[1-9]\d*)(?:\.(\*|[xX]|0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-.]+)?)?)?$"
)


def _partial(text: str):
    """``(major, minor, patch, prerelease)`` with None for an absent/wildcard part."""
    m = _PARTIAL_RE.match(text.strip())
    if not m:
        raise GateSpecError(f"{text!r} is not a version")

    def num(g):
        return None if g is None or g in ("*", "x", "X") else int(g)

    major, minor, patch = num(m.group(1)), num(m.group(2)), num(m.group(3))
    # A wildcard swallows everything after it: `1.x.3` is `1.x`.
    if major is None:
        minor = patch = None
    elif minor is None:
        patch = None
    pre = m.group(4) if patch is not None else None
    return major, minor, patch, pre


def _v(major, minor, patch, pre=None) -> SemVer:
    parsed = SemVer.parse(f"{major}.{minor}.{patch}" + (f"-{pre}" if pre else ""))
    assert parsed is not None
    return parsed


def _lower_upper_for_partial(p) -> tuple[SemVer | None, SemVer | None]:
    """The bare/`=` X-range: `1.2` → [1.2.0, 1.3.0-0), `1` → [1.0.0, 2.0.0-0), `*` → any."""
    major, minor, patch, pre = p
    if major is None:
        return None, None
    if minor is None:
        return _v(major, 0, 0), _v(major + 1, 0, 0, "0")
    if patch is None:
        return _v(major, minor, 0), _v(major, minor + 1, 0, "0")
    return _v(major, minor, patch, pre), None


# A comparator is (op, SemVer); op in <, <=, >, >=, =.
def _desugar(token: str) -> list[tuple[str, SemVer]]:
    token = token.strip()
    if token in ("", "*", "x", "X"):
        return []  # any version (a release; the prerelease rule still applies)
    if token[0] == "^":
        major, minor, patch, pre = _partial(token[1:])
        if major is None:
            return []
        lo = _v(major, minor or 0, patch or 0, pre)
        if major != 0 or minor is None:
            hi = _v(major + 1, 0, 0, "0")
        elif minor != 0 or patch is None:
            hi = _v(0, minor + 1, 0, "0")
        else:
            hi = _v(0, 0, patch + 1, "0")
        return [(">=", lo), ("<", hi)]
    if token[0] == "~":
        major, minor, patch, pre = _partial(token[1:].lstrip(">"))
        if major is None:
            return []
        lo = _v(major, minor or 0, patch or 0, pre)
        hi = _v(major + 1, 0, 0, "0") if minor is None else _v(major, minor + 1, 0, "0")
        return [(">=", lo), ("<", hi)]
    m = re.match(r"^(<=|>=|<|>|=)?\s*(.+)$", token)
    op, rest = (m.group(1) or "="), m.group(2)
    p = _partial(rest)
    major, minor, patch, pre = p
    exact = patch is not None
    if op == "=":
        lo, hi = _lower_upper_for_partial(p)
        if lo is None:
            return []
        if hi is None:
            return [("=", lo)]
        return [(">=", lo), ("<", hi)]
    if major is None:  # `>*` matches nothing, `>=*` / `<=*` everything, `<*` nothing
        return [("<", _v(0, 0, 0, "0"))] if op in (">", "<") else []
    if exact:
        return [(op, _v(major, minor, patch, pre))]
    # A partial with an operator (node-semver's replaceXRange).
    if op == ">":
        return [(">=", _v(major + 1, 0, 0) if minor is None else _v(major, minor + 1, 0))]
    if op == ">=":
        return [(">=", _v(major, minor or 0, 0))]
    if op == "<":
        return [("<", _v(major, minor or 0, 0, "0"))]
    # "<="
    return [("<", _v(major + 1, 0, 0, "0") if minor is None else _v(major, minor + 1, 0, "0"))]


def _hyphen(lo_text: str, hi_text: str) -> list[tuple[str, SemVer]]:
    out: list[tuple[str, SemVer]] = []
    major, minor, patch, pre = _partial(lo_text)
    if major is not None:
        out.append((">=", _v(major, minor or 0, patch or 0, pre)))
    major, minor, patch, pre = _partial(hi_text)
    if major is not None:
        if minor is None:
            out.append(("<", _v(major + 1, 0, 0, "0")))
        elif patch is None:
            out.append(("<", _v(major, minor + 1, 0, "0")))
        else:
            out.append(("<=", _v(major, minor, patch, pre)))
    return out


def parse_range(text: str) -> list[list[tuple[str, SemVer]]]:
    """A node-semver range → a list of comparator SETS (OR of ANDs). Raises
    :class:`GateSpecError` on anything it cannot read."""
    sets: list[list[tuple[str, SemVer]]] = []
    for alt in str(text or "").split("||"):
        alt = alt.strip()
        hy = re.match(r"^(\S+)\s+-\s+(\S+)$", alt)
        if hy:
            sets.append(_hyphen(hy.group(1), hy.group(2)))
            continue
        # `>= 1.2.3` → `>=1.2.3`: glue an operator to its version.
        alt = re.sub(r"(<=|>=|<|>|=|\^|~)\s+", r"\1", alt)
        comps: list[tuple[str, SemVer]] = []
        for token in alt.split():
            try:
                comps.extend(_desugar(token))
            except GateSpecError:
                raise GateSpecError(f"{text!r} is not a semver range (bad comparator {token!r})") from None
        sets.append(comps)
    return sets


def _test(op: str, have: SemVer, want: SemVer) -> bool:
    if op == "=":
        return have._key() == want._key()
    if op == "<":
        return have < want
    if op == "<=":
        return have <= want
    if op == ">":
        return have > want
    return have >= want


def satisfies(version, rng) -> bool:
    """node-semver ``satisfies`` (default options: no ``includePrerelease``)."""
    v = version if isinstance(version, SemVer) else SemVer.parse(version)
    if v is None:
        return False
    sets = parse_range(rng) if isinstance(rng, str) else rng
    for comps in sets:
        if not all(_test(op, v, want) for op, want in comps):
            continue
        if not v.prerelease:
            return True
        # The prerelease rule: only a comparator naming a prerelease on the SAME triple
        # admits one — `>=1.2.3-beta.1` admits `1.2.3-beta.2`, never `1.3.0-beta.1`.
        if any(
            want.prerelease and (want.major, want.minor, want.patch) == (v.major, v.minor, v.patch)
            for _op, want in comps
        ):
            return True
    return False


def max_satisfying(versions, rng) -> SemVer | None:
    sets = parse_range(rng) if isinstance(rng, str) else rng
    best = None
    for raw in versions:
        v = raw if isinstance(raw, SemVer) else SemVer.parse(raw)
        if v is not None and satisfies(v, sets) and (best is None or v > best):
            best = v
    return best


# ── the external seams (classified in tests/test_external_seams.py) ──────────────────
NPM_REGISTRY = "https://registry.npmjs.org"
_HTTP_TIMEOUT_S = 15.0
_GH_TIMEOUT_S = 30.0


class GateCheckError(Exception):
    """An evaluation that could not reach a verdict (network, auth, rate limit…)."""


def npm_token(cfg: dict | None = None) -> str:
    """The optional registry token for PRIVATE packages: ``project_board.npm_token`` (a
    secret, like ``webhook_secret``), else ``PROJECT_BOARD_NPM_TOKEN``, else the
    conventional ``NPM_TOKEN``. Blank = anonymous (public packages only)."""
    return str(
        (cfg or {}).get("npm_token") or os.environ.get("PROJECT_BOARD_NPM_TOKEN", "") or os.environ.get("NPM_TOKEN", "")
    ).strip()


def _http_get_json(url: str, *, token: str = "", timeout: float = _HTTP_TIMEOUT_S) -> tuple[int, object]:
    """GET ``url`` → ``(status, parsed-json-or-None)``. A 4xx/5xx is RETURNED (the
    caller decides whether 404 means "not yet" or an error); a network failure raises
    :class:`GateCheckError`. Only ever called with an https registry URL built from a
    validated package name."""
    headers = {
        # The abbreviated packument: versions + dist-tags, a fraction of the full doc.
        "Accept": "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8",
        "User-Agent": "protoagent-project-board/publish-gate",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https, fixed host
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GateCheckError(f"registry unreachable: {exc}") from exc


def _gh_json(path: str, *, timeout: float = _GH_TIMEOUT_S) -> tuple[int, object, str]:
    """``gh api <path>`` → ``(rc, parsed-json-or-None, stderr)``. Rides the same ``gh``
    credential the board already uses for every PR operation."""
    try:
        proc = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateCheckError(f"gh api failed: {exc}") from exc
    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else None
    except ValueError:
        data = None
    return proc.returncode, data, (proc.stderr or "").strip()


def _not_found(rc: int, data, err: str) -> bool:
    return rc != 0 and ("404" in err or (isinstance(data, dict) and data.get("status") == "404"))


def eval_npm(spec: GateSpec, *, token: str = "") -> dict:
    """Satisfied when the registry has a published version satisfying the range."""
    url = f"{NPM_REGISTRY}/{urllib.parse.quote(spec.target, safe='@')}"
    status, doc = _http_get_json(url, token=token)
    what = spec.describe()
    if status == 404:
        # Not an error: a package that has never been published is simply "not yet".
        return {"met": False, "detail": f"{what} (not published yet)"}
    if status != 200 or not isinstance(doc, dict):
        hint = " — a private package needs project_board.npm_token" if status in (401, 403) else ""
        raise GateCheckError(f"registry answered HTTP {status} for {spec.target}{hint}")
    versions = list((doc.get("versions") or {}).keys())
    latest = str((doc.get("dist-tags") or {}).get("latest") or "")
    best = max_satisfying(versions, spec.constraint or "*")
    if best is not None:
        return {"met": True, "detail": f"{what} ({best} published)", "version": str(best)}
    return {"met": False, "detail": f"{what} (latest {latest or 'none'})", "latest": latest}


def eval_release(spec: GateSpec) -> dict:
    """An exact tag must exist; a range must be satisfied by a published release's tag."""
    what = spec.describe()
    if not spec.is_range:
        rc, data, err = _gh_json(f"repos/{spec.target}/git/ref/tags/{urllib.parse.quote(spec.constraint, safe='')}")
        if rc == 0 and isinstance(data, dict) and data.get("ref"):
            return {"met": True, "detail": f"{what} (tag exists)"}
        if _not_found(rc, data, err):
            return {"met": False, "detail": f"{what} (no such tag yet)"}
        raise GateCheckError(f"GitHub tag lookup failed for {spec.target}: {err or f'rc={rc}'}")
    rc, data, err = _gh_json(f"repos/{spec.target}/releases?per_page=100")
    if rc != 0 or not isinstance(data, list):
        raise GateCheckError(f"GitHub releases lookup failed for {spec.target}: {err or f'rc={rc}'}")
    versions = []
    for rel in data:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "")
        # `v1.2.3`, `1.2.3`, and a changesets-style `@scope/pkg@1.2.3` all read as 1.2.3.
        parsed = SemVer.parse(tag.rsplit("@", 1)[-1])
        if parsed is not None:
            versions.append(parsed)
    best = max_satisfying(versions, spec.constraint)
    if best is not None:
        return {"met": True, "detail": f"{what} ({best} released)", "version": str(best)}
    newest = max(versions) if versions else None
    return {"met": False, "detail": f"{what} (latest release {newest or 'none'})"}


def eval_pr(spec: GateSpec) -> dict:
    what = spec.describe()
    rc, data, err = _gh_json(f"repos/{spec.target}/pulls/{spec.constraint}")
    if rc != 0 or not isinstance(data, dict):
        if _not_found(rc, data, err):
            return {"met": False, "detail": f"{what} (no such PR)"}
        raise GateCheckError(f"GitHub PR lookup failed for {spec.target}#{spec.constraint}: {err or f'rc={rc}'}")
    if data.get("merged"):
        return {"met": True, "detail": f"{what} (merged)"}
    state = "closed without merging" if data.get("state") == "closed" else "open"
    return {"met": False, "detail": f"{what} ({state})"}


# ── cache + evaluation ───────────────────────────────────────────────────────────────
# A met gate is monotonic in practice (a published version stays published; a merged PR
# stays merged), so it is re-read rarely. An unmet one is polled at the ready-sweep's
# pace but no faster than UNMET_TTL_S. An error backs off exponentially per spec.
MET_TTL_S = 3600.0
UNMET_TTL_S = 120.0
ERROR_BACKOFF_BASE_S = 60.0
ERROR_BACKOFF_MAX_S = 1800.0
# An on-demand check (board_check_gates) skips the TTL but never re-asks inside this.
FORCE_MIN_INTERVAL_S = 15.0

# Process-stable (survives a plugin hot-reload re-exec of this module), the pattern the
# store uses for its locks.
_SLOT = "project_board.publish_gates::" + (__name__.rsplit(".", 1)[0] if "." in __name__ else __name__)
_holder = sys.modules.get(_SLOT)
if _holder is None:
    _holder = types.ModuleType(_SLOT)
    _holder.cache = {}
    _holder.lock = threading.Lock()
    sys.modules[_SLOT] = _holder
_CACHE: dict[str, dict] = _holder.cache
_LOCK: threading.Lock = _holder.lock

_EVALUATORS = {KIND_NPM: eval_npm, KIND_RELEASE: eval_release, KIND_PR: eval_pr}


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def cached(spec) -> dict | None:
    """The last result for ``spec`` (a GateSpec or its text), or None if never checked.
    A pure read — no network — for the listing/console path."""
    raw = spec.raw if isinstance(spec, GateSpec) else _canonical(spec)
    with _LOCK:
        entry = _CACHE.get(raw)
        return dict(entry["result"]) if entry else None


def _canonical(text) -> str:
    try:
        return parse_spec(text).raw
    except GateSpecError:
        return str(text or "").strip()


def _due(entry: dict | None, now: float, force: bool) -> bool:
    if entry is None:
        return True
    if force:
        return now - entry["checked_at"] >= FORCE_MIN_INTERVAL_S
    return now >= entry["next_at"]


def evaluate(specs, *, token: str = "", force: bool = False, now: float | None = None) -> list[dict]:
    """Evaluate every spec (text or GateSpec), reusing a fresh cached result. NEVER
    raises: an unparseable spec or a failed check is an UNMET result carrying ``error``.

    Each result: ``{spec, kind, met, detail, error, checked_at}``."""
    out = []
    for item in specs or ():
        try:
            spec = item if isinstance(item, GateSpec) else parse_spec(item)
        except GateSpecError as exc:
            out.append(
                {"spec": str(item), "kind": "", "met": False, "detail": str(exc), "error": str(exc), "checked_at": 0.0}
            )
            continue
        out.append(_evaluate_one(spec, token=token, force=force, now=now))
    return out


def _evaluate_one(spec: GateSpec, *, token: str, force: bool, now: float | None) -> dict:
    t = time.time() if now is None else now
    with _LOCK:
        entry = _CACHE.get(spec.raw)
        if not _due(entry, t, force):
            return dict(entry["result"])
        failures = entry["failures"] if entry else 0
        was_met = bool(entry and entry["result"]["met"])
    error = ""
    try:
        if spec.kind == KIND_NPM:
            verdict = eval_npm(spec, token=token)
        else:
            verdict = _EVALUATORS[spec.kind](spec)
        failures = 0
    except Exception as exc:  # noqa: BLE001 — fail CLOSED, with the reason shown on the card
        failures += 1
        error = str(exc) or type(exc).__name__
        verdict = {"met": False, "detail": f"{spec.describe()} (check failed: {error})"}
    result = {
        "spec": spec.raw,
        "kind": spec.kind,
        "met": bool(verdict.get("met")),
        "detail": str(verdict.get("detail") or spec.describe()),
        "error": error,
        "checked_at": t,
    }
    if error:
        delay = min(ERROR_BACKOFF_MAX_S, ERROR_BACKOFF_BASE_S * (2 ** (failures - 1)))
        log.warning(
            "[project_board] publish gate %s: check failed (%d in a row; next try in %ds) — held: %s",
            spec.raw,
            failures,
            int(delay),
            error,
        )
    else:
        delay = MET_TTL_S if result["met"] else UNMET_TTL_S
        if result["met"] and not was_met:
            log.info("[project_board] publish gate %s is MET: %s", spec.raw, result["detail"])
    with _LOCK:
        _CACHE[spec.raw] = {"result": result, "checked_at": t, "next_at": t + delay, "failures": failures}
    return dict(result)


def card_status(feature: dict) -> list[dict]:
    """Every gate on ``feature`` with its CACHED verdict (no network) — an unchecked gate
    reads unmet with detail ``… (not checked yet)``."""
    out = []
    for raw in feature.get("waits_for") or ():
        hit = cached(raw)
        if hit is None:
            try:
                what = parse_spec(raw).describe()
            except GateSpecError:
                what = str(raw)
            hit = {"spec": raw, "kind": "", "met": False, "detail": f"{what} (not checked yet)", "error": ""}
            hit["checked_at"] = 0.0
        out.append(hit)
    return out


def unmet_sentence(results) -> str:
    """``waiting on publish: npm @x/y >=1.0.0 (latest 0.9.0); pr o/r#4 (open)`` — or ""."""
    unmet = [r["detail"] for r in results or () if not r.get("met")]
    return NEXT_ACTION_PREFIX + "; ".join(unmet) if unmet else ""
