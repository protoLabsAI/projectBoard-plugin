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


# ── the config-key extractor ────────────────────────────────────────────────────────
#
# The first version of this scanned eleven HARDCODED files for the single pattern
# ``cfg.get("…")``. It passed while SIX real knobs were undocumented — `auto_merge`,
# `max_concurrent`, `max_concurrent_sessions`, `max_pending_reviews`, `project` and
# `webhook_secret` — because the code reads config through a vocabulary that regex
# cannot see:
#
#     _knob_int(self.cfg, "max_concurrent", 1, floor=1)     # helper call, not cfg.get(
#     knob_bool(cfg, "auto_merge", False)                   # ditto
#     (cfg or {}).get("webhook_secret")                     # the `or {}` breaks the anchor
#     section.get("default_project")                        # a different receiver name
#
# Three of those six are the knobs an operator actually turns, and `max_concurrent_sessions`
# is the one the boot log tells them to set. A guard that reports success while the thing
# it guards is broken is the same failure this repo burned an epic on for external seams
# (see tests/test_external_seams.py) — it is worse than no guard, because README told
# readers it was trustworthy.
#
# So: walk the AST of EVERY plugin module (no file list to fall out of date), and match
# every shape by which a config value actually reaches the code.

# Expressions that ARE the plugin's config dict, by their source text.
_CFG_RECEIVERS = {"cfg", "self.cfg", "cfg or {}", "section", "persisted_section", "self._cfg", "_cfg"}

# The coercion helpers, whose signature is ``(cfg, key, default, …)`` — the key is arg 1.
_KNOB_HELPERS = {"knob_bool", "knob_int", "knob_str", "_knob_bool", "_knob_int", "_knob_str"}

# Module constants that ENUMERATE knob names (LIVE_KNOBS, LOOP_RESTART_KEYS,
# _PROJECT_SETTING_KEYS, LIVE_KNOB_FLOORS…). A key reached only through one of these —
# ``_knob_bool(section, key, cur)`` with a variable key — is invisible at the call site.
# Matched on config-denoting names so an unrelated list like coder_seam's `_LOCATION_KEYS`
# (ACP location fields, not config) cannot smuggle in seven false positives.
_KEY_LIST_NAMES = re.compile(r"KNOB|SETTING|RESTART_KEYS")


def _plugin_modules() -> list[pathlib.Path]:
    """Every module the plugin ships — globbed, not listed, so a new one is covered the
    day it lands. `projects.py` was never in the old hardcoded list, which is why
    `default_project` reached production documented nowhere at all."""
    return [
        p
        for p in sorted(_ROOT.rglob("*.py"))
        if ".venv" not in p.parts and "tests" not in p.parts and "__pycache__" not in str(p)
    ]


def _config_keys() -> set[str]:
    keys: set[str] = set()
    for path in _plugin_modules():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover — a broken module fails its own tests
            continue
        for node in ast.walk(tree):
            # <cfg>.get("key")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and ast.unparse(node.func.value) in _CFG_RECEIVERS
            ):
                keys.add(node.args[0].value)
            # <cfg>["key"]
            if (
                isinstance(node, ast.Subscript)
                and ast.unparse(node.value) in _CFG_RECEIVERS
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                keys.add(node.slice.value)
            # knob_*(cfg, "key", default)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _KNOB_HELPERS
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and ast.unparse(node.args[0]) in _CFG_RECEIVERS
            ):
                keys.add(node.args[1].value)
            # LIVE_KNOBS = ("max_concurrent", …) / LIVE_KNOB_FLOORS = {"max_concurrent": 1}
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if not (isinstance(target, ast.Name) and _KEY_LIST_NAMES.search(target.id)):
                        continue
                    if isinstance(node.value, (ast.Tuple, ast.List, ast.Dict)):
                        elements = node.value.keys if isinstance(node.value, ast.Dict) else node.value.elts
                        for element in elements:
                            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                                keys.add(element.value)
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


def _documented_config_keys() -> set[str]:
    """The keys the reference actually TABULATES.

    Matched on ``| `key` | `default` |`` — a config row always carries a backticked
    default in its second cell, which is what separates it from the legend rows at the
    top of the file (``| `live` | picked up by the running loop … |``). Counting those
    as knobs is how a naive reader — or a naive script — gets 59 instead of 57."""
    return set(re.findall(r"^\| `([a-z_0-9]+)` \| `", _CONFIG_DOC.read_text(), re.M))


def _manifest_declared_keys() -> set[str]:
    """Everything ``protoagent.plugin.yaml`` declares: the ``config:`` defaults plus the
    ``settings:`` console fields. A key outside this set is YAML-only — `POST
    /api/settings` refuses it and the console cannot render it."""
    import yaml

    manifest = yaml.safe_load((_ROOT / "protoagent.plugin.yaml").read_text())
    declared = set(manifest.get("config") or {})
    for field in manifest.get("settings") or []:
        if isinstance(field, dict) and "key" in field:
            declared.add(field["key"])
    return declared


def test_the_configuration_reference_names_no_key_the_code_stopped_reading():
    """The mirror of the coverage test, and the half that was missing.

    Routes had both directions from the start; config keys and tools had only "is it
    absent?". So a knob could be deleted from the code and stay in the reference forever —
    and a documented knob that does nothing is the failure mode that costs the most trust,
    because the reader sets it, sees no effect, and concludes the board is broken."""
    stale = sorted(_documented_config_keys() - _config_keys())
    assert not stale, (
        f"docs/configuration.md documents {len(stale)} key(s) the code no longer reads: "
        f"{stale}. Remove the row, or restore the read — a knob that silently does nothing "
        f"is worse than one that was never written down."
    )


def test_the_tools_reference_names_no_tool_that_was_removed():
    """Same asymmetry, same fix, for the agent tools. These are callable by any agent on
    the instance, so a stale entry advertises a capability that no longer exists — the
    agent calls it, gets an error it cannot act on, and the doc gets the blame last."""
    doc = _TOOLS_DOC.read_text()
    documented = set(re.findall(r"\| `(board_[a-z_]+)`", doc))
    stale = sorted(documented - _tools())
    assert not stale, (
        f"docs/tools.md documents {len(stale)} tool(s) that no longer exist: {stale}. "
        f"Remove the row — an agent that calls a documented-but-absent tool has no way to "
        f"tell a doc bug from its own."
    )


def test_the_yaml_only_count_in_the_prose_is_true():
    """`docs/configuration.md` states, in prose, how many keys the Settings UI cannot
    reach. That number was written as "18 of 56" and was stale within days — the file had
    57 knobs and 19 of them were YAML-only, and nothing noticed, in the one document whose
    entire job is being complete.

    A hand-maintained count is a fact like any other: either something checks it, or it
    drifts. This recomputes it and compares."""
    doc = _CONFIG_DOC.read_text()
    documented = _documented_config_keys()
    yaml_only = documented - _manifest_declared_keys()
    match = re.search(r"\*\*(\d+) of (\d+) keys are in this state", doc)
    assert match, (
        "docs/configuration.md no longer states the YAML-only count in the form "
        "'**N of M keys are in this state' — restore it or update this test; the number is "
        "the reader's cue that the Settings UI is not the whole surface."
    )
    claimed = (int(match.group(1)), int(match.group(2)))
    actual = (len(yaml_only), len(documented))
    assert claimed == actual, (
        f"docs/configuration.md claims {claimed[0]} of {claimed[1]} keys are YAML-only; the "
        f"file and the manifest say {actual[0]} of {actual[1]}. Update the sentence. "
        f"YAML-only today: {sorted(yaml_only)}"
    )
