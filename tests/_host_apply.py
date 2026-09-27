"""A host-config double for ``HOST.apply_settings`` that covers the WHOLE patch (#452).

``tests/test_project_registry.py::apply_like_host`` models only the ``project_board``
section. Since #452 a register's patch also carries the host's top-level ``projects:`` list
(ADR 0095) and, with an explicit fence override, ``filesystem.projects`` — so this double
holds the config as the YAML DOCUMENT and applies a patch with the host's own rules
(``apply_updates`` below mirrors ``graph.config_io.apply_updates_to_yaml`` line for line).
``tests/test_host_apply_conformance.py`` runs the two side by side against the real host
function whenever protoAgent is importable, so the double cannot drift from the host unseen.

The attributes the plugin reads (``projects``, ``filesystem_projects``, ``plugin_config``,
``onboarding_*``) are PROJECTED from the document on every read, the way the host rebuilds
``LangGraphConfig`` from YAML after an apply.
"""

from __future__ import annotations

import copy
import sys
import types
from typing import Any

_DELETE = None


def apply_updates(doc: dict, updates: dict) -> dict:
    """``graph.config_io.apply_updates_to_yaml`` on a plain dict: a section merges, a
    section-member map merges, the innermost value is ASSIGNED (a list — the top-level
    ``projects`` included — is replaced wholesale), and ``None`` deletes a nested key."""
    for section, values in updates.items():
        if not isinstance(values, dict):
            doc[section] = values
            continue
        if section not in doc or not isinstance(doc.get(section), dict):
            doc[section] = {}
        for key, val in values.items():
            if val is _DELETE:
                if isinstance(doc[section], dict) and key in doc[section]:
                    del doc[section][key]
                continue
            if isinstance(val, dict):
                if key not in doc[section] or not isinstance(doc[section].get(key), dict):
                    doc[section][key] = {}
                for inner_key, inner_val in val.items():
                    if inner_val is _DELETE:
                        if inner_key in doc[section][key]:
                            del doc[section][key][inner_key]
                        continue
                    doc[section][key][inner_key] = inner_val
            else:
                doc[section][key] = val
    return doc


class HostConfig:
    """The live config, projected from ``doc`` like ``LangGraphConfig.from_dict``."""

    def __init__(self, doc: dict, *, registry: bool = True):
        self.doc = doc
        self.registry = registry  # False = a pre-0.115.0 host with no `projects` attribute
        self.patches: list[dict] = []

    def __getattr__(self, name: str) -> Any:
        doc = self.__dict__["doc"]
        if name == "projects":
            if not self.__dict__["registry"]:
                raise AttributeError(name)
            return list(doc.get("projects", []) or [])
        if name == "filesystem_projects":
            return list((doc.get("filesystem") or {}).get("projects", []) or [])
        if name == "filesystem_enabled":
            return bool((doc.get("filesystem") or {}).get("enabled", True))
        if name == "plugin_config":
            return {"project_board": copy.deepcopy(doc.get("project_board") or {})}
        if name == "onboarding_enabled":
            return bool((doc.get("onboarding") or {}).get("enabled", True))
        if name == "onboarding_root":
            return str((doc.get("onboarding") or {}).get("root", "") or "")
        raise AttributeError(name)

    def apply_settings(self, patch: dict):
        self.patches.append(copy.deepcopy(patch))
        apply_updates(self.doc, copy.deepcopy(patch))
        return True, []


def wire_host(monkeypatch, host: HostConfig) -> None:
    """Install ``host`` as ``graph.sdk.config()`` and ``graph.plugins.host.HOST``."""
    fake_sdk = types.ModuleType("graph.sdk")
    fake_sdk.config = lambda: host
    fake_plugins = types.ModuleType("graph.plugins")
    fake_plugins.__path__ = []
    fake_host = types.ModuleType("graph.plugins.host")
    fake_host.HOST = types.SimpleNamespace(apply_settings=host.apply_settings)
    monkeypatch.setitem(sys.modules, "graph.sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "graph.plugins", fake_plugins)
    monkeypatch.setitem(sys.modules, "graph.plugins.host", fake_host)
