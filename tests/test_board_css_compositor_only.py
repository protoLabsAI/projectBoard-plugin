"""Every INFINITE animation on the board view animates only transform/opacity (#686).

An infinite animation that touches anything else (box-shadow, background-position, width, …)
makes the page repaint every frame for as long as the board is open. The building-card ping
animated box-shadow, and with cards building WKWebView grew ~50 MB/min and froze the desktop
console (2026-11-08). transform and opacity are composited, so they need no repaint."""

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "view" / "board.css").read_text()


def _keyframes() -> dict[str, str]:
    out = {}
    for m in re.finditer(r"@keyframes\s+([\w-]+)\s*\{", CSS):
        depth, j = 1, m.end()
        while depth and j < len(CSS):
            depth += {"{": 1, "}": -1}.get(CSS[j], 0)
            j += 1
        out[m.group(1)] = CSS[m.end() : j - 1]
    return out


def _infinite_names() -> set[str]:
    names = set()
    for decl in re.findall(r"animation\s*:\s*([^;}]+)", CSS):
        if "infinite" in decl:
            names.add(decl.split()[0])
    return names


def test_infinite_animations_touch_only_transform_and_opacity():
    frames = _keyframes()
    bad = {}
    for name in _infinite_names():
        body = frames.get(name)
        if body is None:
            continue  # defined elsewhere (or nowhere): nothing on this page animates it
        props = {p.strip() for p in re.findall(r"([a-z-]+)\s*:", body)}
        extra = props - {"transform", "opacity"}
        if extra:
            bad[name] = sorted(extra)
    assert bad == {}, f"infinite animations repaint every frame: {bad}"


def test_the_guard_sees_the_building_ping_and_the_pending_sweep():
    assert {"bld-ping", "pending-sweep", "bld-sweep"} <= _infinite_names()
