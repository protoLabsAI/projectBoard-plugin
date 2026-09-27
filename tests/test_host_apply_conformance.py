"""The host-config double (tests/_host_apply.py) must apply a patch exactly as the host does.

The plugin suite is host-free, so the managed-project write (#452) is tested against a double
of ``HOST.apply_settings``. A double that disagrees with the host is how #408 shipped: a
delete that passed against a fake with the wrong merge rule and deleted nothing for real. So
this runs the double and the REAL ``graph.config_io.apply_updates_to_yaml`` over the same
documents and patches — the exact patch shapes the registry sends — and requires the same
result.

It needs a protoAgent checkout (``PB_PROTOAGENT_SRC=<path>``) and SKIPS otherwise, which is
why ``_apply_registry`` stays UNCOVERED in the seam ratchet: this is a conformance check a
developer runs, not a CI tier. The host function is lifted out of ``graph/config_io.py`` by
AST and executed as written — importing the module would pull the whole host in, and the suite
stubs the ``graph`` package anyway (tests/conftest.py).
"""

from __future__ import annotations

import ast
import copy
import os
import typing
from pathlib import Path

import pytest
from _host_apply import apply_updates


def _host_apply_updates():
    src_root = os.environ.get("PB_PROTOAGENT_SRC", "").strip()
    path = Path(src_root, "graph", "config_io.py") if src_root else None
    if path is None or not path.is_file():
        pytest.skip("set PB_PROTOAGENT_SRC to a protoAgent checkout to run the host conformance check")
    tree = ast.parse(path.read_text())
    keep = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name == "apply_updates_to_yaml")
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_DELETE" for t in n.targets))
    ]
    assert len(keep) == 2, "graph/config_io.py no longer defines apply_updates_to_yaml + _DELETE as expected"
    ns: dict = {"Any": typing.Any}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns)
    return ns["apply_updates_to_yaml"]


_DOC = {
    "onboarding": {"enabled": True, "root": "/ws"},
    "filesystem": {"enabled": True, "projects": [{"name": "notes", "path": "/n", "write": True}]},
    "projects": [{"name": "protoAgent", "path": "/pa", "write": False}],
    "project_board": {
        "coder": "proto",
        "default_project": "a",
        "projects": {"a": {"repo": "/a", "coders": {"smart": "x"}}, "b": {"repo": "/b"}},
    },
}

_PATCHES = [
    # register: board entry + registry list + fence mirror, in one patch
    {
        "projects": [
            {"name": "protoAgent", "path": "/pa", "write": False},
            {"name": "c", "path": "/c", "default_branch": "main", "github": "o/c", "write": False},
        ],
        "filesystem": {
            "projects": [
                {"name": "notes", "path": "/n", "write": True},
                {"name": "c", "path": "/c", "write": False},
            ]
        },
        "project_board": {
            "projects": {"a": {"repo": "/a", "coders": {"smart": "x"}}, "c": {"repo": "/c", "managed_project": "c"}},
            "default_project": "a",
        },
    },
    # unregister: a None deletes the board member; the registry list is assigned wholesale
    {
        "projects": [{"name": "protoAgent", "path": "/pa", "write": False}],
        "project_board": {"projects": {"a": {"repo": "/a"}, "b": None}, "default_project": "a"},
    },
    # the board half alone (a host without the registry)
    {"project_board": {"projects": {"b": {"repo": "/b2", "base_branch": "dev"}}}},
]


@pytest.mark.parametrize("patch", _PATCHES)
def test_the_double_applies_a_patch_exactly_as_the_host_does(patch):
    ours = apply_updates(copy.deepcopy(_DOC), copy.deepcopy(patch))
    theirs = _host_apply_updates()(copy.deepcopy(_DOC), copy.deepcopy(patch))
    assert ours == theirs
