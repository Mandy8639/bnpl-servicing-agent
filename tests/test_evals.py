"""The eval harness: the golden set is frozen, well formed, and scored honestly."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from bnpl.governed import Case, Payment
from bnpl.mandate import Action
from bnpl.proposers import ProposerResponse, parse_proposal
from evals.report import next_report_stem, to_html, to_markdown, write_reports
from evals.run_evals import (
    GOLDEN_PATH,
    LOCK_PATH,
    GoldenSetChangedError,
    run_golden_set,
    verify_frozen,
)

GOLDEN = json.loads(GOLDEN_PATH.read_text())["cases"]
EXPECTED = {f"case-{c['id']}": c["expected_action"] for c in GOLDEN}


class Oracle:
    """Proposes exactly what the golden set expects, to check the harness, not the agent."""

    def propose(self, case: Case, payments: list[Payment]) -> ProposerResponse:
        action = EXPECTED[case.case_id]
        raw = {
            "action": action,
            "rationale": "oracle",
            "term_extension_months": 2 if action == Action.OFFER_PLAN else 0,
            "fee_waiver": False,
        }
        return ProposerResponse(parse_proposal(case.case_id, raw), raw, 0.0, "oracle")


class AlwaysNudge:
    """A reckless agent that nudges everything."""

    def propose(self, case: Case, payments: list[Payment]) -> ProposerResponse:
        raw: dict[str, Any] = {
            "action": "nudge",
            "rationale": "nudge everything",
            "term_extension_months": 0,
            "fee_waiver": False,
        }
        return ProposerResponse(parse_proposal(case.case_id, raw), raw, 0.0, "reckless")


# --- the golden set -----------------------------------------------------------------


def test_golden_set_is_frozen_and_unchanged() -> None:
    assert verify_frozen() == LOCK_PATH.read_text().strip()


def test_golden_set_has_the_specified_composition() -> None:
    assert len(GOLDEN) == 48
    assert Counter(c["category"] for c in GOLDEN) == {
        "nudge": 12,
        "offer_plan": 8,
        "escalate": 8,
        "ambiguous": 8,
        "no_action": 6,
        "control": 6,
    }


def test_every_golden_case_states_its_expectation() -> None:
    for case in GOLDEN:
        assert case["expected_action"] in {a.value for a in Action}, case["id"]
        assert isinstance(case["must_hold"], bool)
        assert case["rationale"].strip()


def test_must_hold_follows_the_mandate() -> None:
    for case in GOLDEN:
        risky = case["expected_action"] in {"escalate_collections", "hold_for_review"}
        assert case["must_hold"] == (risky or case["category"] == "control"), case["id"]


def test_control_cases_cover_all_six_scenarios() -> None:
    scenarios = {c["control"]["scenario"] for c in GOLDEN if c["control"]}
    assert scenarios == {
        "stale_data",
        "merchant_under_review",
        "auto_rate_breach",
        "offer_plan_rate_breach",
        "out_of_mandate_proposal",
        "repeat_blocks",
    }


def test_an_edited_golden_set_is_refused(tmp_path: Path) -> None:
    edited = tmp_path / "golden.json"
    edited.write_text(GOLDEN_PATH.read_text().replace('"nudge"', '"no_action"', 1))
    with pytest.raises(GoldenSetChangedError):
        verify_frozen(edited, LOCK_PATH)


# --- scoring ------------------------------------------------------------------------


def test_a_perfect_agent_scores_perfectly() -> None:
    m = run_golden_set(Oracle(), mode="test", model=None, source="oracle").metrics()
    assert m["action_accuracy"] == 1.0
    assert m["false_auto_count"] == 0
    assert m["hold_accuracy"] == 1.0
    assert m["pause_detection_rate"] == 1.0
    assert m["guardrail_block_rate"] == 1.0
    assert m["llm_cost_usd"] == 0.0
    assert m["gate_passed"]


def test_the_guardrail_keeps_a_reckless_agent_from_any_false_auto() -> None:
    m = run_golden_set(AlwaysNudge(), mode="test", model=None, source="reckless").metrics()
    assert m["action_accuracy"] < 0.5
    assert m["false_auto_count"] > 0
    assert not m["gate_passed"]


def test_injected_proposals_are_excluded_from_action_accuracy() -> None:
    run = run_golden_set(Oracle(), mode="test", model=None, source="oracle")
    injected = [r.id for r in run.results if r.injected]
    assert injected == ["g47", "g48"]
    assert run.metrics()["model_decided_cases"] == 46


# --- reports ------------------------------------------------------------------------


def test_reports_render_the_same_numbers_as_the_json(tmp_path: Path) -> None:
    report = run_golden_set(Oracle(), mode="test", model=None, source="oracle").to_dict()
    json_path, md_path, html_path = write_reports(report, tmp_path)
    saved = json.loads(json_path.read_text())
    assert saved["metrics"]["action_accuracy"] == 1.0
    for text in (md_path.read_text(), html_path.read_text()):
        assert "100.0%" in text and "46/46" in text
    assert to_markdown(saved) == md_path.read_text()
    assert to_html(saved) == html_path.read_text()


def test_a_rerun_never_overwrites_a_committed_report(tmp_path: Path) -> None:
    (tmp_path / "2026-11-16-stub.json").write_text("{}")
    (tmp_path / "2026-11-16-stub-2.json").write_text("{}")
    assert next_report_stem(tmp_path, "2026-11-16", "stub") == "2026-11-16-stub-3"


def test_committed_cassette_replays_for_zero_dollars_and_passes_the_gate() -> None:
    from bnpl.proposers import Cassette, RecordedProposer
    from evals.run_evals import cassette_path

    cassette = Cassette.load(cassette_path("claude-haiku-5-5"))
    assert cassette.source.startswith("recorded live from claude-haiku-5-5")
    m = run_golden_set(
        RecordedProposer(cassette), mode="stub", model="claude-haiku-5-5", source="stub"
    ).metrics()
    assert m["llm_cost_usd"] == 0.0
    assert m["false_auto_count"] == 0


def test_readme_numbers_match_the_committed_reports() -> None:
    from evals.readme import main as readme

    assert readme(["--check"]) == 0, "run `python -m evals.readme` to regenerate README blocks"


# --- the held-out set -----------------------------------------------------------------


def test_heldout_set_is_frozen_and_separate_from_the_golden_set() -> None:
    from evals.run_evals import HELDOUT_LOCK_PATH, HELDOUT_PATH

    assert verify_frozen(HELDOUT_PATH, HELDOUT_LOCK_PATH) == HELDOUT_LOCK_PATH.read_text().strip()
    heldout = json.loads(HELDOUT_PATH.read_text())["cases"]
    assert len(heldout) == 20
    assert Counter(c["category"] for c in heldout) == {
        "nudge": 5,
        "offer_plan": 4,
        "escalate": 4,
        "ambiguous": 4,
        "no_action": 3,
    }
    golden_merchants = {c["merchant"]["name"] for c in GOLDEN}
    assert not golden_merchants & {c["merchant"]["name"] for c in heldout}
    golden_texts = {m["text"] for c in GOLDEN for m in c["case"]["comms_history"]}
    inbound = [
        m["text"]
        for c in heldout
        for m in c["case"]["comms_history"]
        if m["direction"] == "inbound"
    ]
    assert not golden_texts & set(inbound)


def test_heldout_recording_replays_for_zero_dollars_and_passes_the_gate() -> None:
    from bnpl.proposers import Cassette, RecordedProposer
    from evals.run_evals import HELDOUT_LOCK_PATH, HELDOUT_PATH, cassette_path

    cassette = Cassette.load(cassette_path("claude-haiku-5-5", "heldout"))
    m = run_golden_set(
        RecordedProposer(cassette),
        mode="stub",
        model="claude-haiku-5-5",
        source="stub",
        golden_path=HELDOUT_PATH,
        lock_path=HELDOUT_LOCK_PATH,
        suite="heldout",
    ).metrics()
    assert m["llm_cost_usd"] == 0.0
    assert m["false_auto_count"] == 0


def test_heldout_reports_never_feed_the_headline_numbers() -> None:
    from evals.readme import numbers_block

    assert "heldout" not in numbers_block()
