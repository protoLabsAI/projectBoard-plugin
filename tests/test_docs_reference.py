"""The reference docs must describe the code that exists.

A README that is merely INCOMPLETE costs a reader time. One that is WRONG costs them
trust, and this repo had both: the Layout table still named `loop.py` after #268 split it
into a package, `coders` was documented as one delegate per tier after #362 made it a
list, and 7 of 28 routes plus 10 of 16 agent tools had never been documented at all —
including every destructive one.

Docs drift because nothing fails when they do. These tests fail.

They deliberately check COVERAGE, not prose: that every route and tool the code exposes
appears in its reference, and that the reference names nothing that no longer exists.
What each one MEANS is a human's job — this only guarantees the list is honest.
"""

from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API_DOC = _ROOT / "docs" / "api.md"
_TOOLS_DOC = _ROOT / "docs" / "tools.md"
_CONFIG_DOC = _ROOT / "docs" / "configuration.md"


def _routes() -> set[tuple[str, str]]:
    """(VERB, path) for every route `api.py` registers, by AST — so a rename or a new
    decorator cannot slip past a text search."""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse((_ROOT / "api.py").read_text())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            verb = getattr(dec.func, "attr", "")
            owner = getattr(getattr(dec.func, "value", None), "id", "")
            if verb in ("get", "post", "put", "patch", "delete") and owner == "router":
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    found.add((verb.upper(), dec.args[0].value))
    return found


def _tools() -> set[str]:
    return {
        n.name
        for n in ast.walk(ast.parse((_ROOT / "__init__.py").read_text()))
        if isinstance(n, ast.FunctionDef) and n.name.startswith("board_")
    }


def test_every_route_is_in_the_api_reference():
    doc = _API_DOC.read_text()
    missing = sorted(f"{v} {p}" for v, p in _routes() if f"| `{v}` | `{p}` |" not in doc)
    assert not missing, (
        f"{len(missing)} route(s) are not in docs/api.md: {missing}. A public HTTP surface "
        f"that is not written down is not usable by anyone outside this repo — add the row, "
        f"with what the route is FOR, not just its shape."
    )


def test_the_api_reference_names_no_route_that_was_removed():
    doc = _API_DOC.read_text()
    live = {f"| `{v}` | `{p}` |" for v, p in _routes()}
    documented = set(re.findall(r"^\| `(?:GET|POST|PUT|PATCH|DELETE)` \| `[^`]+` \|", doc, re.M))
    stale = sorted(r for r in documented if not any(r == lv[: len(r)] for lv in live))
    assert not stale, (
        f"docs/api.md documents {len(stale)} route(s) that no longer exist: {stale}. A doc "
        f"that describes a removed endpoint is worse than a missing one — a reader builds "
        f"against it and only finds out at runtime."
    )


def test_every_agent_tool_is_in_the_tools_reference():
    doc = _TOOLS_DOC.read_text()
    missing = sorted(t for t in _tools() if f"`{t}`" not in doc)
    assert not missing, (
        f"{len(missing)} tool(s) are not in docs/tools.md: {missing}. These are callable by "
        f"any agent on the instance; an undocumented one is a capability nobody can audit."
    )


def test_the_readme_layout_table_matches_the_real_files():
    """The Layout table is the first thing a contributor reads to find their way around,
    and it was stale within a day of the #268 package split."""
    readme = (_ROOT / "README.md").read_text()
    layout = readme.split("## Layout", 1)
    assert len(layout) == 2, "README lost its Layout section"
    body = layout[1].split("\n## ", 1)[0]
    for name in re.findall(r"^\| `([a-z_/.]+)` \|", body, re.M):
        target = _ROOT / name
        assert target.exists(), (
            f"README's Layout table names `{name}`, which does not exist. #268 renamed "
            f"loop.py → loop/ and the table kept the old name — that is exactly the drift "
            f"this catches."
        )


def _config_keys() -> set[str]:
    """Every ``cfg.get("…")`` key the plugin reads, across the loop package and the
    modules around it. Text-scanned rather than AST'd because the reads are spread over
    eleven files and the pattern is unambiguous."""
    keys: set[str] = set()
    for name in (
        "loop/core.py",
        "loop/_common.py",
        "loop/drive.py",
        "loop/reconcile.py",
        "loop/preflight.py",
        "store.py",
        "config.py",
        "setup_check.py",
        "api.py",
        "__init__.py",
        "br_fetch.py",
    ):
        path = _ROOT / name
        if path.exists():
            keys |= set(re.findall(r'(?:self\.)?cfg\.get\(\s*"([a-z_0-9]+)"', path.read_text()))
    return keys


def test_every_config_key_the_code_reads_is_documented():
    """A knob nobody can find is a knob nobody can use.

    18 of 56 keys were not even in the plugin manifest — including `coders` and
    `projects`, the two a multi-repo board cannot work without — so `POST /api/settings`
    refused them and the console never rendered them. Being YAML-only is a legitimate
    choice; being UNDISCOVERABLE is not."""
    doc = _CONFIG_DOC.read_text()
    missing = sorted(k for k in _config_keys() if f"`{k}`" not in doc)
    assert not missing, (
        f"{len(missing)} config key(s) are read by the code but absent from "
        f"docs/configuration.md: {missing}. Add them with a default and whether the change "
        f"is live, reload or restart — a stranger configures this board from that table."
    )


def test_the_readme_never_documents_a_nonexistent_server_subcommand():
    """The Install section told a first-time reader to run, as step 2 of 2:

        python -m server plugin enable project_board

    There is no `enable` subcommand. A stranger following the README hit
    `error: argument cmd: invalid choice: 'enable'` on the second line of setup — the
    worst place to be wrong, and invisible to every test because nothing executes the
    README.

    This pins the `python -m server plugin <cmd>` invocations the README claims against
    the subcommands that actually exist. It is a coarse guard (it does not run the host)
    but it catches the failure that occurred: a plausible verb that was never implemented.
    """
    readme = (_ROOT / "README.md").read_text()
    claimed = set(re.findall(r"python -m server plugin ([a-z][a-z-]*)", readme))
    # The real subcommand list, from `python -m server plugin --help` on protoAgent 0.155.x.
    real = {
        "new",
        "new-bundle",
        "install",
        "list",
        "uninstall",
        "update-bundle",
        "uninstall-bundle",
        "sync",
        "install-deps",
    }
    invented = sorted(claimed - real)
    assert not invented, (
        f"README documents `python -m server plugin {invented}` — not a real subcommand. "
        f"Valid: {sorted(real)}. A setup step that cannot be executed is worse than a "
        f"missing one: the reader assumes they made the mistake."
    )
