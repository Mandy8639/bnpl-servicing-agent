"""The MCP server, exercised as a real stdio subprocess with a real MCP client."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from bnpl.ledger.seed import seed_ledger

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fresh_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    seed_ledger(path)  # stamped now: the server checks freshness against the real clock
    return path


def call(ledger: Path, *calls: tuple[str, dict[str, Any]]) -> list[Any]:
    """Start the server over stdio, make the calls in order, return each result."""

    async def run() -> list[Any]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "bnpl.mcp_server"],
            cwd=str(ROOT),
            env={**os.environ, "BNPL_DB_PATH": str(ledger)},
        )
        async with Client(params) as client:
            results: list[Any] = [await client.list_tools()]
            for name, args in calls:
                results.append(await client.call_tool(name, args))
            return results

    return asyncio.run(run())


def test_server_lists_exactly_the_four_governed_tools(fresh_ledger: Path) -> None:
    [tools] = call(fresh_ledger)
    names = {t.name: t for t in tools.tools}
    assert set(names) == {"get_case", "list_exceptions", "propose_action", "submit_for_review"}
    assert names["get_case"].annotations.read_only_hint is True
    assert names["list_exceptions"].annotations.read_only_hint is True


def test_get_case_returns_governed_facts(fresh_ledger: Path) -> None:
    _, result = call(fresh_ledger, ("get_case", {"case_id": "case-0093"}))
    body = result.structured_content
    assert not result.is_error
    assert body["case"]["case_id"] == "case-0093"
    assert body["payments"] and body["status"] == "open"


def test_unknown_case_is_a_clear_tool_error(fresh_ledger: Path) -> None:
    _, result = call(fresh_ledger, ("get_case", {"case_id": "case-9999"}))
    assert result.is_error and "no case case-9999" in result.content[0].text


def test_a_low_risk_proposal_runs_under_the_mandate(fresh_ledger: Path) -> None:
    _, result = call(
        fresh_ledger,
        ("propose_action", {"case_id": "case-0093", "action": "nudge", "rationale": "First miss."}),
    )
    assert result.structured_content["verdict"] == "ALLOW"
    assert result.structured_content["executed_action"] == "nudge"


def test_a_high_risk_proposal_is_held_for_a_human(fresh_ledger: Path) -> None:
    _, result = call(
        fresh_ledger,
        (
            "propose_action",
            {"case_id": "case-0004", "action": "escalate_collections", "rationale": "Charged off."},
        ),
    )
    body = result.structured_content
    assert (body["verdict"], body["executed_action"]) == ("HOLD", None)
    assert body["escalation_owner"] == "servicing lead" and body["decide_by"]


def test_an_out_of_mandate_proposal_is_blocked(fresh_ledger: Path) -> None:
    _, result = call(
        fresh_ledger,
        ("propose_action", {"case_id": "case-0001", "action": "waive_fees", "rationale": "x"}),
    )
    assert result.structured_content["reason_code"] == "OUT_OF_MANDATE_ACTION"


def test_submit_for_review_queues_the_case_and_inventories_it(fresh_ledger: Path) -> None:
    _, submitted, exceptions = call(
        fresh_ledger,
        ("submit_for_review", {"case_id": "case-0022", "reason": "Evidence conflicts."}),
        ("list_exceptions", {"kind": "HOLD"}),
    )
    assert submitted.structured_content["reason_code"] == "AGENT_ABSTAINED"
    rows = exceptions.structured_content["result"]
    assert rows[0]["case_id"] == "case-0022"
    assert rows[0]["detail"]["escalation_owner"] == "servicing lead"


def test_a_worked_case_cannot_be_reproposed(fresh_ledger: Path) -> None:
    proposal = {"case_id": "case-0093", "action": "nudge", "rationale": "First miss."}
    _, first, second = call(
        fresh_ledger, ("propose_action", proposal), ("propose_action", proposal)
    )
    assert not first.is_error
    assert second.is_error and "already been worked" in second.content[0].text
