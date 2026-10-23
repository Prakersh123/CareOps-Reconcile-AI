import json
import logging
import time
from typing import Literal

from langchain.agents import create_agent
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

from careops.llm import default_model
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

log = logging.getLogger("careops.critic")


CRITIC_PROMPT = """
You are the Critic in a hospital billing reconciliation system.
You review the Investigator's findings before they reach a human reviewer.
You do not investigate yourself and you have no tools. You judge ONLY against
the retrieved data you are given; anything not in that data is unsupported.

You receive:
1. The user's original request.
2. The retrieved data: every data request the Investigator made, with the raw rows returned.
3. The Investigator's findings (JSON).

Before judging, verify every evidence item mechanically, one by one:
a. The invoice_id must be a real invoice ID that appears in an invoice_id column of the
   retrieved rows. An order ID (e.g. "L900") used as an invoice_id is wrong.
   For a missing invoice, evidence must cite the order's row with invoice_id null
   (invoice_id: null), not invent an ID.
b. The field must be a column that was actually returned by one of the queries.
   If the query did not select that column, the evidence value is unsupported,
   even if it is null.
c. The value must exactly equal that row's value for that field (same case, same text).
d. Every ID in invoice_ids must appear in the retrieved rows for that order.
Any evidence item that fails a, b, c or d is a "support" problem: name the item and say
what the Investigator must retrieve or correct.

Check each finding for:
1. Support: the explanation and finding_type follow from the retrieved rows.
   Any claim not backed by a retrieved row is a problem, including the evidence
   verification failures above.
2. Candidate vs confirmed: a missing invoice must be treated as a candidate needing
   verification, never as a confirmed billing error.
3. Replacement vs duplicate: if history (event_type / notes) shows a cancellation and a
   replacement, the order must NOT be called duplicate billing. If a replacement is claimed,
   the history must support it.
4. Unpaid vs incorrect: an issued but unpaid invoice needs payment follow-up; it must not
   be called an incorrect invoice.
5. History checked: for orders with a cancelled, unpaid, or more than one invoice, the
   Investigator must have retrieved event_type and notes.
6. Coverage: every order in the retrieved data that falls in scope of the request, and
   every completed order the Investigator examined, must have a finding. Orders that were
   checked and found correctly billed should be reported as RECONCILED, not left out.
7. Uncertainty and confidence: they must match the evidence. High confidence with thin or
   no evidence is a problem; "None" as uncertainty for a missing invoice is a problem.
   A MISSING_INVOICE_CANDIDATE is unverified by definition (the invoice may exist in a
   system we cannot see), so its confidence must not be "high".
8. Safety: recommended actions must be review or follow-up only, never changing invoices,
   issuing payments, or editing billing records.

Verdict:
- ACCEPT only if there are no material problems.
- RETRY if any check fails. List each problem with its order_id and a concrete, actionable
  instruction for the Investigator (what to retrieve or correct).
- Do not report style or wording preferences; only problems that would mislead a reviewer.
"""


class CriticIssue(BaseModel):
        order_id: str | None = Field(description="Order the problem concerns; null if it applies to the whole result.")
        check: str = Field(description="Which check failed, e.g. 'support', 'coverage', 'history_checked'.")
        problem: str = Field(description="What is wrong and what the Investigator must do to fix it.")


class CriticVerdict(BaseModel):
        verdict: Literal["ACCEPT", "RETRY"]
        issues: list[CriticIssue] = Field(default_factory=list)


critic_agent = create_agent(
        model=default_model(),
        system_prompt=CRITIC_PROMPT,
        tools=[],
        response_format=CriticVerdict
)


def collect_retrieved_data(messages: list) -> list[dict]:
        """Pair each ask_data_agent request with the raw data it returned, from an investigator run."""
        requests = {}
        for message in messages:
                if isinstance(message, AIMessage):
                        for call in message.tool_calls:
                                requests[call["id"]] = call["args"].get("request")
        return [
                {"request": requests.get(message.tool_call_id), "response": message.content}
                for message in messages
                if isinstance(message, ToolMessage) and message.name == "ask_data_agent"
        ]


def review(user_request: str, retrieved_data: list[dict], findings: str) -> CriticVerdict:
        """Ask the critic LLM to judge the investigator's findings against the retrieved data."""
        content = (
                "USER REQUEST:\n" + user_request
                + "\n\nRETRIEVED DATA:\n" + json.dumps(retrieved_data, indent=2)
                + "\n\nINVESTIGATOR FINDINGS:\n" + findings
        )
        log.info("reviewing findings against %d data request(s) (%d chars of input)",
                 len(retrieved_data), len(content))
        started = time.monotonic()
        try:
                result = critic_agent.invoke({"messages": [HumanMessage(content=content)]})
        except Exception as exc:
                log.error("critic LLM call failed after %.1fs: %s: %s", time.monotonic() - started,
                          type(exc).__name__, exc)
                raise
        verdict = result["structured_response"]
        log.info("verdict %s with %d issue(s) in %.1fs", verdict.verdict, len(verdict.issues),
                 time.monotonic() - started)
        for issue in verdict.issues:
                log.info("  - [%s] %s: %s", issue.check, issue.order_id, issue.problem)
        return verdict


if __name__ == "__main__":
        from careops.investigator_agent import investigator_agent

        request = (
                "Investigate completed lab orders with missing invoices or unpaid "
                "replacement invoices. Check the available billing history, explain "
                "the evidence, and identify which cases require human review."
        )
        investigation = investigator_agent.invoke({"messages": [HumanMessage(content=request)]})
        findings = investigation["messages"][-1].content
        retrieved = collect_retrieved_data(investigation["messages"])

        print("INVESTIGATOR FINDINGS:\n" + findings)
        print(f"\nDATA REQUESTS GIVEN TO CRITIC: {len(retrieved)}")

        verdict = review(request, retrieved, findings)
        print("\nCRITIC VERDICT:\n" + verdict.model_dump_json(indent=2))
