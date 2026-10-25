"""Report, trace, sample mode, the /run contract and the web UI endpoints, without a live LLM or Zetaris.

API tests drive the real pipeline in SAMPLE_MODE (bundled rows, rule-based stand-ins, real World
Model register and code checks), with every artefact under a temporary CAREOPS_HOME.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import api, data_check, report, service, trace

REAL_DATA_CHECK = data_check.check  # before the autouse fixture replaces it
from app.engines.careops_world import RunWorld
from app.adapters.zetaris_client import ZetarisError
from app.sample_mode import classify, load_rows

ROWS = [
        {"order_id": "L102", "invoice_id": None, "notes": None},
        {"order_id": "L103", "invoice_id": "I104", "payment_status": "UNPAID", "event_type": "REPLACEMENT_INVOICE_CREATED",
         "notes": "Replacement for I102"},
        {"order_id": "L103", "invoice_id": "I102", "payment_status": "UNPAID", "event_type": "INVOICE_CANCELLED",
         "notes": "Cancelled as duplicate"},
]
FINDINGS = {"findings": [
        {"order_id": "L102", "finding_type": "MISSING_INVOICE_CANDIDATE", "invoice_ids": [], "confidence": "medium",
         "evidence": [{"invoice_id": None, "field": "invoice_id", "value": None}], "requires_human_review": True},
        {"order_id": "L103", "finding_type": "UNPAID_REPLACEMENT_INVOICE", "invoice_ids": ["I102", "I104"], "confidence": "high",
         "evidence": [{"invoice_id": "I104", "field": "payment_status", "value": "UNPAID"},
                      {"invoice_id": "I104", "field": "payment_status", "value": "PAID"}],
         "requires_human_review": True},
]}


@pytest.fixture(autouse=True)
def careops_home(tmp_path, monkeypatch):
        monkeypatch.setenv("CAREOPS_HOME", str(tmp_path))
        monkeypatch.setattr(api, "_runs", {})
        return tmp_path


READY = {"ready": True, "problem": None, "detail": "ok"}


@pytest.fixture(autouse=True)
def data_ready(monkeypatch):
        """API tests do not depend on a live Zetaris: the data check says ready unless a test says otherwise."""
        monkeypatch.setattr(api.data_check, "check", lambda: READY)


@pytest.fixture
def finished_run(tmp_path):
        rw = RunWorld("run-20261024-120000", "Investigate", directory=tmp_path)
        rw.start_attempt(1)
        rw.record_query("SELECT ...", ROWS, 3, False)
        rw.record_findings(json.dumps(FINDINGS))
        rw.record_verdict("RETRY", [{"order_id": "L102", "check": "code:confidence", "problem": "too high"}], decided_by="code")
        rw.start_attempt(2)
        rw.record_verdict("ACCEPT", [], decided_by="llm")
        final = {"request": "Investigate", "status": "VERIFIED", "attempt": 2, "stop_reason": "accepted",
                 "findings": json.dumps(FINDINGS)}
        return final, rw


# ------------------------------------------------------------------ report

def test_report_marks_each_evidence_item_against_zetaris(finished_run):
        built = report.build_report(*finished_run)
        l103 = next(f for f in built["findings"] if f["order_id"] == "L103")
        assert [e["verified"] for e in l103["evidence"]] == [True, False]
        assert l103["evidence"][1]["check"] == "evidence_value"
        assert built["summary"] == {"findings": 2, "needs_human_review": 2,
                                    "by_type": {"MISSING_INVOICE_CANDIDATE": 1, "UNPAID_REPLACEMENT_INVOICE": 1},
                                    "evidence_items": 3, "evidence_verified": 2}
        assert built["stop_reason"] == "accepted" and built["sample_mode"] is False


def test_report_attempt_history_says_who_decided(finished_run):
        history = report.build_report(*finished_run)["attempt_history"]
        assert [(a["number"], a["verdict"], a["decided_by"]) for a in history] == [(1, "RETRY", "code"), (2, "ACCEPT", "llm")]


def test_report_register_summary(finished_run):
        reg = report.build_report(*finished_run)["register"]
        assert reg["orders_retrieved"] == 2 and reg["invoices_retrieved"] == 2
        assert "I104 replaces I102" in reg["relationships"]


def test_unparsable_findings_are_kept_raw(finished_run):
        final, rw = finished_run
        built = report.build_report({**final, "findings": "oops"}, rw)
        assert built["findings"] == [] and built["findings_unparsed"] == "oops"


def test_save_and_list_reports(finished_run):
        built = report.build_report(*finished_run)
        report.save_report(built)
        assert report.load_report(built["run_id"])["status"] == "VERIFIED"
        assert [r["run_id"] for r in report.list_reports()] == [built["run_id"]]
        assert report.load_report("../../etc/passwd") is None


def test_summary_names_the_orders_needing_review(finished_run):
        text = service.summarize(report.build_report(*finished_run))
        assert text.startswith("VERIFIED after 2 attempt(s)")
        assert "L102 (missing invoice candidate)" in text and "L103 (unpaid replacement invoice)" in text


# ------------------------------------------------------------- sample mode

def test_sample_rules_classify_every_bundled_order():
        by_order = {}
        for row in load_rows():
                by_order.setdefault(row["order_id"], []).append(row)
        types = {o: classify(o, rows)["finding_type"] for o, rows in by_order.items()}
        assert types == {"L101": "RECONCILED", "L102": "MISSING_INVOICE_CANDIDATE",
                         "L103": "UNPAID_REPLACEMENT_INVOICE", "L104": "RECONCILED"}


@pytest.mark.parametrize("rows, expected", [
        ([{"order_id": "L9", "invoice_id": "I1", "invoice_status": "ISSUED", "payment_status": "UNPAID"}], "UNPAID_INVOICE"),
        ([{"order_id": "L9", "invoice_id": "I1", "invoice_status": "CANCELLED", "payment_status": "UNPAID",
           "event_type": "INVOICE_CANCELLED", "notes": "wrong test"}], "CANCELLED_WITHOUT_REPLACEMENT"),
        ([{"order_id": "L9", "invoice_id": "I1", "invoice_status": "ISSUED", "payment_status": "PAID"},
          {"order_id": "L9", "invoice_id": "I2", "invoice_status": "ISSUED", "payment_status": "UNPAID"}], "POSSIBLE_DUPLICATE_BILLING"),
        ([{"order_id": "L9", "invoice_id": "I1", "invoice_status": "CANCELLED", "payment_status": "UNPAID"},
          {"order_id": "L9", "invoice_id": "I2", "invoice_status": "ISSUED", "payment_status": "PAID",
           "event_type": "REPLACEMENT_INVOICE_CREATED", "notes": "Replacement for I1"}], "RECONCILED"),
])
def test_sample_rules_cover_the_other_finding_types(rows, expected):
        assert classify("L9", rows)["finding_type"] == expected


def test_sample_run_writes_a_trace_with_agents_handoffs_and_engine(careops_home):
        result = service.run_once("Check order L103", run_id="demo-trace", sample_mode=True)
        assert result["status"] == "success"
        records = [json.loads(line) for line in (careops_home / "logs" / "trace-demo-trace.jsonl").read_text().splitlines()]
        assert {r["run_id"] for r in records} == {"demo-trace"}
        actions = [(r["agent_name"], r["action"]) for r in records]
        assert ("Investigator", "delegate_task") in actions
        assert ("Data Agent", "query_zetaris") in actions
        assert ("World Model (Meterless engine)", "record_zetaris_rows") in actions
        assert ("Critic (code checks vs World Model)", "verify_evidence") in actions
        assert actions[-1] == ("Orchestrator", "finish_run")
        assert set(records[0]) >= {"timestamp", "agent_name", "action", "input_summary", "output_summary",
                                   "target_agent", "model", "retry_count", "status"}



def test_raw_events_are_kept_and_old_traces_rebuild_them(careops_home):
        service.run_once("Check order L103", run_id="demo-events", sample_mode=True)
        events = trace.load_events("demo-events")
        kinds = [e["kind"] for e in events]
        assert kinds[0] == "run_started" and kinds[-1] == "run_finished" and "code_checks" in kinds
        # A run traced before events files existed: the same story comes back from the trace alone.
        (careops_home / "logs" / "events-demo-events.jsonl").unlink()
        rebuilt = trace.load_events("demo-events")
        assert [e["kind"] for e in rebuilt] == kinds
        query = next(e for e in rebuilt if e["kind"] == "query")
        original = next(e for e in events if e["kind"] == "query")
        assert (query["rows"], query["total"]) == (original["rows"], original["total"])
        assert rebuilt[-1]["status"] == "VERIFIED" and rebuilt[-1]["attempts"] == 1


def test_trace_issues_and_findings_rebuild_into_events():
        issues = [{"check": "code:confidence", "order_id": "L102", "problem": "too high"},
                  {"check": "llm:coverage", "order_id": None, "problem": "L101 not covered"}]
        events = [
                {"kind": "investigator_done", "at": "t", "attempt": 1, "data_requests": 2,
                 "findings": [{"order_id": "L102", "finding_type": "MISSING_INVOICE_CANDIDATE", "confidence": "high"}]},
                {"kind": "code_checks", "at": "t", "attempt": 1, "passed": False, "issues": issues},
                {"kind": "verdict", "at": "t", "attempt": 2, "verdict": "RETRY", "decided_by": "llm", "issues": issues},
                {"kind": "attempt_started", "at": "t", "attempt": 2, "max_attempts": 3, "retry_issues": issues},
        ]
        rebuilt = [trace.from_record(trace.to_record(e, "r")) for e in events]
        assert rebuilt[0]["findings"] == events[0]["findings"] and rebuilt[0]["data_requests"] == 2
        assert rebuilt[1]["issues"] == issues and rebuilt[1]["passed"] is False
        assert (rebuilt[2]["verdict"], rebuilt[2]["decided_by"], rebuilt[2]["issues"]) == ("RETRY", "llm", issues)
        assert (rebuilt[3]["attempt"], rebuilt[3]["max_attempts"], rebuilt[3]["retry_issues"]) == (2, 3, issues)

# -------------------------------------------------------------- POST /run

@pytest.fixture
def client():
        return TestClient(api.app)


def test_run_returns_the_documented_contract(client, careops_home):
        response = client.post("/run", json={"run_id": "demo-001", "input": {"query": "Check order L103"},
                                             "options": {"sample_mode": True}})
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == "demo-001" and body["status"] == "success" and body["trace_id"] == "demo-001"
        assert body["log_file"] == "logs/trace-demo-001.jsonl"
        assert body["output"]["verification_status"] == "VERIFIED" and body["output"]["sample_mode"] is True
        assert [r["order_id"] for r in body["output"]["recommendations"]] == ["L103"]
        assert body["output"]["artifacts"] == ["reports/demo-001.json", ".world/demo-001.json", "logs/trace-demo-001.jsonl"]
        assert {a["name"] for a in body["agents"]} >= {"Investigator", "Data Agent", "Critic (code checks)"}
        assert isinstance(body["execution_time_seconds"], float)
        assert (careops_home / "reports" / "demo-001.json").exists()


def test_reused_run_id_gets_a_suffix_instead_of_mixing_runs(client):
        payload = {"run_id": "demo-dup", "input": {"query": "Check order L101"}, "options": {"sample_mode": True}}
        assert client.post("/run", json=payload).json()["run_id"] == "demo-dup"
        second = client.post("/run", json=payload).json()
        assert second["status"] == "success" and second["run_id"] == "demo-dup-2"
        assert second["log_file"] == "logs/trace-demo-dup-2.jsonl"


def test_unsafe_run_id_is_a_structured_400(client):
        body = client.post("/run", json={"run_id": "../../etc", "input": {"query": "x"}}).json()
        assert body["status"] == "error" and body["error"]["type"] == "ValueError"


def test_failed_run_is_a_structured_error(client, monkeypatch):
        import app.orchestrator as orchestrator

        def broken(*args, **kwargs):
                raise TimeoutError("Zetaris did not respond")
        monkeypatch.setattr(orchestrator, "run_investigation", broken)
        response = client.post("/run", json={"run_id": "demo-err", "input": {"query": "x"}})
        assert response.status_code == 502
        error = response.json()["error"]
        assert error == {"type": "TimeoutError", "message": "Zetaris did not respond", "recoverable": True}


def test_empty_query_uses_the_default(client):
        import app.orchestrator as orchestrator
        body = client.post("/run", json={"run_id": "demo-default", "options": {"sample_mode": True}}).json()
        assert json.loads((report.reports_dir() / "demo-default.json").read_text())["request"] == orchestrator.DEFAULT_REQUEST
        assert body["output"]["verification_status"] == "VERIFIED"


def test_health(client):
        body = client.get("/health").json()
        assert body["status"] == "ok" and body["model"] and body["running"] is None


# --------------------------------------------------------- web UI endpoints

def wait_finished(client, run_id):
        for _ in range(100):
                body = client.get(f"/api/runs/{run_id}").json()
                if body["state"] != "running":
                        return body
                time.sleep(0.05)
        raise AssertionError("run did not finish")


def test_ui_run_streams_events_then_serves_report_and_register(client):
        started = client.post("/api/runs", json={"request": "Check order L103", "sample_mode": True}).json()
        body = wait_finished(client, started["run_id"])
        assert body["state"] == "finished" and body["report"]["status"] == "VERIFIED"
        kinds = [e["kind"] for e in body["events"]]
        assert kinds[0] == "run_started" and kinds[-1] == "run_finished" and "code_checks" in kinds
        later = client.get(f"/api/runs/{started['run_id']}", params={"since": len(kinds) - 1}).json()
        assert [e["kind"] for e in later["events"]] == ["run_finished"]
        register = client.get(f"/api/runs/{started['run_id']}/register").json()
        assert {e["id"] for e in register["entities"]} >= {"order:zetaris:L103", "invoice:zetaris:I104"}
        assert client.get("/api/runs").json()[0]["run_id"] == started["run_id"]



def test_a_finished_run_opened_later_still_has_its_timeline(client):
        started = client.post("/api/runs", json={"request": "Check order L103", "sample_mode": True}).json()
        live = wait_finished(client, started["run_id"])
        api._runs.clear()  # as after a server restart: only the files on disk remain
        later = client.get(f"/api/runs/{started['run_id']}").json()
        assert later["state"] == "finished" and later["report"]["status"] == "VERIFIED"
        assert later["events"] == live["events"] and later["next"] == len(live["events"])

def test_only_one_run_at_a_time(client):
        api._runs["run-busy"] = api.Run(run_id="run-busy", request="x")
        assert client.post("/api/runs", json={}).status_code == 409
        assert client.post("/run", json={"input": {"query": "x"}}).json()["error"]["type"] == "Busy"


def test_unknown_or_unsafe_run_ids_are_404(client):
        assert client.get("/api/runs/run-20990101-000000").status_code == 404
        assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404
        assert client.get("/api/runs/not.a.run/register").status_code == 404


# ------------------------------------------------------------- data check

def test_real_run_is_refused_quickly_when_the_data_is_not_set_up(client, monkeypatch):
        missing = {"ready": False, "problem": "data", "detail": "Zetaris is reachable but the CareOps data is not set up: run ..."}
        monkeypatch.setattr(api.data_check, "check", lambda: missing)
        body = client.post("/run", json={"input": {"query": "x"}, "options": {"sample_mode": False}})
        assert body.status_code == 503
        assert body.json()["error"]["type"] == "DataNotReady" and "not set up" in body.json()["error"]["message"]
        assert client.post("/api/runs", json={"sample_mode": False}).status_code == 503
        assert client.get("/api/config").json()["data"] == missing


def test_sample_mode_runs_even_without_zetaris_data(client, monkeypatch):
        monkeypatch.setattr(api.data_check, "check", lambda: {"ready": False, "problem": "zetaris", "detail": "down"})
        body = client.post("/run", json={"input": {"query": "Check order L103"}, "options": {"sample_mode": True}})
        assert body.status_code == 200 and body.json()["output"]["sample_mode"] is True


class FakeZetaris:
        def __init__(self, rows=None, error=None):
                self.rows, self.error = rows, error

        def select(self, sql):
                if self.error:
                        raise ZetarisError(self.error)
                return type("Result", (), {"rows": self.rows})()


@pytest.mark.parametrize("fake, ready, problem", [
        (FakeZetaris(rows=[{"n": "5"}]), True, None),
        (FakeZetaris(rows=[{"n": "0"}]), False, "data"),
        (FakeZetaris(error="Zetaris returned HTTP 400: [TABLE_OR_VIEW_NOT_FOUND] The table or view ... cannot be found"), False, "data"),
        (FakeZetaris(error="Zetaris returned HTTP 400: Invalid namespace : List(careops)"), False, "data"),
        (FakeZetaris(error="Cannot reach Zetaris at http://localhost:3000: ConnectError"), False, "zetaris"),
        (FakeZetaris(error="Zetaris login failed (HTTP 404: Not Allowed); check ..."), False, "zetaris"),
])
def test_data_check_tells_missing_data_from_an_unreachable_zetaris(monkeypatch, fake, ready, problem):
        monkeypatch.setattr(data_check, "_zetaris", lambda: fake)
        status = REAL_DATA_CHECK()
        assert (status["ready"], status["problem"]) == (ready, problem)
        if problem == "data":
                assert "setup_zetaris.py" in status["detail"]
