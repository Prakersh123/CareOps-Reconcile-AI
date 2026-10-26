"""One investigation, JSON in -> JSON out. Shared by POST /run and `python run.py`.

Success:  {run_id, status: "success", output: {...}, agents, trace_id, log_file, execution_time_seconds}
Failure:  {run_id, status: "error", error: {type, message, recoverable}, trace_id, log_file, execution_time_seconds}
A run that finishes but is not verified is still "success"; output.verification_status says
VERIFIED or INCONCLUSIVE and output.stop_reason says why.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx

from app.paths import home, world_file
from app.adapters.zetaris_client import ZetarisError

log = logging.getLogger("careops.service")

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

AGENTS = [
        {"name": "Orchestrator", "role": "LangGraph state machine: runs attempts, routes critic feedback back to the "
                                         "investigator, stops at 3 attempts or the run's time budget"},
        {"name": "Investigator", "role": "Plans the data it needs, asks the Data Agent in plain language, writes "
                                         "evidence-backed findings under the reconciliation business rules"},
        {"name": "Data Agent", "role": "Turns requests into read-only SQL over the Zetaris USL table and returns the "
                                       "raw rows unchanged"},
        {"name": "World Model (Meterless engine)", "role": "Per-run register of every Zetaris row, derived link, finding "
                                                           "and verdict, with provenance and an audit log"},
        {"name": "Critic (code checks)", "role": "Verifies every evidence item against the register; on failure sends "
                                                 "concrete fixes back and skips the LLM critic"},
        {"name": "Critic (LLM)", "role": "Judges logic, candidate vs confirmed, replacement vs duplicate and coverage; "
                                         "ACCEPT or RETRY"},
]

# Failures a retry later could fix (a service down or slow), as opposed to bugs or bad input.
_RECOVERABLE = (ZetarisError, httpx.HTTPError, TimeoutError, ConnectionError)


def available_run_id(run_id: str) -> str:
        """The run_id itself, or run_id-2, run_id-3... if it was used before (e.g. an example run twice).

        A used id is never reused: its register and report would mix with the new run's.
        """
        if not RUN_ID.match(run_id):
                raise ValueError("run_id must be 1-64 letters, digits, '-' or '_'")
        candidate, n = run_id, 1
        while world_file(candidate).exists():
                n += 1
                candidate = f"{run_id}-{n}"
        if not RUN_ID.match(candidate):
                raise ValueError(f"run_id {run_id!r} is taken and too long to suffix; pick a new one")
        return candidate


def summarize(report: dict) -> str:
        """One readable paragraph: verdict, counts, and the orders that need a human."""
        if report["status"] == "REFUSED":
                return report["message"]
        summary = report["summary"]
        flagged = [f"{f['order_id']} ({f['finding_type'].replace('_', ' ').lower()})"
                   for f in report["findings"] if f.get("requires_human_review")]
        text = (f"{report['status']} after {report['attempts']} attempt(s): {summary['findings']} order(s) reviewed, "
                f"{summary['evidence_verified']}/{summary['evidence_items']} evidence items verified against "
                f"{'the bundled sample data' if report.get('sample_mode') else 'Zetaris'}. ")
        text += (f"Needs human review: {', '.join(flagged)}." if flagged else "No order needs human review.")
        if report.get("sample_mode"):
                text += " (Sample mode: bundled data, rule-based stand-in agents, code checks only.)"
        return text


def _relative(path: str) -> str:
        try:
                return str(Path(path).resolve().relative_to(home().resolve()))
        except ValueError:
                return path


def run_once(query: str, run_id: str | None = None, sample_mode: bool | None = None,
             on_event=None) -> dict:
        """Run one investigation and return the response document; never raises for run failures."""
        from app import progress
        from app.orchestrator import DEFAULT_REQUEST, new_run_id, run_investigation

        run_id = run_id or new_run_id()
        query = (query or "").strip() or DEFAULT_REQUEST
        started = time.monotonic()
        try:
                run_id = available_run_id(run_id)
        except ValueError as exc:
                return {"run_id": run_id, "status": "error",
                        "error": {"type": type(exc).__name__, "message": str(exc), "recoverable": False},
                        "trace_id": run_id, "log_file": None, "execution_time_seconds": 0.0}
        trace_file = f"logs/trace-{run_id}.jsonl"
        try:
                if on_event:
                        with progress.listen(on_event):
                                final = run_investigation(query, run_id=run_id, sample_mode=sample_mode)
                else:
                        final = run_investigation(query, run_id=run_id, sample_mode=sample_mode)
        except Exception as exc:  # report it as data, not a crash
                log.exception("run %s failed", run_id)
                return {
                        "run_id": run_id,
                        "status": "error",
                        "error": {"type": type(exc).__name__, "message": str(exc)[:500],
                                  "recoverable": isinstance(exc, _RECOVERABLE)},
                        "trace_id": run_id,
                        "log_file": trace_file,
                        "execution_time_seconds": round(time.monotonic() - started, 1),
                }
        report = final["report"]
        return {
                "run_id": run_id,
                "status": "success",
                "output": {
                        "summary": summarize(report),
                        "verification_status": report["status"],
                        "stop_reason": report.get("stop_reason"),
                        "sample_mode": report.get("sample_mode", False),
                        "findings": report["findings"],
                        "recommendations": [
                                {"order_id": f.get("order_id"), "finding_type": f.get("finding_type"),
                                 "action": f.get("recommended_action"), "requires_human_review": f.get("requires_human_review")}
                                for f in report["findings"]],
                        "critic_history": report["attempt_history"],
                        "artifacts": [_relative(final["report_file"]), _relative(final["world_file"]),
                                      _relative(final["trace_file"])],
                },
                "agents": AGENTS,
                "trace_id": run_id,
                "log_file": trace_file,
                "execution_time_seconds": final["execution_time_seconds"],
        }
