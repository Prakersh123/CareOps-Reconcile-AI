"""The read-only guard: requests to change data are refused before any agent, model or Zetaris call."""

import json

import pytest
from fastapi.testclient import TestClient

from app import api, trace
from app.write_guard import write_request


@pytest.mark.parametrize("request_text", [
        "Delete invoice I102",
        "Please mark I104 as paid",
        "Can you cancel invoice I106?",
        "Find duplicate invoices and delete them",
        "DELETE FROM billing_records WHERE invoice_id = 'I102'",
        "UPDATE billing_records SET payment_status = 'PAID'",
        "drop table billing_records",
        "Create an invoice for L102",
        "Refund the patient for L105",
        "Investigate L105. Then remove the duplicate invoice.",
])
def test_requests_to_change_data_are_caught(request_text):
        assert write_request(request_text)


@pytest.mark.parametrize("request_text", [
        "Which completed lab orders have no invoice at all?",
        "Check order L103: are its invoices duplicate billing or a valid replacement?",
        "Which invoices were cancelled, and was a replacement issued?",
        "Should invoice I106 be cancelled?",
        "Check order L105 and recommend what the billing team should fix",
] + [json.load(open(f"input_examples/{name}"))["input"]["query"]
     for name in ("example_1.json", "example_2.json", "example_3.json", "example_4_sample_mode.json")])
def test_questions_still_run(request_text):
        assert write_request(request_text) is None


@pytest.fixture(autouse=True)
def careops_home(tmp_path, monkeypatch):
        monkeypatch.setenv("CAREOPS_HOME", str(tmp_path))
        monkeypatch.setattr(api, "_runs", {})


def test_delete_request_is_refused_even_without_zetaris(monkeypatch):
        # Zetaris is down: a real investigation would get 503, a write request still gets its refusal.
        monkeypatch.setattr(api.data_check, "check",
                            lambda: {"ready": False, "problem": "zetaris", "detail": "Cannot reach Zetaris"})
        response = TestClient(api.app).post("/run", json={"run_id": "refuse-1", "input": {"query": "Delete invoice I102"},
                                                          "options": {"sample_mode": False}})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["output"]["verification_status"] == "REFUSED"
        assert "read-only" in body["output"]["summary"] and "Nothing was changed" in body["output"]["summary"]
        assert body["output"]["findings"] == []
        kinds = [e["kind"] for e in trace.load_events("refuse-1")]
        assert kinds == ["run_started", "write_refused", "run_finished"]  # no agent, model or query
        records = [json.loads(line) for line in trace.trace_path("refuse-1").read_text().splitlines()]
        assert ("refuse_write", "refused") in [(r["action"], r["status"]) for r in records]
        # rebuilt from the trace alone, the refusal reads the same
        trace.events_path("refuse-1").unlink()
        assert [e["kind"] for e in trace.load_events("refuse-1")] == kinds


def test_sidebar_examples_run_except_the_read_only_demo():
        refused = [example for example in api.EXAMPLES if write_request(example)]
        assert refused == [api.READ_ONLY_DEMO]
