"""Publish gates — ``waits_for`` conditions outside the board.

``depends_on`` / ``foundation`` release a dependent card when its blocker MERGES. For a
cross-repo chain that is too early: a design-system change merges in one repo, a
changesets "Version Packages" PR is opened and merged, and only THEN is the new version
on npm. A consumer card released at the first merge makes its coder try to install a
version that does not exist yet. A publish gate names the external fact the card really
waits for, and the loop leaves the card out of the claimable set until it holds.

Grammar (one spec; a card carries a comma-separated list, ALL must hold)::

    npm:<package>@contains:<owner>/<repo>@<card-id-or-sha>
                                         a published npm version is PROVEN to contain that
                                         commit (or that board card's merge commit) — the
                                         gate a consumer of a just-merged change wants
    npm:<package>[@<semver-range>]       a published npm version satisfies the range
                                         (no range = any published version, prereleases too)
    release:<owner>/<repo>@<tag>         that git tag exists on GitHub
    release:<owner>/<repo>@<semver-range>
                                         a published GitHub release (not draft, not
                                         prerelease) whose tag is a version satisfying the
                                         range — for a repo whose tags are plain versions
    release:<owner>/<repo>@<package>@<semver-range>
                                         the same, counting only that package's tags
                                         (`<package>@x.y.z` — changesets monorepos)
    pr:<owner>/<repo>#<n>                that PR is merged

A spec is decided ``release:…@<range>`` rather than ``@<tag>`` when the text after the
last ``@`` starts with a range operator (``< > = ^ ~``), is ``*``, contains a space or
``||``, or has an ``x``/``X`` wildcard part; anything else is an exact tag name.

**Why ``contains:``.** A version floor (``>0.62.0``) cannot tie a publish to a change: in a
changesets repo the "Version Packages" PR can publish ``0.62.1`` WITHOUT the change the
consumer needs (it was open before the change merged). npm does not record which commit
a pnpm/changesets publish came from (no ``gitHead`` in the packument), but changesets
pushes a ``<package>@<version>`` git tag for every version it publishes. So ``contains:``
reads the newest published version, finds its tag, and asks GitHub whether the tagged
commit is the anchor commit or a descendant of it (``compare/<anchor>...<tag commit>`` →
``identical``/``ahead``). A card-id anchor resolves to that card's merged PR's merge
commit, and is unmet until the card's PR has merged — no ``depends_on`` needed for
correctness.

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
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# A board card id (`bd-a1`, `protoEngineer-x9z`): a prefix, a dash, a suffix.
_CARD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.]*-[A-Za-z0-9_.-]+$")
CONTAINS_PREFIX = "contains:"


def _valid_slug(slug: str) -> bool:
    """``owner/repo`` — and never a ``.``/``..`` segment, which would walk the API path."""
    return bool(_SLUG_RE.match(slug)) and ".." not in slug and all(p not in (".", "..") for p in slug.split("/"))


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
    package: str = ""  # release: the package whose `<package>@<version>` tags count
    anchor_repo: str = ""  # npm contains: the repo the anchor commit lives in
    anchor: str = ""  # npm contains: a commit sha or a board card id

    def describe(self) -> str:
        """Human text for the card: ``npm @protolabsai/ui >=0.63.0``."""
        if self.kind == KIND_NPM:
            if self.anchor:
                return f"npm {self.target} containing {self.anchor_repo}@{self.anchor}"
            return f"npm {self.target} {self.constraint or '(any version)'}"
        if self.kind == KIND_RELEASE:
            if self.package:
                return f"release {self.target} {self.package} {self.constraint}"
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
    except Exception as exc:  # noqa: BLE001 — a parser bug must surface as a refusal, never a crash
        raise GateSpecError(f"waits_for spec {text!r}: unreadable ({type(exc).__name__}: {exc})") from None


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
        if "@" + CONTAINS_PREFIX in rest:
            package, _, anchor_part = rest.partition("@" + CONTAINS_PREFIX)
            package = package.strip().lower()
            slug, at, anchor = anchor_part.strip().partition("@")
            slug, anchor = slug.strip(), anchor.strip()
            if not _NPM_NAME_RE.match(package):
                raise GateSpecError(f"{package!r} is not a valid npm package name")
            if not at or not _valid_slug(slug):
                raise GateSpecError("expected npm:<package>@contains:<owner>/<repo>@<card-id-or-sha>")
            if _SHA_RE.match(anchor.lower()):
                anchor = anchor.lower()
            elif not _CARD_ID_RE.match(anchor):
                raise GateSpecError(f"{anchor!r} is neither a commit sha (7-40 hex) nor a board card id")
            return GateSpec(
                f"npm:{package}@{CONTAINS_PREFIX}{slug}@{anchor}",
                KIND_NPM,
                package,
                f"{CONTAINS_PREFIX}{slug}@{anchor}",
                False,
                anchor_repo=slug,
                anchor=anchor,
            )
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
        if not at or not ref or not _valid_slug(slug):
            raise GateSpecError(f"waits_for spec {text!r}: expected release:<owner>/<repo>@<tag-or-range>")
        # `release:o/r@<package>@<range>` — a package-qualified range (changesets tags each
        # package separately, `@scope/pkg@1.2.3`). The package is everything before the
        # LAST `@` (a scoped name starts with one); an exact `<package>@1.2.3` stays a tag.
        pat = ref.rfind("@")
        if pat > 0 and _looks_like_range(ref[pat + 1 :]):
            pkg, rng = ref[:pat].strip().lower(), ref[pat + 1 :].strip()
            if not _NPM_NAME_RE.match(pkg):
                raise GateSpecError(f"{pkg!r} is not a valid package name")
            parse_range(rng)
            return GateSpec(f"release:{slug}@{pkg}@{rng}", KIND_RELEASE, slug, rng, True, package=pkg)
        if _looks_like_range(ref):
            parse_range(ref)
            return GateSpec(f"release:{slug}@{ref}", KIND_RELEASE, slug, ref, True)
        if not _TAG_RE.match(ref):
            raise GateSpecError(f"waits_for spec {text!r}: {ref!r} is not a valid tag name")
        return GateSpec(f"release:{slug}@{ref}", KIND_RELEASE, slug, ref, False)
    slug, hsh, num = rest.partition("#")
    slug, num = slug.strip(), num.strip()
    if not hsh or not _valid_slug(slug) or not num.isdigit() or int(num) <= 0:
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


def _partial(text: str, *, lenient: bool = False):
    """``(major, minor, patch, prerelease)`` with None for an absent/wildcard part.
    ``lenient`` (node's caret/tilde parsers) lets a wildcard swallow what follows it."""
    m = _PARTIAL_RE.match(text.strip())
    if not m:
        raise GateSpecError(f"{text!r} is not a version")

    def num(g):
        return None if g is None or g in ("*", "x", "X") else int(g)

    major, minor, patch = num(m.group(1)), num(m.group(2)), num(m.group(3))
    # A wildcard may only be followed by wildcards: node refuses `x.1`, `1.*.3`.
    if not lenient and (
        (major is None and (minor is not None or patch is not None))
        or (minor is None and m.group(2) is not None and patch is not None)
    ):
        raise GateSpecError(f"{text!r} is not a version (a number after a wildcard)")
    if major is None:
        minor = patch = None
    elif minor is None:
        patch = None
    pre = m.group(4) if patch is not None else None
    return major, minor, patch, pre


def _v(major, minor, patch, pre=None) -> SemVer:
    text = f"{major}.{minor}.{patch}" + (f"-{pre}" if pre else "")
    parsed = SemVer.parse(text)
    if parsed is None:  # e.g. a leading-zero prerelease `1.2.3-01` — never an assert (#457 review)
        raise GateSpecError(f"{text!r} is not a valid semver version")
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
        major, minor, patch, pre = _partial(token[1:], lenient=True)
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
        major, minor, patch, pre = _partial(token[1:].lstrip(">"), lenient=True)
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
            try:
                sets.append(_hyphen(hy.group(1), hy.group(2)))
            except GateSpecError:
                raise GateSpecError(f"{text!r} is not a semver range (bad hyphen range)") from None
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
    # node-semver drops a bare `>=0.0.0` (it is ANY), so a set left empty by that is ANY too.
    zero = SemVer(0, 0, 0)
    sets = [[(op, v) for op, v in comps if not (op == ">=" and v == zero)] for comps in sets]
    # node-semver: a set that is just ANY (`*`, `x`, ``, `>=*`) makes the whole range ANY,
    # so `* || 1.2.3-beta` does NOT admit 1.2.3-beta — the other sets are dropped.
    if any(not comps for comps in sets):
        return [[]]
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


def _gh_json(path: str, *, timeout: float = _GH_TIMEOUT_S, paginate: bool = False) -> tuple[int, object, str]:
    """``gh api <path>`` → ``(rc, parsed-json-or-None, stderr)``. Rides the same ``gh``
    credential the board already uses for every PR operation. ``paginate`` follows every
    page of a list endpoint and returns ONE list (``gh --paginate`` prints each page as its
    own JSON document)."""
    args = ["gh", "api", "-H", "Accept: application/vnd.github+json"]
    if paginate:
        args.append("--paginate")
    try:
        proc = subprocess.run(
            [*args, path],
            capture_output=True,
            text=True,
            timeout=timeout * (4 if paginate else 1),
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateCheckError(f"gh api failed: {exc}") from exc
    text = proc.stdout or ""
    data = None
    try:
        if paginate and text.strip():
            dec, idx, pages = json.JSONDecoder(), 0, []
            while idx < len(text):
                while idx < len(text) and text[idx].isspace():
                    idx += 1
                if idx >= len(text):
                    break
                page, idx = dec.raw_decode(text, idx)
                pages.append(page)
            data = [x for page in pages for x in page] if all(isinstance(pg, list) for pg in pages) else pages[-1]
        elif text.strip():
            data = json.loads(text)
    except ValueError:
        data = None
    return proc.returncode, data, (proc.stderr or "").strip()


def _not_found(rc: int, data, err: str) -> bool:
    return rc != 0 and ("404" in err or (isinstance(data, dict) and data.get("status") == "404"))


def _packument(spec: GateSpec, token: str) -> tuple[int, dict]:
    url = f"{NPM_REGISTRY}/{urllib.parse.quote(spec.target, safe='@')}"
    status, doc = _http_get_json(url, token=token)
    if status == 404:
        return 404, {}
    if status != 200 or not isinstance(doc, dict):
        hint = " — a private package needs project_board.npm_token" if status in (401, 403) else ""
        raise GateCheckError(f"registry answered HTTP {status} for {spec.target}{hint}")
    return 200, doc


def _live_versions(doc: dict) -> tuple[list[str], list[str]]:
    """``(live, deprecated)`` version strings — a deprecated version never satisfies a gate."""
    live, dead = [], []
    for v, meta in (doc.get("versions") or {}).items():
        (dead if isinstance(meta, dict) and meta.get("deprecated") else live).append(v)
    return live, dead


def _not_published_detail(spec: GateSpec, token: str) -> str:
    # npm answers a PRIVATE package with 404 to an anonymous read — indistinguishable from
    # "never published", so say so on a scoped package when no token is configured.
    hint = " — or private: set project_board.npm_token" if spec.target.startswith("@") and not token else ""
    return f"{spec.describe()} (not published yet{hint})"


def eval_npm(spec: GateSpec, *, token: str = "", resolve_card=None) -> dict:
    """A plain spec: a live (non-deprecated) published version satisfies the range — any
    version at all, prereleases included, when there is no range. A ``contains:`` spec:
    the newest live stable version's git tag is the anchor commit or a descendant of it."""
    if spec.anchor:
        return _eval_npm_contains(spec, token=token, resolve_card=resolve_card)
    status, doc = _packument(spec, token)
    what = spec.describe()
    if status == 404:
        return {"met": False, "detail": _not_published_detail(spec, token)}
    live, dead = _live_versions(doc)
    latest = str((doc.get("dist-tags") or {}).get("latest") or "")
    if not spec.constraint:
        parsed = [v for v in (SemVer.parse(x) for x in live) if v is not None]
        if parsed:
            best = max(parsed)
            return {"met": True, "detail": f"{what} ({best} published)", "version": str(best)}
        return {"met": False, "detail": f"{what} ({'only deprecated versions' if dead else 'none published'})"}
    best = max_satisfying(live, spec.constraint)
    if best is not None:
        return {"met": True, "detail": f"{what} ({best} published)", "version": str(best)}
    extra = "; only deprecated versions satisfy" if max_satisfying(dead, spec.constraint) else ""
    return {"met": False, "detail": f"{what} (latest {latest or 'none'}{extra})", "latest": latest}


def _anchor_sha(spec: GateSpec, resolve_card) -> tuple[str, str]:
    """``(sha, "")`` for the anchor commit, or ``("", why-unmet)``. A card id resolves to its
    PR's merge commit, and only once that PR has MERGED."""
    if _SHA_RE.match(spec.anchor):
        return spec.anchor, ""
    card = resolve_card(spec.anchor) if resolve_card else None
    if card is None:
        return "", f"card {spec.anchor} not found on this board"
    pr_url = str(card.get("pr_url") or "")
    m = re.search(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", pr_url)
    if not m:
        return "", f"card {spec.anchor} has no PR yet"
    if m.group(1).lower() != spec.anchor_repo.lower():
        raise GateCheckError(f"card {spec.anchor}'s PR is in {m.group(1)}, not {spec.anchor_repo}")
    rc, data, err = _gh_json(f"repos/{m.group(1)}/pulls/{m.group(2)}")
    if rc != 0 or not isinstance(data, dict):
        raise GateCheckError(f"GitHub PR lookup failed for {pr_url}: {err or f'rc={rc}'}")
    if not data.get("merged") or not data.get("merge_commit_sha"):
        return "", f"card {spec.anchor} not merged yet (#{m.group(2)})"
    return str(data["merge_commit_sha"]), ""


def _tag_commit(slug: str, tag: str) -> str:
    """The commit a tag points at (annotated tags dereferenced), or "" when there is no tag."""
    rc, data, err = _gh_json(f"repos/{slug}/git/ref/tags/{urllib.parse.quote(tag, safe='')}")
    if _not_found(rc, data, err):
        return ""
    if rc != 0 or not isinstance(data, dict):
        raise GateCheckError(f"GitHub tag lookup failed for {slug} {tag}: {err or f'rc={rc}'}")
    obj = data.get("object") or {}
    for _hop in range(3):  # a tag object may point at another tag object
        if obj.get("type") != "tag":
            break
        rc, tagobj, err = _gh_json(f"repos/{slug}/git/tags/{obj.get('sha')}")
        if rc != 0 or not isinstance(tagobj, dict):
            raise GateCheckError(f"GitHub tag read failed for {slug} {tag}: {err or f'rc={rc}'}")
        obj = tagobj.get("object") or {}
    return str(obj.get("sha") or "")


def _contains(slug: str, anchor: str, commit: str) -> bool:
    """Is ``commit`` the anchor or a descendant of it? (``compare/<anchor>...<commit>``)."""
    rc, data, err = _gh_json(f"repos/{slug}/compare/{anchor}...{commit}")
    if rc != 0 or not isinstance(data, dict):
        raise GateCheckError(f"GitHub compare failed for {slug} {anchor[:12]}...{commit[:12]}: {err or f'rc={rc}'}")
    return str(data.get("status") or "") in ("ahead", "identical")


def _eval_npm_contains(spec: GateSpec, *, token: str, resolve_card) -> dict:
    what = spec.describe()
    anchor, why = _anchor_sha(spec, resolve_card)
    if not anchor:
        return {"met": False, "detail": f"{what} ({why})"}
    status, doc = _packument(spec, token)
    if status == 404:
        return {"met": False, "detail": _not_published_detail(spec, token)}
    live, _dead = _live_versions(doc)
    stable = sorted((v for v in (SemVer.parse(x) for x in live) if v is not None and not v.prerelease), reverse=True)
    if not stable:
        return {"met": False, "detail": f"{what} (no stable version published)"}
    newest = stable[0]
    # changesets tags `<package>@<version>`; a single-package repo tags `v<version>`.
    commit, tag = "", ""
    for cand in (f"{spec.target}@{newest}", f"v{newest}", str(newest)):
        commit = _tag_commit(spec.anchor_repo, cand)
        if commit:
            tag = cand
            break
    if not commit:
        return {"met": False, "detail": f"{what} (latest {newest} has no tag in {spec.anchor_repo} to prove it)"}
    short = anchor[:12]
    if _contains(spec.anchor_repo, anchor, commit):
        return {"met": True, "detail": f"{what} ({newest} published, contains {short})", "version": str(newest)}
    # The NEWEST publish is checked: releases are cut from one linear main, so an older
    # version never contains a commit the newest lacks.
    return {"met": False, "detail": f"{what} (latest {newest}, tag {tag} at {commit[:12]}, lacks {short})"}


def _release_versions(spec: GateSpec) -> tuple[list[SemVer], list[str]]:
    """``(versions, package-scoped tag names)`` of the repo's published releases: not
    drafts, not GitHub prereleases, every page."""
    rc, data, err = _gh_json(f"repos/{spec.target}/releases?per_page=100", paginate=True)
    if rc != 0 or not isinstance(data, list):
        raise GateCheckError(f"GitHub releases lookup failed for {spec.target}: {err or f'rc={rc}'}")
    versions, scoped = [], []
    for rel in data:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "")
        pkg, at, ver = tag.rpartition("@")
        if spec.package:
            if not at or pkg.lower() != spec.package:
                continue
        elif at and pkg:
            scoped.append(tag)
            continue
        parsed = SemVer.parse(ver if at else tag)
        if parsed is not None:
            versions.append(parsed)
    return versions, scoped


def scoped_tag_refusal(spec: GateSpec, scoped: list[str]) -> str:
    return (
        f"{spec.target} tags releases per package (e.g. {scoped[0]}), so a bare range would match ANY "
        f"package's version — name the package: release:{spec.target}@<package>@{spec.constraint}"
    )


def eval_release(spec: GateSpec) -> dict:
    """An exact tag must exist; a range must be satisfied by a published release's tag
    (only ``<package>@x.y.z`` tags when the spec names a package)."""
    what = spec.describe()
    if not spec.is_range:
        rc, data, err = _gh_json(f"repos/{spec.target}/git/ref/tags/{urllib.parse.quote(spec.constraint, safe='')}")
        if rc == 0 and isinstance(data, dict) and data.get("ref"):
            return {"met": True, "detail": f"{what} (tag exists)"}
        if _not_found(rc, data, err):
            return {"met": False, "detail": f"{what} (no such tag yet)"}
        raise GateCheckError(f"GitHub tag lookup failed for {spec.target}: {err or f'rc={rc}'}")
    versions, scoped = _release_versions(spec)
    if scoped:
        # A bare range on a repo that tags per package (changesets): never met — any
        # package's version would satisfy it (#457 review, B1).
        return {"met": False, "detail": f"{what} ({scoped_tag_refusal(spec, scoped)})"}
    best = max_satisfying(versions, spec.constraint)
    if best is not None:
        return {"met": True, "detail": f"{what} ({best} released)", "version": str(best)}
    newest = max(versions) if versions else None
    return {"met": False, "detail": f"{what} (latest release {newest or 'none'})"}


def remote_refusal(spec: GateSpec) -> str:
    """Why ``spec`` must be refused at WRITE time given what the remote looks like, or "".
    Today: a bare ``release:`` range on a repo that tags per package. Best-effort — a
    failed read is not a refusal (evaluation catches the same case, visibly)."""
    if spec.kind != KIND_RELEASE or not spec.is_range or spec.package:
        return ""
    try:
        versions, scoped = _release_versions(spec)
    except Exception:  # noqa: BLE001 — best-effort; the evaluator re-detects it
        return ""
    return scoped_tag_refusal(spec, scoped) if scoped else ""


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
# Scheduling runs on the MONOTONIC clock (a wall-clock jump never stalls or floods the
# checks); ``checked_at`` in a result is wall time, for people.
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
    except Exception:  # noqa: BLE001 — a listing read never raises
        return str(text or "").strip()


def _due(entry: dict | None, now: float, force: bool) -> bool:
    if entry is None:
        return True
    if force:
        return now - entry["sched_at"] >= FORCE_MIN_INTERVAL_S
    return now >= entry["next_at"]


def _unreadable(item, exc: Exception) -> dict:
    msg = str(exc) or type(exc).__name__
    return {"spec": str(item), "kind": "", "met": False, "detail": msg, "error": msg, "checked_at": 0.0}


def evaluate(specs, *, token: str = "", force: bool = False, now: float | None = None, resolve_card=None) -> list[dict]:
    """Evaluate every spec (text or GateSpec), reusing a fresh cached result. NEVER
    raises: an unparseable spec or a failed check is an UNMET result carrying ``error``.
    ``resolve_card`` (card id → projected card or None) resolves a ``contains:`` anchor
    that names a board card; without it such a gate reads unmet.

    Each result: ``{spec, kind, met, detail, error, checked_at}``."""
    out = []
    for item in specs or ():
        try:
            spec = item if isinstance(item, GateSpec) else parse_spec(item)
            out.append(_evaluate_one(spec, token=token, force=force, now=now, resolve_card=resolve_card))
        except Exception as exc:  # noqa: BLE001 — one bad gate is unmet, never a crash of the scan
            out.append(_unreadable(item, exc))
    return out


def _evaluate_one(spec: GateSpec, *, token: str, force: bool, now: float | None, resolve_card=None) -> dict:
    t = time.monotonic() if now is None else now
    wall = time.time() if now is None else now
    with _LOCK:
        entry = _CACHE.get(spec.raw)
        if not _due(entry, t, force):
            return dict(entry["result"])
        failures = entry["failures"] if entry else 0
        was_met = bool(entry and entry["result"]["met"])
    error = ""
    try:
        if spec.kind == KIND_NPM:
            verdict = eval_npm(spec, token=token, resolve_card=resolve_card)
        elif spec.kind == KIND_RELEASE:
            verdict = eval_release(spec)
        else:
            verdict = eval_pr(spec)
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
        "checked_at": wall,
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
        _CACHE[spec.raw] = {"result": result, "sched_at": t, "next_at": t + delay, "failures": failures}
    return dict(result)


def card_status(feature: dict) -> list[dict]:
    """Every gate on ``feature`` with its CACHED verdict (no network) — an unchecked gate
    reads unmet with detail ``… (not checked yet)``, an unparseable one unmet with its
    error. Never raises: this runs inside every listing."""
    out = []
    for raw in feature.get("waits_for") or ():
        try:
            hit = cached(raw)
            if hit is None:
                what = parse_spec(raw).describe()
                hit = {"spec": raw, "kind": "", "met": False, "detail": f"{what} (not checked yet)", "error": ""}
                hit["checked_at"] = 0.0
        except Exception as exc:  # noqa: BLE001
            hit = _unreadable(raw, exc)
        out.append(hit)
    return out


def unmet_sentence(results) -> str:
    """``waiting on publish: npm @x/y >=1.0.0 (latest 0.9.0); pr o/r#4 (open)`` — or ""."""
    unmet = [r["detail"] for r in results or () if not r.get("met")]
    return NEXT_ACTION_PREFIX + "; ".join(unmet) if unmet else ""
