"""Operator-facing settings copy stays short; detailed guidance belongs in docs."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())["settings"]
BANNED_LABEL_TERMS = ("br", "dep_gate", "coder_solve", "coder.solve", "best-of-k", "max_mode", "delegate")


def _plain_label(label):
    # Match words, not substrings such as "br" in "branch"; include spelling variants
    # and internal-name suffixes such as max_mode_n and coder_solve_test_cmd.
    normalized = re.sub(r"[_.-]+", " ", label.casefold())
    return not any(
        re.search(r"\b" + re.escape(re.sub(r"[_.-]+", " ", term)) + r"s?\b", normalized) for term in BANNED_LABEL_TERMS
    )


def _single_sentence(description):
    # Allow dots inside domain names, but no sentence-ending punctuation followed
    # by whitespace before the final full stop, and no line breaks.
    return (
        bool(re.fullmatch(r"[^!?\n\r]+\.", description))
        and not re.search(r"\.(?:\s|\.|$)", description[:-1])
        and description == description.strip()
    )


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda setting: setting["key"])
def test_settings_copy(setting):
    description = setting["description"]
    assert len(description) <= 160, "Move longer explanations to docs/console-settings.md"
    assert _single_sentence(description), "Help must be one sentence"
    assert setting["label"].strip() and _plain_label(setting["label"]), "Use operator words in labels"


@pytest.mark.parametrize("description", ["First. Second.", "First! Second.", "Why?", "No ending", "Line\nbreak.", ""])
def test_rejects_non_sentence_help(description):
    assert not _single_sentence(description)


@pytest.mark.parametrize(
    "term", BANNED_LABEL_TERMS + ("Max-Mode candidates", "Coder delegates", "coder_solve_test_cmd")
)
def test_rejects_internal_labels(term):
    assert not _plain_label(term)


def test_allows_operator_words():
    assert _plain_label("Update base branches")
    assert _single_sentence("Allow two builds at once; zero removes the limit.")
    assert _single_sentence("Download the board tool from github.com when it is missing.")


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda setting: setting["key"])
def test_settings_have_matching_documentation(setting):
    docs = (ROOT / "docs/console-settings.md").read_text()
    assert f"### {setting['label']} (`{setting['key']}`)" in docs
    assert f"| {setting['label']} | {setting['description']} |" in docs
