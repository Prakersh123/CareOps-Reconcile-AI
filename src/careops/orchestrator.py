import json
import logging
import operator
import time
from typing import Annotated, TypedDict

from langchain.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from careops.critic_agent import CriticVerdict, collect_retrieved_data, review
from careops.investigator_agent import investigator_agent
from careops.logging_setup import setup_logging

log = logging.getLogger("careops.orchestrator")

# 1 first investigation + 2 retries.
MAX_ATTEMPTS = 3


class InvestigationState(TypedDict):
        request: str                                 # the user's investigation request
        findings: str                                # latest findings JSON from the investigator
        retrieved: Annotated[list, operator.add]     # data from ALL attempts; each node's list is appended
        verdict: CriticVerdict | None                # latest critic verdict
        attempt: int                                 # how many times the investigator has run
        status: str                                  # VERIFIED or INCONCLUSIVE, set by finalize


def _log_findings(findings: str) -> None:
        try:
                parsed = json.loads(findings)["findings"]
        except (ValueError, KeyError, TypeError):
                log.warning("findings are not valid JSON with a 'findings' list: %s", findings[:200])
                return
        for f in parsed:
                log.info("  finding %s: %s (confidence %s, invoices %s, %d evidence item(s))",
                         f.get("order_id"), f.get("finding_type"), f.get("confidence"),
                         f.get("invoice_ids"), len(f.get("evidence") or []))


def investigate(state: InvestigationState) -> dict:
        attempt = state["attempt"] + 1
        if state["attempt"] == 0:
                log.info("=== ATTEMPT %d/%d: investigator starts", attempt, MAX_ATTEMPTS)
                message = state["request"]
        else:
                log.info("=== ATTEMPT %d/%d: investigator retries with %d critic issue(s)",
                         attempt, MAX_ATTEMPTS, len(state["verdict"].issues))
                # Retry: original request + previous findings + the critic's issues.
                issues = "\n".join(
                        f"- [{issue.check}] {issue.order_id or 'all'}: {issue.problem}"
                        for issue in state["verdict"].issues
                )
                message = (
                        state["request"]
                        + "\n\nYour previous findings:\n" + state["findings"]
                        + "\n\nThe critic rejected them for these reasons:\n" + issues
                        + "\n\nFix these issues and return the complete corrected findings. "
                        + "Retrieve more data if needed."
                )
        started = time.monotonic()
        result = investigator_agent.invoke({"messages": [HumanMessage(content=message)]})
        findings = result["messages"][-1].content
        retrieved = collect_retrieved_data(result["messages"])
        log.info("investigator done in %.1fs: %d data request(s)", time.monotonic() - started, len(retrieved))
        _log_findings(findings)
        return {"findings": findings, "retrieved": retrieved, "attempt": attempt}


def critique(state: InvestigationState) -> dict:
        log.info("=== CRITIC reviews attempt %d", state["attempt"])
        verdict = review(state["request"], state["retrieved"], state["findings"])
        return {"verdict": verdict}


def finalize(state: InvestigationState) -> dict:
        status = "VERIFIED" if state["verdict"].verdict == "ACCEPT" else "INCONCLUSIVE"
        log.info("=== FINAL STATUS: %s after %d attempt(s)", status, state["attempt"])
        return {"status": status}


def route_after_critique(state: InvestigationState) -> str:
        if state["verdict"].verdict == "ACCEPT":
                log.info("route: critic accepted -> finalize")
                return "finalize"
        if state["attempt"] >= MAX_ATTEMPTS:
                log.warning("route: critic still says RETRY but %d/%d attempts used -> finalize as INCONCLUSIVE",
                            state["attempt"], MAX_ATTEMPTS)
                return "finalize"  # retry limit reached: report as INCONCLUSIVE
        log.info("route: critic says RETRY -> back to investigator")
        return "investigate"


builder = StateGraph(InvestigationState)
builder.add_node("investigate", investigate)
builder.add_node("critique", critique)
builder.add_node("finalize", finalize)

builder.add_edge(START, "investigate")
builder.add_edge("investigate", "critique")
builder.add_conditional_edges("critique", route_after_critique, ["investigate", "finalize"])
builder.add_edge("finalize", END)

graph = builder.compile()


def run_investigation(request: str) -> dict:
        """Run the full investigate -> critique -> retry loop and return the final state."""
        log.info("REQUEST: %s", request)
        return graph.invoke({
                "request": request,
                "findings": "",
                "retrieved": [],
                "verdict": None,
                "attempt": 0,
                "status": "",
        })


if __name__ == "__main__":
        log_file = setup_logging()
        request = (
                "Investigate completed lab orders with missing invoices or unpaid "
                "replacement invoices. Check the available billing history, explain "
                "the evidence, and identify which cases require human review."
        )
        try:
                final = run_investigation(request)
        except Exception:
                log.exception("run failed")
                raise SystemExit(f"\nRun failed. Full log: {log_file}")

        print("\nFINAL FINDINGS:\n" + final["findings"])
        print(f"\nFull log: {log_file}")
