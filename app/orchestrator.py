import json
import logging
import operator
import os
import time
from datetime import datetime
from typing import Annotated, TypedDict

from langchain.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from app.engines import careops_world
from app import progress
from app.agents.critic_agent import CriticIssue, CriticVerdict, collect_retrieved_data, review
from app.agents.critic_checks import check_findings
from app.agents.investigator_agent import investigator_agent
from app.adapters.llm import describe_completion
from app.report import build_report, save_report
from app.trace import tracing

log = logging.getLogger("careops.orchestrator")

# 1 first investigation + 2 retries.
MAX_ATTEMPTS = 3
# Whole-run budget: no new attempt starts after this, and the run ends INCONCLUSIVE ("time budget").
RUN_TIMEOUT_SECONDS = float(os.environ.get("RUN_TIMEOUT_SECONDS", "600"))

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
        stop_reason: str                             # accepted | attempt limit | time budget, set by finalize
        deadline: float                              # time.monotonic() after which no new attempt starts


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
        progress.emit("attempt_started", attempt=attempt, max_attempts=MAX_ATTEMPTS,
                      retry_issues=[issue.model_dump() for issue in state["verdict"].issues] if state["attempt"] else [])
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
                progress.emit("reminder", attempt=attempt, completion=describe_completion(result["messages"][-1]))
                started = time.monotonic()
                result = investigator_agent.invoke(
                        {"messages": result["messages"] + [HumanMessage(content=FINDINGS_REMINDER)]})
                log.info("reminder answered in %.1fs; %s", time.monotonic() - started,
                         describe_completion(result["messages"][-1]))
        findings = result["messages"][-1].content
        retrieved = collect_retrieved_data(result["messages"])
        _log_findings(findings)
        parsed = _findings_list(findings)
        progress.emit("investigator_done", attempt=attempt, data_requests=len(retrieved),
                      completion=describe_completion(result["messages"][-1]),
                      findings=[{"order_id": f.get("order_id"), "finding_type": f.get("finding_type"),
                                 "confidence": f.get("confidence")} for f in parsed if isinstance(f, dict)]
                      if parsed is not None else None)
        if run_world:
                run_world.record_findings(findings)
        return {"findings": findings, "retrieved": retrieved, "attempt": attempt}


def critique(state: InvestigationState) -> dict:
        log.info("=== CRITIC reviews attempt %d", state["attempt"])
        run_world = careops_world.current()
        verdict, decided_by = None, "llm"
        if run_world:
                # Deterministic checks against the World Model register first. A failure here
                # is certain, so the slower LLM critic is skipped and the issues go straight back.
                code_issues = check_findings(state["findings"], run_world.world)
                progress.emit("code_checks", attempt=state["attempt"], passed=not code_issues, issues=code_issues)
                if code_issues:
                        log.info("code checks: %d issue(s); skipping LLM critic", len(code_issues))
                        for issue in code_issues:
                                log.info("  - [%s] %s: %s", issue["check"], issue["order_id"], issue["problem"])
                        verdict = CriticVerdict(verdict="RETRY", issues=[CriticIssue(**i) for i in code_issues])
                        decided_by = "code"
                else:
                        log.info("code checks: all passed against the register")
        if verdict is None:
                progress.emit("critic_started", attempt=state["attempt"])
                verdict = review(state["request"], state["retrieved"], state["findings"])
        progress.emit("verdict", attempt=state["attempt"], verdict=verdict.verdict, decided_by=decided_by,
                      issues=[issue.model_dump() for issue in verdict.issues])
        if run_world:
                run_world.record_verdict(verdict.verdict, [issue.model_dump() for issue in verdict.issues], decided_by)
        return {"verdict": verdict}


def finalize(state: InvestigationState) -> dict:
        if state["verdict"].verdict == "ACCEPT":
                status, reason = "VERIFIED", "accepted"
        elif state["attempt"] >= MAX_ATTEMPTS:
                status, reason = "INCONCLUSIVE", "attempt limit"
        else:
                status, reason = "INCONCLUSIVE", "time budget"
        log.info("=== FINAL STATUS: %s after %d attempt(s) (%s)", status, state["attempt"], reason)
        return {"status": status, "stop_reason": reason}


def route_after_critique(state: InvestigationState) -> str:
        if state["verdict"].verdict == "ACCEPT":
                log.info("route: critic accepted -> finalize")
                return "finalize"
        if state["attempt"] >= MAX_ATTEMPTS:
                log.warning("route: critic still says RETRY but %d/%d attempts used -> finalize as INCONCLUSIVE",
                            state["attempt"], MAX_ATTEMPTS)
                return "finalize"  # retry limit reached: report as INCONCLUSIVE
        if time.monotonic() > state["deadline"]:
                log.warning("route: critic says RETRY but the %.0fs run budget is used -> finalize as INCONCLUSIVE",
                            RUN_TIMEOUT_SECONDS)
                return "finalize"
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


DEFAULT_REQUEST = (
        "Investigate completed lab orders with missing invoices or unpaid "
        "replacement invoices. Check the available billing history, explain "
        "the evidence, and identify which cases require human review."
)


def new_run_id() -> str:
        return f"run-{datetime.now():%Y%m%d-%H%M%S}"


def sample_mode_enabled() -> bool:
        return os.environ.get("SAMPLE_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def run_investigation(request: str, run_id: str | None = None, sample_mode: bool | None = None) -> dict:
        """Run the full investigate -> critique -> retry loop and return the final state.

        Everything the run sees and decides is recorded in a World Model register
        (.world/<run_id>.json) and traced to logs/trace-<run_id>.jsonl. The final report is
        built from the register and saved to reports/<run_id>.json; the paths and the report
        are returned with the final state.

        sample_mode (default: SAMPLE_MODE env) runs offline on bundled synthetic rows with
        rule-based stand-ins for the LLM agents, so it needs no Zetaris, model or network.
        """
        run_id = run_id or new_run_id()
        sample_mode = sample_mode_enabled() if sample_mode is None else sample_mode
        started = time.monotonic()
        with tracing(run_id) as trace_file:
                log.info("REQUEST%s: %s", " (SAMPLE MODE)" if sample_mode else "", request)
                progress.emit("run_started", run_id=run_id, request=request, max_attempts=MAX_ATTEMPTS,
                              sample_mode=sample_mode)
                run_world = careops_world.RunWorld(run_id, request)
                with careops_world.activate(run_world):
                        if sample_mode:
                                from app.sample_mode import run_sample
                                final = run_sample(request, run_world)
                        else:
                                final = graph.invoke({
                                        "request": request,
                                        "findings": "",
                                        "retrieved": [],
                                        "verdict": None,
                                        "attempt": 0,
                                        "status": "",
                                        "stop_reason": "",
                                        "deadline": started + RUN_TIMEOUT_SECONDS,
                                })
                run_world.finish(final["status"], final["attempt"])
                report = build_report({**final, "sample_mode": sample_mode}, run_world)
                report_file = save_report(report)
                log.info("report saved: %s; trace: %s", report_file, trace_file)
                progress.emit("run_finished", run_id=run_id, status=final["status"], attempts=final["attempt"],
                              stop_reason=final.get("stop_reason"), seconds=round(time.monotonic() - started, 1))
        return {**final, "run_id": run_id, "world_file": str(run_world.file), "report": report,
                "report_file": str(report_file), "trace_file": str(trace_file),
                "execution_time_seconds": round(time.monotonic() - started, 1)}


if __name__ == "__main__":
        from app.cli import main
        raise SystemExit(main(["investigate"]))
