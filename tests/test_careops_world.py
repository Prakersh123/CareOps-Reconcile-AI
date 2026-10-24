"""CareOps register wiring: Zetaris rows, findings and verdicts recorded in the World Model."""

import json

import pytest

from careops import careops_world
from careops.careops_world import RunWorld, invoice_entity_id, order_entity_id

# The five rows of the CAREOPS_RECONCILIATION USL table (SELECT *).
ALL_ROWS = [
        {"order_id": "L101", "patient_id": "P101", "test_code": "CBC", "order_date": "2026-10-20", "lab_status": "COMPLETED",
         "invoice_id": "I101", "invoice_status": "ISSUED", "payment_status": "PAID", "event_type": None, "event_date": None, "notes": None},
        {"order_id": "L102", "patient_id": "P102", "test_code": "LFT", "order_date": "2026-10-20", "lab_status": "COMPLETED",
         "invoice_id": None, "invoice_status": None, "payment_status": None, "event_type": None, "event_date": None, "notes": None},
        {"order_id": "L103", "patient_id": "P103", "test_code": "CBC", "order_date": "2026-10-21", "lab_status": "COMPLETED",
         "invoice_id": "I102", "invoice_status": "CANCELLED", "payment_status": "UNPAID", "event_type": "INVOICE_CANCELLED",
         "event_date": "2026-10-21", "notes": "Cancelled as duplicate"},
        {"order_id": "L103", "patient_id": "P103", "test_code": "CBC", "order_date": "2026-10-21", "lab_status": "COMPLETED",
         "invoice_id": "I104", "invoice_status": "ISSUED", "payment_status": "UNPAID", "event_type": "REPLACEMENT_INVOICE_CREATED",
         "event_date": "2026-10-21", "notes": "Replacement for I102"},
        {"order_id": "L104", "patient_id": "P104", "test_code": "KFT", "order_date": "2026-10-21", "lab_status": "COMPLETED",
         "invoice_id": "I103", "invoice_status": "ISSUED", "payment_status": "PAID", "event_type": None, "event_date": None, "notes": None},
]


@pytest.fixture
def run_world(tmp_path):
        rw = RunWorld("run-test", "Investigate completed lab orders", directory=tmp_path)
        rw.start_attempt(1)
        return rw


def graph(rw):
        return rw.world.view("graph")


def test_rows_become_orders_and_invoices_linked_by_bills(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        assert {e["id"] for e in graph(run_world).query(type="order")} == {order_entity_id(o) for o in ("L101", "L102", "L103", "L104")}
        assert {e["id"] for e in graph(run_world).query(type="invoice")} == {invoice_entity_id(i) for i in ("I101", "I102", "I103", "I104")}
        billed = graph(run_world).incoming(order_entity_id("L103"), via="bills")
        assert [e["id"] for e in billed] == [invoice_entity_id("I102"), invoice_entity_id("I104")]


def test_invoice_holds_invoice_columns_and_order_holds_order_columns(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        i104 = graph(run_world).entity(invoice_entity_id("I104"))["attrs"]
        assert i104["payment_status"] == "UNPAID" and i104["notes"] == "Replacement for I102"
        assert "lab_status" not in i104
        l103 = graph(run_world).entity(order_entity_id("L103"))["attrs"]
        assert l103["lab_status"] == "COMPLETED" and "payment_status" not in l103


def test_missing_invoice_nulls_are_recorded_on_the_order(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        l102 = graph(run_world).entity(order_entity_id("L102"))["attrs"]
        assert "invoice_id" in l102 and l102["invoice_id"] is None
        assert "event_type" in l102 and l102["event_type"] is None


def test_columns_not_selected_are_not_recorded(run_world):
        # "List completed orders with no invoice" selecting only order columns + invoice_id.
        run_world.record_query("SELECT order_id, lab_status, invoice_id FROM t WHERE invoice_id IS NULL",
                               [{"order_id": "L102", "lab_status": "COMPLETED", "invoice_id": None}], 1, False)
        l102 = graph(run_world).entity(order_entity_id("L102"))["attrs"]
        assert l102 == {"order_id": "L102", "lab_status": "COMPLETED", "invoice_id": None}
        assert "event_type" not in l102  # never retrieved, so not evidence


def test_replacement_link_is_derived_from_history_notes(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        replaced = graph(run_world).neighbors_of(invoice_entity_id("I104"), via="replaces")
        assert [e["id"] for e in replaced] == [invoice_entity_id("I102")]
        relate_events = [e for e in run_world.world.events()
                         if e["kind"] == "relate" and e["payload"]["type"] == "replaces"]
        assert relate_events[0]["payload"]["source"]["kind"] == "derived"


def test_zetaris_writes_carry_zetaris_provenance(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        assert graph(run_world).entity(invoice_entity_id("I104"))["source"]["kind"] == "zetaris"
        assert graph(run_world).entity(invoice_entity_id("I104"))["source"]["by"] == "data_agent"


def test_each_query_is_recorded_as_a_context_under_the_attempt(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        queries = [e["payload"] for e in run_world.world.events() if e["kind"] == "upsertContext" and e["payload"]["type"] == "query"]
        assert queries[0]["attrs"] == {"sql": "SELECT * FROM t", "row_count": 5, "total_count": 5, "truncated": False}
        assert queries[0]["parent"] == run_world.attempt_context


def test_findings_become_investigator_facts_about_the_order(run_world):
        findings = {"findings": [{"order_id": "L103", "finding_type": "UNPAID_REPLACEMENT_INVOICE", "confidence": "high"}]}
        assert run_world.record_findings(json.dumps(findings)) == 1
        facts = run_world.world.view("facts").about(order_entity_id("L103"))
        assert facts[0]["value"]["finding_type"] == "UNPAID_REPLACEMENT_INVOICE"
        assert facts[0]["confidence"] == 0.9
        assert facts[0]["source"]["by"] == "investigator"
        assert facts[0]["context"] == run_world.attempt_context


def test_later_attempt_finding_wins(run_world):
        run_world.record_findings(json.dumps({"findings": [{"order_id": "L102", "finding_type": "MISSING_INVOICE_CANDIDATE", "confidence": "high"}]}))
        run_world.start_attempt(2)
        run_world.record_findings(json.dumps({"findings": [{"order_id": "L102", "finding_type": "MISSING_INVOICE_CANDIDATE", "confidence": "medium"}]}))
        facts = run_world.world.view("facts").about(order_entity_id("L102"))
        assert len(facts) == 2
        current = [f for f in facts if not f.get("superseded_by")]
        assert [f["value"]["confidence"] for f in current] == ["medium"]


def test_invalid_findings_json_records_nothing(run_world):
        assert run_world.record_findings("not json") == 0


def test_verdict_is_recorded_on_the_attempt(run_world):
        run_world.record_verdict("RETRY", [{"order_id": "L102", "check": "confidence", "problem": "too high"}])
        attempt = [e for e in run_world.world.events()
                   if e["kind"] == "upsertContext" and e["payload"]["id"] == run_world.attempt_context][-1]["payload"]
        assert attempt["attrs"]["verdict"] == "RETRY"
        assert attempt["source"]["by"] == "critic"


def test_register_is_saved_to_disk(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        run_world.finish("VERIFIED", 1)
        saved = json.loads(run_world.file.read_text())
        assert saved["events"] and saved["audit"]
        run_updates = [e["payload"]["attrs"] for e in saved["events"]
                       if e["kind"] == "upsertContext" and e["payload"]["type"] == "run"]
        assert run_updates[-1] == {"status": "VERIFIED", "attempts": 1}


def test_current_is_set_only_inside_activate(run_world):
        assert careops_world.current() is None
        with careops_world.activate(run_world):
                assert careops_world.current() is run_world
        assert careops_world.current() is None


# ------------------------------------------------- retry context (summary)

def test_summary_groups_retrieved_data_by_order(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        summary = careops_world.ZetarisData(run_world.world).summary()
        assert sorted(summary["orders"]) == ["L101", "L102", "L103", "L104"]
        assert sorted(summary["orders"]["L103"]["invoices"]) == ["I102", "I104"]
        assert summary["orders"]["L103"]["invoices"]["I104"]["notes"] == "Replacement for I102"
        assert summary["orders"]["L102"]["invoices"] == {}
        assert summary["orders"]["L102"]["order"]["invoice_id"] is None
        assert summary["replacement_links_derived_from_notes"] == ["I104 replaces I102"]


def test_summary_leaves_out_agent_written_values(run_world):
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        run_world.record_findings(json.dumps({"findings": [{"order_id": "L102", "finding_type": "MISSING_INVOICE_CANDIDATE"}]}))
        assert "finding" not in careops_world.ZetarisData(run_world.world).summary()["orders"]["L102"]["order"]


def test_summary_only_has_columns_that_were_selected(run_world):
        run_world.record_query("SELECT order_id, invoice_id FROM t", [{"order_id": "L102", "invoice_id": None}], 1, False)
        assert careops_world.ZetarisData(run_world.world).summary()["orders"]["L102"]["order"] == {"order_id": "L102", "invoice_id": None}


def test_invoice_without_selected_order_is_listed_separately(run_world):
        run_world.record_query("SELECT invoice_id, payment_status FROM t", [{"invoice_id": "I104", "payment_status": "UNPAID"}], 1, False)
        summary = careops_world.ZetarisData(run_world.world).summary()
        assert summary["orders"] == {} and summary["invoices_without_order"]["I104"]["payment_status"] == "UNPAID"


def test_retrieved_context_is_none_before_any_query(run_world):
        assert run_world.retrieved_context() is None
        run_world.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        assert json.loads(run_world.retrieved_context())["orders"]["L101"]["invoices"]["I101"]["payment_status"] == "PAID"
