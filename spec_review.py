"""Opt-in, advisory two-stage spec review. No admission or delivery controls."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
import uuid
from pathlib import Path

from langchain_core.tools import tool

FIELDS = (
    "title",
    "spec",
    "acceptance_criteria",
    "design",
    "files_to_modify",
    "difficulty",
    "project",
    "depends_on",
    "issue_type",
)
MAX_INPUT = 64000
MAX_OUTPUT = 16000
TIMEOUT = 120
NAMES = ("board_spec_comprehension", "board_spec_readiness")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def snapshot(card):
    return {key: card.get(key) for key in FIELDS}


def input_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def repo_tools(root):
    """Bounded UTF-8 reads/searches; resolved paths cannot escape the project root."""
    root = Path(root).resolve(strict=True)

    def resolve(path):
        target = (root / path).resolve(strict=True)
        if not target.is_relative_to(root) or ".git" in target.relative_to(root).parts:
            raise ValueError("path outside review repository")
        return target

    @tool
    def spec_review_read(path: str) -> str:
        """Read a repo-relative UTF-8 file, at most 16000 bytes; cite path and line numbers."""
        with resolve(path).open("rb") as stream:
            data = stream.read(16001)
        return data[:16000].decode("utf-8", errors="replace") + ("\n[truncated]" if len(data) > 16000 else "")

    @tool
    def spec_review_search(query: str, path: str = ".") -> str:
        """Search literal text under a repo-relative path; bounded to 500 files and 80 matches."""
        base = resolve(path)
        matches = []
        candidates = base.rglob("*") if base.is_dir() else [base]
        scanned = 0
        for item in candidates:
            if scanned >= 500 or len(matches) >= 80:
                break
            if not item.is_file() or ".git" in item.relative_to(root).parts:
                continue
            scanned += 1
            try:
                target = resolve(str(item.relative_to(root)))
                with target.open("rb") as stream:
                    content = stream.read(16000).decode("utf-8")
                for number, line in enumerate(content.splitlines(), 1):
                    if query in line:
                        matches.append(f"{item.relative_to(root)}:{number}: {line[:300]}")
                        if len(matches) >= 80:
                            break
            except (OSError, UnicodeError, ValueError):
                continue
        return canonical({"matches": matches, "coverage": "partial; 500 files/16000 bytes each/80 matches maximum"})

    return [spec_review_read, spec_review_search]


def runtime():
    from graph import sdk
    from .subagents import SPEC_COMPREHENSION_CONFIG, SPEC_READINESS_CONFIG

    run = sdk.run_subagent
    if "extra_tools" not in inspect.signature(run).parameters:
        raise ValueError("SDK lacks explicit extra_tools support")
    for config in (SPEC_COMPREHENSION_CONFIG, SPEC_READINESS_CONFIG):
        if config is None or config.tools != [] or getattr(config, "inherit_read_tools", None) is not False:
            raise ValueError("host cannot declare tools=[] and inherit_read_tools=False")
    return run


async def stage(run, name, payload, extra_tools):
    started = time.monotonic()
    result = {
        "status": "gap",
        "tool_policy": {"tools": [], "inherit_read_tools": False, "extra_tools": [t.name for t in extra_tools]},
        "metrics": {"tokens": None, "cost": None},
    }
    try:
        raw = await asyncio.wait_for(run(name, canonical(payload), extra_tools=extra_tools), TIMEOUT)
        if not isinstance(raw, str) or len(raw.encode()) > MAX_OUTPUT:
            raise ValueError("worker output must be bounded JSON text")
        parsed = json.loads(raw)
        required = (
            ("proposal", "current_behavior", "invariants", "missing_decisions")
            if name == NAMES[0]
            else (
                "sources",
                "ambiguities",
                "simpler_alternatives",
                "uncertainties",
                "dispositions",
            )
        )
        if not isinstance(parsed, dict) or any(not isinstance(parsed.get(k), list) for k in required):
            raise ValueError("malformed worker output: expected all requested array fields")
        if name == NAMES[1] and (
            not parsed["sources"]
            or any(
                not isinstance(source, dict)
                or not isinstance(source.get("path"), str)
                or not isinstance(source.get("line"), int)
                or source["line"] < 1
                or not isinstance(source.get("observation"), str)
                for source in parsed["sources"]
            )
        ):
            raise ValueError("readiness lacks structured source references")
        result.update(status="reported", result=parsed)
    except TimeoutError:
        result["gap"] = "worker timeout"
    except Exception as exc:
        result["gap"] = str(exc)[:1000]
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


async def review(store, feature_id):
    card = store.get_feature(feature_id)
    if not card:
        return {"status": "refused", "reason": "unknown card"}
    if card.get("issue_type") != "feature" or str(card.get("difficulty", "")).lower() not in {
        "medium",
        "large",
        "hard",
    }:
        return {"status": "refused", "reason": "requires a medium/large (or hard) coding feature"}
    snap = snapshot(card)
    if len(canonical(snap).encode()) > MAX_INPUT:
        return {"status": "refused", "reason": "authoring snapshot exceeds 64000 bytes"}
    started = time.monotonic()
    report = {
        "schema": "spec-review",
        "version": 1,
        "run_id": str(uuid.uuid4()),
        "feature_id": feature_id,
        "input_hash": input_hash(snap),
        "snapshot": snap,
        "advisory": True,
        "status": "gaps",
        "freshness": "unchecked",
        "isolation": {
            "verified": False,
            "gap": "SDK context isolation and effective tool enforcement are not runtime-attested",
        },
        "metrics": {"tokens": None, "cost": None, "coverage": None},
        "stages": {"comprehension": {"status": "not_run"}, "readiness": {"status": "not_run"}},
    }
    try:
        run = runtime()
        report["stages"]["comprehension"] = await stage(run, NAMES[0], {"snapshot": snap}, [])
        if report["stages"]["comprehension"]["status"] == "reported":
            from .project_registry import project_registry_snapshot

            registry = project_registry_snapshot()
            project = snap["project"] or registry["default_project"]
            entry = next((p for p in registry["projects"] if p["name"] == project), None)
            if not entry or not entry.get("repo") or not Path(entry["repo"]).is_dir():
                raise ValueError("missing live project repository binding/access")
            report["repo"] = str(Path(entry["repo"]).resolve())
            report["code_snapshot"] = "live checkout; not pinned or atomic; bounded partial reads"
            report["stages"]["readiness"] = await stage(
                run,
                NAMES[1],
                {
                    "snapshot": snap,
                    "comprehension": report["stages"]["comprehension"]["result"],
                    "repo": report["repo"],
                },
                repo_tools(report["repo"]),
            )
    except Exception as exc:
        report["gap"] = str(exc)[:1000]
    refresh(store, feature_id, report)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    # The ledger records pending verification, never claims its own write succeeded.
    report["persistence"] = "unverified"
    encoded = canonical(report)
    try:
        store.comment(feature_id, encoded)
        comments = store.feature_comments(feature_id, unknown_ok=False)
        report["persistence"] = "verified" if encoded in comments else "failed: run record absent on readback"
    except Exception as exc:
        report["persistence"] = f"failed: {str(exc)[:1000]}"
    # Covers edits/deletion during comment persistence as well as during either worker.
    refresh(store, feature_id, report)
    return report


def refresh(store, feature_id, report):
    try:
        current = store.get_feature(feature_id)
        report["freshness"] = (
            "current" if current and input_hash(snapshot(current)) == report["input_hash"] else "stale"
        )
    except Exception as exc:
        report["freshness"] = "unknown"
        report["freshness_gap"] = str(exc)[:1000]
