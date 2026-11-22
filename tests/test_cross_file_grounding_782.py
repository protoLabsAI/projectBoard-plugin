"""Cross-file findings need evidence even when the quote is outside the diff (#782)."""

from types import SimpleNamespace

import pytest

from project_board.loop._common import partition_by_grounding


DIFF = """diff --git a/src/a2a_impl/hitl_routing.py b/src/a2a_impl/hitl_routing.py
--- a/src/a2a_impl/hitl_routing.py
+++ b/src/a2a_impl/hitl_routing.py
@@ -1 +1 @@
-route(task)
+route(private_task)
"""


def finding(evidence, **overrides):
    fields = dict(file="src/a2a_impl/hitl_routing.py", category="cross-file", evidence=evidence)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_bd_cge6j_declared_no_quote_is_ungrounded():
    speculative = finding("No verbatim quote available. Scenario: hitl_routing.py route() sets message.task_id …")
    assert partition_by_grounding([speculative], DIFF) == ([], [speculative])


@pytest.mark.parametrize(
    "evidence",
    [
        "",
        " \n\t",
        None,
        "...",
        " \u2026 \n...",
        "NO VERBATIM QUOTE AVAILABLE. Scenario: routing fails",
        "  No Quote: routing fails",
        "Not Quoted: routing fails",
    ],
)
def test_cross_file_without_quote_is_ungrounded(evidence):
    speculative = finding(evidence)
    assert partition_by_grounding([speculative], DIFF) == ([], [speculative])


def test_cross_file_quote_from_untouched_file_stays_grounded():
    quote = "return parked_tasks[peer_task_id]"
    assert quote not in DIFF
    real = finding(quote, category=" Cross-File ")
    assert partition_by_grounding([real], DIFF) == ([real], [])


@pytest.mark.parametrize(
    "evidence",
    [
        # Shorter than the diff-matching floor, but still a real quote of the untouched file.
        "pass",
        "x = 1",
        "}",
        "+    pass",
        "...\nx = 1\n...",
        # Starts like a declared phrase but is a quoted line, not a declaration.
        "no_quote = True",
        "not quoted_names:",
    ],
)
def test_cross_file_short_or_lookalike_quote_stays_grounded(evidence):
    real = finding(evidence)
    assert evidence not in DIFF
    assert partition_by_grounding([real], DIFF) == ([real], [])


@pytest.mark.parametrize("evidence", ["", None, "No quote available. Scenario: routing fails"])
def test_absent_file_keeps_pass_through(evidence):
    elsewhere = finding(evidence, file="src/untouched.py")
    assert partition_by_grounding([elsewhere], DIFF) == ([elsewhere], [])


def test_missing_host_evidence_is_ungrounded_for_cross_file():
    speculative = finding("")
    del speculative.evidence
    assert partition_by_grounding([speculative], DIFF) == ([], [speculative])


def test_other_categories_and_partition_order_are_preserved():
    empty = finding("", category="correctness")
    no_quote = finding("No quote available")
    real = finding('message = "no quote"')
    bad_quote = finding("assert missing_from_diff(task)", category="correctness")
    assert partition_by_grounding([empty, no_quote, real, bad_quote], DIFF) == (
        [empty, real],
        [no_quote, bad_quote],
    )
