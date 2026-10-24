import json
import logging
import operator
import time
from datetime import datetime
from typing import Annotated, TypedDict

from langchain.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from careops import careops_world
from careops.critic_agent import CriticIssue, CriticVerdict, collect_retrieved_data, review
from careops.critic_checks import check_findings
from careops.investigator_agent import investigator_agent
from careops.llm import describe_completion
from careops.logging_setup import setup_logging

log = logging.getLogger("careops.orchestrator")

# 1 first investigation + 2 retries.
MAX_ATTEMPTS = 3

FINDINGS_REMINDER = (
        "Your last reply contained no findings. Using only the data you already retrieved in this "
        "conversation, return ONLY the findings JSON object now, in the exact format from your instructions. "
        "Do not request more data."
)


class InvestigationState(TypedDict):
        request: str                                 # the user's investigation request
        findings: str                                # latest findings JSON from the investigator
        retrieved: Annotated[list, operator.add]     # data from ALL attempts; each node's list is appended
        verdict: CriticVerdict | None                # latest critic verdict
        attempt: int                                 # how many times the investigator has run
        status: str                                  # VERIFIED or INCONCLUSIVE, set by finalize


def _findings_list(findings: str) -> list | None:
        try:
                parsed = json.loads(findings)["findings"]
        except (ValueError, KeyError, TypeError):
                return None
        return parsed if isinstance(parsed, list) else None


def _log_findings(findings: str) -> None:
        parsed = _findings_list(findings)
        if parsed is None:
                log.warning("findings are not valid JSON with a 'findings' list: %s", findings[:200])
                return
        for f in parsed:
                log.info("  finding %s: %s (confidence %s, invoices %s, %d evidence item(s))",
                         f.get("order_id"), f.get("finding_type"), f.get("confidence"),
                         f.get("invoice_ids"), len(f.get("evidence") or []))


def investigate(state: InvestigationState) -> dict:
        attempt = state["attempt"] + 1
        run_world = careops_world.current()
        if run_world:
                run_world.start_attempt(attempt)
        if state["attempt"] == 0:
                log.info("=== ATTEMPT %d/%d: investigator starts", attempt, MAX_ATTEMPTS)
                message = state["request"]
        else:
                log.info("=== ATTEMPT %d/%d: investigator retries with %d critic issue(s)",
                         attempt, MAX_ATTEMPTS, len(state["verdict"].issues))
                # Retry: original request + the data already retrieved (from the World Model register)
                # + previous findings + the critic's issues. With the data in hand the investigator can
                # fix the issues without fetching everything again.
                issues = "\n".join(
                        f"- [{issue.check}] {issue.order_id or 'all'}: {issue.problem}"
                        for issue in state["verdict"].issues
                )
                known = run_world.retrieved_context() if run_world else None
                if known:
                        log.info("retry includes the data already retrieved (%d chars from the register)", len(known))
                message = state["request"]
                if known:
                        message += (
                                "\n\nData already retrieved from Zetaris in earlier attempts (exact values; "
                                "columns not listed were never retrieved):\n" + known
                        )
                message += (
                        "\n\nYour previous findings:\n" + state["findings"]
                        + "\n\nThe critic rejected them for these reasons:\n" + issues
                        + "\n\nFix these issues and return the complete corrected findings. "
                )
                message += (
                        "Use the data above; call ask_data_agent only if a fix needs a column or record "
                        "that is not listed there." if known else "Retrieve more data if needed."
                )
        started = time.monotonic()
        result = investigator_agent.invoke({"messages": [HumanMessage(content=message)]})
        log.info("investigator done in %.1fs: %d data request(s); %s", time.monotonic() - started,
                 len(collect_retrieved_data(result["messages"])), describe_completion(result["messages"][-1]))
        if _findings_list(result["messages"][-1].content) is None:
                # Usually the reply ran out of tokens and came back empty. The investigator already has its
                # data in this conversation, so ask once more for just the JSON instead of a full retry.
                log.warning("no findings JSON in the reply; asking the investigator once more for the JSON only")
                started = time.monotonic()
                result = investigator_agent.invoke(
                        {"messages": result["messages"] + [HumanMessage(content=FINDINGS_REMINDER)]})
                log.info("reminder answered in %.1fs; %s", time.monotonic() - started,
                         describe_completion(result["messages"][-1]))
        findings = result["messages"][-1].content
        retrieved = collect_retrieved_data(result["messages"])
        _log_findings(findings)
        if run_world:
                run_world.record_findings(findings)
        return {"findings": findings, "retrieved": retrieved, "attempt": attempt}


def critique(state: InvestigationState) -> dict:
        log.info("=== CRITIC reviews attempt %d", state["attempt"])
        run_world = careops_world.current()
        verdict = None
        if run_world:
                # Deterministic checks against the World Model register first. A failure here
                # is certain, so the slower LLM critic is skipped and the issues go straight back.
                code_issues = check_findings(state["findings"], run_world.world)
                if code_issues:
                        log.info("code checks: %d issue(s); skipping LLM critic", len(code_issues))
                        for issue in code_issues:
                                log.info("  - [%s] %s: %s", issue["check"], issue["order_id"], issue["problem"])
                        verdict = CriticVerdict(verdict="RETRY", issues=[CriticIssue(**i) for i in code_issues])
                else:
                        log.info("code checks: all passed against the register")
        if verdict is None:
                verdict = review(state["request"], state["retrieved"], state["findings"])
        if run_world:
                run_world.record_verdict(verdict.verdict, [issue.model_dump() for issue in verdict.issues])
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
        """Run the full investigate -> critique -> retry loop and return the final state.

        Everything the run sees and decides is recorded in a World Model register,
        saved to .world/<run_id>.json; its path is returned as final["world_file"].
        """
        run_id = f"run-{datetime.now():%Y%m%d-%H%M%S}"
        log.info("REQUEST (%s): %s", run_id, request)
        run_world = careops_world.RunWorld(run_id, request)
        with careops_world.activate(run_world):
                final = graph.invoke({
                        "request": request,
                        "findings": "",
                        "retrieved": [],
                        "verdict": None,
                        "attempt": 0,
                        "status": "",
                })
        run_world.finish(final["status"], final["attempt"])
        return {**final, "world_file": str(run_world.file)}


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
        print(f"World Model register: {final['world_file']}")
