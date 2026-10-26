"""HTTP API: a synchronous run endpoint for scripts and judges, plus the endpoints behind the web UI.

    POST /run                      {"run_id"?, "input": {"query"}, "options": {"sample_mode"}} -> JSON result
    GET  /health                   liveness, model in use, sample mode default
    POST /api/runs                 {"request", "sample_mode"?} -> start a run in the background (web UI)
    GET  /api/runs                 runs, newest first
    GET  /api/runs/{id}?since=N    status, progress events from index N, and the report when finished
    GET  /api/runs/{id}/register   the run's World Model register: entities, links, facts, audit trail
    GET  /api/config               default request, examples, model in use

One investigation runs at a time. The built React app (ui/dist) is served at /.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import data_check, service
from app.paths import PROJECT_ROOT, home, world_file
from app.logging_setup import setup_logging
from app.report import list_reports, load_report
from app.trace import load_events
from app.write_guard import write_request
from app.engines.world_model import WorldModel

log = logging.getLogger("careops.api")

UI_DIST = PROJECT_ROOT / "ui" / "dist"
# Shows the read-only guard: refused before any agent runs.
READ_ONLY_DEMO = "Delete the duplicate invoice I106 and mark I108 as paid"
EXAMPLES = [
        "Investigate completed lab orders with missing invoices or unpaid replacement invoices. Check the available "
        "billing history, explain the evidence, and identify which cases require human review.",
        "Check order L103: are its invoices duplicate billing or a valid replacement?",
        "Which completed lab orders have no invoice at all?",
        "Are all paid invoices correctly issued and linked to a completed order?",
        "Investigate all completed lab orders. Classify each one, explain the evidence, and identify which cases "
        "need human review.",
        "Check order L105: it has two invoices. Is this duplicate billing or a valid replacement?",
        "Check order L106: its invoice was cancelled. Was a replacement issued?",
        "Check order L107: is its invoice paid?",
        "Check order L108: it is completed. Was it billed?",
        "Check orders L105 and L107. Show the exact payment status of each invoice and explain what needs follow-up.",
        READ_ONLY_DEMO,
]


@dataclass
class Run:
        run_id: str
        request: str
        sample_mode: bool = False
        status: str = "running"  # running | finished | failed
        started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
        events: list = field(default_factory=list)
        report: dict | None = None
        error: str | None = None


_runs: dict[str, Run] = {}
_lock = threading.Lock()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
        if not logging.getLogger("careops").handlers:  # `careops serve` / run.py already set it up
                setup_logging()
        if not _sample_default(None):
                # In the background, so a slow or absent Zetaris never delays startup; the result goes to the log.
                threading.Thread(target=data_check.log_status, daemon=True, name="data-check").start()
        yield


app = FastAPI(title="CareOps Reconcile AI", lifespan=_lifespan)


def _claim(run_id: str, request: str, sample_mode: bool) -> Run:
        """Register a new run, or raise 409 if one is in progress (one investigation at a time)."""
        with _lock:
                busy = next((r for r in _runs.values() if r.status == "running"), None)
                if busy:
                        raise HTTPException(409, f"Run {busy.run_id} is still in progress; one run at a time.")
                run = Run(run_id=run_id, request=request, sample_mode=sample_mode)
                _runs[run_id] = run
                return run


def _data_status(sample_mode: bool, request: str = "") -> dict:
        """Readiness of the Zetaris data for this kind of run; sample mode and refused write requests never need it."""
        if sample_mode:
                return {"ready": True, "problem": None, "detail": "sample mode: bundled data, Zetaris not used"}
        if write_request(request):
                return {"ready": True, "problem": None, "detail": "write request: refused before any data is read"}
        return data_check.check()


def _sample_default(value: bool | None) -> bool:
        from app.orchestrator import sample_mode_enabled
        return sample_mode_enabled() if value is None else value


# ------------------------------------------------------------------ /run

class RunInput(BaseModel):
        query: str | None = None


class RunOptions(BaseModel):
        sample_mode: bool | None = None


class RunBody(BaseModel):
        run_id: str | None = None
        input: RunInput = Field(default_factory=RunInput)
        options: RunOptions = Field(default_factory=RunOptions)


@app.post("/run")
def run(body: RunBody) -> JSONResponse:
        """Run one investigation and wait for it. Errors come back as structured JSON, never a bare 500."""
        from app.orchestrator import DEFAULT_REQUEST, new_run_id
        run_id = body.run_id or new_run_id()
        query = (body.input.query or "").strip() or DEFAULT_REQUEST
        sample_mode = _sample_default(body.options.sample_mode)
        data = _data_status(sample_mode, query)
        if not data["ready"]:  # fail in a second instead of after minutes of model calls
                return JSONResponse(status_code=503, content={
                        "run_id": run_id, "status": "error",
                        "error": {"type": "DataNotReady" if data["problem"] == "data" else "ZetarisUnavailable",
                                  "message": data["detail"], "recoverable": True},
                        "trace_id": run_id, "execution_time_seconds": 0.0})
        try:
                run_id = service.available_run_id(run_id)  # a reused id gets a -2, -3... suffix
                run_record = _claim(run_id, query, sample_mode)
        except (ValueError, HTTPException) as exc:
                busy = isinstance(exc, HTTPException)
                return JSONResponse(status_code=409 if busy else 400, content={
                        "run_id": run_id, "status": "error",
                        "error": {"type": "Busy" if busy else type(exc).__name__,
                                  "message": exc.detail if busy else str(exc), "recoverable": busy},
                        "trace_id": run_id, "execution_time_seconds": 0.0})
        result = service.run_once(query, run_id=run_id, sample_mode=sample_mode, on_event=run_record.events.append)
        if result["status"] == "success":
                run_record.report = load_report(run_id)
                run_record.status = "finished"
        else:
                run_record.error = f"{result['error']['type']}: {result['error']['message']}"
                run_record.status = "failed"
        return JSONResponse(status_code=200 if result["status"] == "success" else 502, content=result)


@app.get("/health")
def health() -> dict:
        from app.adapters.llm import resolve_model_spec
        spec, reason = resolve_model_spec()
        return {"status": "ok", "model": spec, "model_reason": reason, "sample_mode_default": _sample_default(None),
                "data": _data_status(_sample_default(None)),
                "running": next((r.run_id for r in _runs.values() if r.status == "running"), None)}


# ------------------------------------------------------- web UI endpoints

class StartBody(BaseModel):
        request: str | None = None
        sample_mode: bool | None = None


def _execute(run_record: Run) -> None:
        result = service.run_once(run_record.request, run_id=run_record.run_id, sample_mode=run_record.sample_mode,
                                  on_event=run_record.events.append)
        if result["status"] == "success":
                run_record.report = load_report(run_record.run_id)
                run_record.status = "finished"
        else:
                run_record.error = f"{result['error']['type']}: {result['error']['message']}"
                run_record.status = "failed"


@app.post("/api/runs", status_code=202)
def start_run(body: StartBody) -> dict:
        from app.orchestrator import DEFAULT_REQUEST, new_run_id
        request = (body.request or "").strip() or DEFAULT_REQUEST
        sample_mode = _sample_default(body.sample_mode)
        data = _data_status(sample_mode, request)
        if not data["ready"]:
                raise HTTPException(503, data["detail"])
        run_record = _claim(new_run_id(), request, sample_mode)
        threading.Thread(target=_execute, args=(run_record,), daemon=True, name=run_record.run_id).start()
        return {"run_id": run_record.run_id, "request": request, "sample_mode": run_record.sample_mode}


@app.get("/api/runs")
def runs() -> list[dict]:
        listed = {r["run_id"]: {**r, "state": "finished"} for r in list_reports()}
        for run_record in _runs.values():
                if run_record.status != "finished" or run_record.run_id not in listed:
                        listed[run_record.run_id] = {
                                "run_id": run_record.run_id, "request": run_record.request, "state": run_record.status,
                                "status": run_record.report and run_record.report["status"],
                                "generated_at": run_record.started_at, "sample_mode": run_record.sample_mode}
        return sorted(listed.values(), key=lambda r: r.get("generated_at") or "", reverse=True)


def _check_id(run_id: str) -> None:
        if not service.RUN_ID.match(run_id):
                raise HTTPException(404, "Unknown run")


@app.get("/api/runs/{run_id}")
def run_status(run_id: str, since: int = 0) -> dict:
        _check_id(run_id)
        run_record = _runs.get(run_id)
        if run_record:
                return {"run_id": run_record.run_id, "request": run_record.request, "state": run_record.status,
                        "started_at": run_record.started_at, "sample_mode": run_record.sample_mode,
                        "events": run_record.events[since:], "next": len(run_record.events),
                        "report": run_record.report, "error": run_record.error}
        report = load_report(run_id)
        if not report:
                raise HTTPException(404, "Unknown run")
        events = load_events(run_id)  # saved with the run, so its timeline and agent flow can be replayed
        return {"run_id": run_id, "request": report["request"], "state": "finished", "started_at": report["generated_at"],
                "sample_mode": report.get("sample_mode", False), "events": events[since:], "next": len(events),
                "report": report, "error": None}


@app.get("/api/runs/{run_id}/register")
def register(run_id: str) -> dict:
        _check_id(run_id)
        run_record = _runs.get(run_id)
        if run_record and run_record.status == "running":
                raise HTTPException(409, "The register is still being written; open it when the run finishes.")
        if not world_file(run_id).exists():
                raise HTTPException(404, "No register for this run")
        world = WorldModel(namespace=run_id, storage="local", directory=home())
        graph = world.view("graph")
        return {
                "entities": [{"id": e["id"], "type": e["type"], "attrs": {k: v for k, v in e["attrs"].items() if k != "finding"},
                              "source": e["source"]} for e in graph.query(type="order") + graph.query(type="invoice")],
                "relationships": [e["payload"] for e in world.events() if e["kind"] == "relate"],
                "facts": [f for e in graph.query(type="order") for f in world.view("facts").about(e["id"])],
                "contexts": [e["payload"] for e in world.events() if e["kind"] == "upsertContext"],
                "audit": world.audit(),
        }


@app.get("/api/config")
def config() -> dict:
        from app.adapters.llm import resolve_model_spec
        from app.orchestrator import DEFAULT_REQUEST, MAX_ATTEMPTS
        return {"default_request": DEFAULT_REQUEST, "examples": EXAMPLES, "max_attempts": MAX_ATTEMPTS,
                "model": resolve_model_spec()[0], "sample_mode_default": _sample_default(None),
                "data": _data_status(False)}  # always the real pipeline's status, for the UI banner


if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
