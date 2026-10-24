"""Per-run JSONL trace: logs/trace-<run_id>.jsonl, one JSON object per line.

Each progress event becomes a record saying which agent did what, for whom, with which
model, on which attempt, and how it went. This is what a reviewer reads to verify the
agents' work: handoffs, Zetaris queries, World Model (Meterless engine) activity, critique,
retries and the model behind every LLM call.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app import progress
from app.logging_setup import short
from app.paths import logs_dir


def _issues(issues) -> str:
        return "; ".join(f"[{i.get('check')}] {i.get('order_id')}: {i.get('problem')}" for i in issues or [])


def to_record(event: dict, run_id: str) -> dict:
        """Map a progress event onto the trace format."""
        kind, attempt = event["kind"], event.get("attempt")
        record = {"timestamp": event["at"], "run_id": run_id, "agent_name": "Orchestrator", "action": kind,
                  "input_summary": "", "output_summary": "", "target_agent": None, "model": None,
                  "retry_count": max(attempt - 1, 0) if attempt else 0, "status": "success"}
        if kind == "run_started":
                record.update(action="start_run", input_summary=short(event.get("request"), 300), target_agent="Investigator")
        elif kind == "attempt_started":
                retry = event.get("retry_issues") or []
                record.update(action="delegate_task" if not retry else "request_refinement", target_agent="Investigator",
                              input_summary=f"attempt {attempt}/{event.get('max_attempts')}"
                              + (f"; fix: {short(_issues(retry), 400)}" if retry else ""))
        elif kind == "data_request":
                record.update(agent_name="Investigator", action="delegate_task", target_agent="Data Agent",
                              input_summary=short(event.get("request"), 300))
        elif kind == "query":
                record.update(agent_name="Data Agent", action="query_zetaris", target_agent="Zetaris USL",
                              input_summary=short(event.get("sql"), 400),
                              output_summary=f"{event.get('rows')} row(s) of {event.get('total')} in {event.get('seconds')}s"
                              + (" (truncated)" if event.get("truncated") else ""))
        elif kind == "query_failed":
                record.update(agent_name="Data Agent", action="query_zetaris", target_agent="Zetaris USL", status="error",
                              input_summary=short(event.get("sql"), 400), output_summary=short(event.get("error"), 300))
        elif kind == "world_model":
                record.update(agent_name="World Model (Meterless engine)", action=event.get("action", "record"),
                              input_summary=short(event.get("input"), 300), output_summary=short(event.get("output"), 300))
        elif kind == "llm_call":
                record.update(agent_name=event.get("agent", "LLM"), action="model_call", model=event.get("model"),
                              output_summary=event.get("completion", ""),
                              status="error" if event.get("finish") == "length" else "success")
        elif kind == "investigator_done":
                findings = event.get("findings")
                record.update(agent_name="Investigator", action="produce_findings", target_agent="Critic",
                              output_summary=(", ".join(f"{f['order_id']}: {f['finding_type']} ({f['confidence']})" for f in findings)
                                              if findings is not None else "no valid findings JSON"),
                              input_summary=f"{event.get('data_requests')} data request(s)",
                              status="success" if findings is not None else "error")
        elif kind == "reminder":
                record.update(action="recover", target_agent="Investigator", status="retry",
                              input_summary=f"empty reply ({event.get('completion')}); asked once more for the JSON")
        elif kind == "code_checks":
                record.update(agent_name="Critic (code checks vs World Model)", action="verify_evidence",
                              output_summary="all evidence verified" if event.get("passed") else short(_issues(event.get("issues")), 600),
                              target_agent=None if event.get("passed") else "Investigator",
                              status="success" if event.get("passed") else "rejected")
        elif kind == "critic_started":
                record.update(agent_name="Critic (LLM)", action="review", input_summary="findings + retrieved data")
        elif kind == "verdict":
                retry = event.get("verdict") == "RETRY"
                record.update(agent_name="Critic (code checks)" if event.get("decided_by") == "code" else "Critic (LLM)",
                              action="verdict", output_summary=f"{event.get('verdict')}" + (f": {short(_issues(event.get('issues')), 600)}" if retry else ""),
                              target_agent="Investigator" if retry else "Orchestrator", status="rejected" if retry else "success")
        elif kind == "run_finished":
                record.update(action="finish_run", output_summary=f"{event.get('status')} after {event.get('attempts')} attempt(s)",
                              status="success" if event.get("status") == "VERIFIED" else "partial")
        else:
                record["output_summary"] = short(json.dumps({k: v for k, v in event.items() if k not in ("kind", "at")}, default=str), 300)
        return record


def trace_path(run_id: str) -> Path:
        return logs_dir() / f"trace-{run_id}.jsonl"


@contextmanager
def tracing(run_id: str) -> Iterator[Path]:
        """Write every progress event of this run to logs/trace-<run_id>.jsonl while the block runs."""
        logs_dir().mkdir(parents=True, exist_ok=True)
        path = trace_path(run_id)
        lock = threading.Lock()
        with path.open("a", encoding="utf-8") as out:
                def write(event: dict) -> None:
                        with lock:
                                out.write(json.dumps(to_record(event, run_id), ensure_ascii=False, default=str) + "\n")
                                out.flush()
                with progress.run_scope(run_id), progress.listen(write):
                        yield path
