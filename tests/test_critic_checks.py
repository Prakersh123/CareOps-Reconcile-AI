"""Code checks of the critic, against a register filled with the real USL rows."""

import copy
import json

import pytest

from careops.careops_world import RunWorld
from careops.critic_checks import check_findings

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

# Findings from a real run (run-20261023-164756) that the LLM critic accepted.
L102 = {
        "order_id": "L102", "finding_type": "MISSING_INVOICE_CANDIDATE", "invoice_ids": [],
        "evidence": [{"invoice_id": None, "field": "invoice_id", "value": None},
                     {"invoice_id": None, "field": "notes", "value": None}],
        "explanation": "Completed lab order L102 has no invoice in the available billing data.",
        "confidence": "medium", "uncertainty": "The invoice may exist in a system we cannot see.",
        "recommended_action": "Verify with the billing team.", "requires_human_review": True,
}
L103 = {
        "order_id": "L103", "finding_type": "UNPAID_REPLACEMENT_INVOICE", "invoice_ids": ["I102", "I104"],
        "evidence": [{"invoice_id": "I102", "field": "invoice_status", "value": "CANCELLED"},
                     {"invoice_id": "I104", "field": "notes", "value": "Replacement for I102"},
                     {"invoice_id": "I104", "field": "payment_status", "value": "UNPAID"}],
        "explanation": "I102 was cancelled and replaced by I104, which is unpaid.",
        "confidence": "high", "uncertainty": "A payment for I104 may not be recorded yet.",
        "recommended_action": "Check payment of I104.", "requires_human_review": True,
}
L101 = {
        "order_id": "L101", "finding_type": "RECONCILED", "invoice_ids": ["I101"],
        "evidence": [{"invoice_id": "I101", "field": "payment_status", "value": "PAID"}],
        "explanation": "I101 is issued and paid.", "confidence": "high", "uncertainty": "None material.",
        "recommended_action": "None.", "requires_human_review": False,
}


@pytest.fixture
def world(tmp_path):
        rw = RunWorld("run-test", "test", directory=tmp_path)
        rw.start_attempt(1)
        rw.record_query("SELECT * FROM t", ALL_ROWS, 5, False)
        return rw.world


def run(world, *findings):
        return check_findings(json.dumps({"findings": list(findings)}), world)


def checks(issues):
        return {i["check"] for i in issues}


def with_changes(finding, **changes):
        return {**copy.deepcopy(finding), **changes}


# ------------------------------------------------------------------ passes

def test_real_accepted_findings_pass(world):
        assert run(world, L102, L103, L101) == []


# ------------------------------------------------------------------ format

def test_not_json(world):
        assert checks(check_findings("Here are my findings: ...", world)) == {"code:format"}


def test_findings_not_a_list(world):
        assert checks(check_findings(json.dumps({"findings": "L102"}), world)) == {"code:format"}


def test_unknown_finding_type(world):
        assert "code:finding_type" in checks(run(world, with_changes(L103, finding_type="DUPLICATE")))


def test_missing_fields(world):
        finding = with_changes(L103)
        del finding["uncertainty"]
        issues = run(world, finding)
        assert "code:fields" in checks(issues) and "uncertainty" in issues[0]["problem"]


def test_order_never_retrieved(world):
        assert checks(run(world, with_changes(L102, order_id="L999"))) == {"code:order_retrieved"}


# ---------------------------------------------------------------- evidence

def test_empty_evidence(world):
        assert "code:evidence_present" in checks(run(world, with_changes(L102, evidence=[])))


def test_order_id_used_as_invoice_id(world):
        bad = with_changes(L102, evidence=[{"invoice_id": "L102", "field": "invoice_status", "value": None}])
        assert "code:evidence_record" in checks(run(world, bad))


def test_column_never_retrieved(tmp_path):
        rw = RunWorld("run-partial", "test", directory=tmp_path)
        rw.record_query("SELECT order_id, invoice_id FROM t WHERE invoice_id IS NULL",
                        [{"order_id": "L102", "invoice_id": None}], 1, False)
        bad = with_changes(L102, evidence=[{"invoice_id": None, "field": "event_type", "value": None}])
        issues = run(rw.world, bad)
        assert checks(issues) == {"code:evidence_retrieved"}
        assert "never retrieved" in issues[0]["problem"]


def test_value_mismatch(world):
        bad = with_changes(L103, evidence=[{"invoice_id": "I104", "field": "payment_status", "value": "PAID"}])
        issues = run(world, bad)
        assert checks(issues) == {"code:evidence_value"}
        assert "'UNPAID'" in issues[0]["problem"]


def test_value_case_must_match(world):
        bad = with_changes(L103, evidence=[{"invoice_id": "I104", "field": "payment_status", "value": "unpaid"}])
        assert checks(run(world, bad)) == {"code:evidence_value"}


def test_invoice_of_another_order(world):
        assert "code:invoice_belongs" in checks(run(world, with_changes(L103, invoice_ids=["I102", "I104", "I101"])))


def test_agent_written_values_do_not_count_as_evidence(world):
        # An investigator fact on the order must not make "finding" look like retrieved data.
        world.assert_fact(about="order:zetaris:L102", predicate="finding", value="x",
                          source={"kind": "agent", "by": "investigator", "at": "2026-10-23T00:00:00.000Z"})
        bad = with_changes(L102, evidence=[{"invoice_id": None, "field": "finding", "value": "x"}])
        assert "code:evidence_retrieved" in checks(run(world, bad))


# ---------------------------------------------------------- business rules

def test_missing_invoice_with_high_confidence(world):
        assert checks(run(world, with_changes(L102, confidence="high"))) == {"code:confidence"}


@pytest.mark.parametrize("uncertainty", ["None", "", "unknown", "N/A"])
def test_missing_invoice_without_uncertainty(world, uncertainty):
        assert checks(run(world, with_changes(L102, uncertainty=uncertainty))) == {"code:uncertainty"}


def test_missing_invoice_for_an_order_that_has_invoices(world):
        bad = with_changes(L102, order_id="L104", evidence=[{"invoice_id": None, "field": "order_id", "value": "L104"}])
        assert "code:missing_invoice_contradicted" in checks(run(world, bad))


def test_replacement_claim_without_replacement_history(world):
        bad = with_changes(L103, order_id="L104", invoice_ids=["I103"],
                           evidence=[{"invoice_id": "I103", "field": "payment_status", "value": "PAID"}])
        assert "code:replacement_supported" in checks(run(world, bad))


def test_reconciled_needs_a_paid_invoice(world):
        bad = with_changes(L101, order_id="L103", invoice_ids=["I104"],
                           evidence=[{"invoice_id": "I104", "field": "payment_status", "value": "UNPAID"}])
        assert checks(run(world, bad)) == {"code:reconciled_supported"}


def test_human_review_required_except_reconciled(world):
        assert checks(run(world, with_changes(L103, requires_human_review=False))) == {"code:human_review"}
        assert run(world, L101) == []  # RECONCILED may skip human review


@pytest.mark.parametrize("bad", [None, "", 104])
def test_null_or_non_string_invoice_id_gets_a_clear_fix(world, bad):
        issues = run(world, with_changes(L102, invoice_ids=[bad]))
        assert checks(issues) == {"code:invoice_ids_format"}
        assert '"invoice_ids": []' in issues[0]["problem"]
