"""Per-run JSONL trace: logs/trace-<run_id>.jsonl, one JSON object per line.

Each progress event becomes a record saying which agent did what, for whom, with which
model, on which attempt, and how it went. This is what a reviewer reads to verify the
agents' work: handoffs, Zetaris queries, World Model (Meterless engine) activity, critique,
retries and the model behind every LLM call.

The raw progress events are kept too (logs/events-<run_id>.jsonl), so the web UI can show
the timeline and replay the agent flow of any finished run. Runs traced before that file
existed get their events rebuilt from the trace (load_events).
"""

from __future__ import annotations

import json
import re
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


def events_path(run_id: str) -> Path:
        return logs_dir() / f"events-{run_id}.jsonl"


@contextmanager
def tracing(run_id: str) -> Iterator[Path]:
        """Write every progress event of this run to the trace (and raw, to the events file) while the block runs."""
        logs_dir().mkdir(parents=True, exist_ok=True)
        path = trace_path(run_id)
        lock = threading.Lock()
        with path.open("a", encoding="utf-8") as out, events_path(run_id).open("a", encoding="utf-8") as raw:
                def write(event: dict) -> None:
                        with lock:
                                out.write(json.dumps(to_record(event, run_id), ensure_ascii=False, default=str) + "\n")
                                out.flush()
                                raw.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                                raw.flush()
                with progress.run_scope(run_id), progress.listen(write):
                        yield path


def _read_jsonl(path: Path) -> list[dict]:
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
                try:
                        out.append(json.loads(line))
                except ValueError:
                        continue  # a line cut short by a crash
        return out


def load_events(run_id: str) -> list[dict]:
        """The progress events of a finished run: the raw events file, else rebuilt from the trace."""
        if events_path(run_id).exists():
                return _read_jsonl(events_path(run_id))
        if trace_path(run_id).exists():
                return [e for e in (from_record(r) for r in _read_jsonl(trace_path(run_id))) if e]
        return []


_ISSUE = re.compile(r"\[([^\]]+)\] ([^:]+): (.*?)(?=; \[|$)")


def _parse_issues(text: str) -> list[dict]:
        return [{"check": c, "order_id": None if o == "None" else o, "problem": p}
                for c, o, p in _ISSUE.findall(text or "")]


def _number(pattern: str, text: str, cast=int):
        match = re.search(pattern, text or "")
        return cast(match.group(1)) if match else None


def from_record(record: dict) -> dict | None:
        """Best-effort inverse of to_record, for runs that have a trace but no events file.

        Summaries in the trace are shortened, so long SQL or issue lists may come back cut off.
        """
        agent, action = record.get("agent_name"), record.get("action")
        inp, out = record.get("input_summary") or "", record.get("output_summary") or ""
        attempt = (record.get("retry_count") or 0) + 1
        event = {"at": record.get("timestamp")}
        if action == "start_run":
                event.update(kind="run_started", request=inp)
        elif agent == "Orchestrator" and action in ("delegate_task", "request_refinement"):
                fix = inp.split("; fix: ", 1)
                event.update(kind="attempt_started", attempt=_number(r"attempt (\d+)", inp) or attempt,
                             max_attempts=_number(r"attempt \d+/(\d+)", inp),
                             retry_issues=_parse_issues(fix[1]) if len(fix) > 1 else [])
        elif agent == "Investigator" and action == "delegate_task":
                event.update(kind="data_request", request=inp)
        elif action == "query_zetaris" and record.get("status") == "error":
                event.update(kind="query_failed", sql=inp, error=out)
        elif action == "query_zetaris":
                event.update(kind="query", sql=inp, rows=_number(r"(\d+) row", out), total=_number(r"of (\d+)", out),
                             seconds=_number(r"in ([\d.]+)s", out, float), truncated="(truncated)" in out)
        elif agent == "World Model (Meterless engine)":
                event.update(kind="world_model", action=action, input=inp, output=out)
        elif action == "model_call":
                event.update(kind="llm_call", agent=agent, model=record.get("model"), completion=out)
        elif action == "produce_findings":
                findings = None if record.get("status") == "error" else [
                        {"order_id": o, "finding_type": t, "confidence": c}
                        for o, t, c in re.findall(r"([^,:\s]+): (\S+) \(([^)]*)\)", out)]
                event.update(kind="investigator_done", attempt=attempt, findings=findings,
                             data_requests=_number(r"(\d+) data request", inp))
        elif action == "recover":
                event.update(kind="reminder", attempt=attempt)
        elif action == "verify_evidence":
                passed = record.get("status") == "success"
                event.update(kind="code_checks", attempt=attempt, passed=passed, issues=[] if passed else _parse_issues(out))
        elif action == "review":
                event.update(kind="critic_started", attempt=attempt)
        elif action == "verdict":
                verdict, _, issues = out.partition(": ")
                event.update(kind="verdict", attempt=attempt, verdict=verdict,
                             decided_by="code" if agent == "Critic (code checks)" else "llm", issues=_parse_issues(issues))
        elif action == "finish_run":
                event.update(kind="run_finished", status=out.split(" after ")[0],
                             attempts=_number(r"after (\d+) attempt", out))
        else:
                return None
        return event
