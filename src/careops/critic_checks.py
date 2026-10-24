"""Deterministic critic checks: investigator findings verified against the World Model register.

No LLM. Each check either passes or returns an issue the investigator can act on.
Evidence is matched against what Zetaris itself returned: only register writes with
source kind "zetaris" count, never values that agents wrote.

Known limit: a claim that only follows from a query's WHERE clause (e.g. "L102 is
completed" from WHERE lab_status = 'COMPLETED' without selecting lab_status) is not
checked here; that would need SQL parsing.
"""

from __future__ import annotations

import json

from careops.careops_world import ZetarisData, invoice_entity_id, order_entity_id
from careops.world_model import WorldModel

FINDING_TYPES = {
        "RECONCILED", "MISSING_INVOICE_CANDIDATE", "UNPAID_INVOICE", "UNPAID_REPLACEMENT_INVOICE",
        "CANCELLED_WITHOUT_REPLACEMENT", "POSSIBLE_DUPLICATE_BILLING", "INSUFFICIENT_EVIDENCE",
}
REQUIRED_FIELDS = (
        "order_id", "finding_type", "invoice_ids", "evidence", "explanation", "confidence",
        "uncertainty", "recommended_action", "requires_human_review",
)
EMPTY_UNCERTAINTY = {"", "none", "n/a", "na", "null", "unknown"}


def check_findings(findings_json: str, world: WorldModel) -> list[dict]:
        """Return a list of issues ({order_id, check, problem}); empty means every check passed."""
        issues: list[dict] = []

        def add(order_id, check: str, problem: str) -> None:
                issues.append({"order_id": order_id, "check": f"code:{check}", "problem": problem})

        try:
                findings = json.loads(findings_json)["findings"]
                if not isinstance(findings, list):
                        raise TypeError
        except (ValueError, KeyError, TypeError):
                add(None, "format", "Output is not a JSON object with a 'findings' list. "
                                    "Return ONLY the JSON object described in your instructions.")
                return issues

        register = ZetarisData(world)
        for finding in findings:
                if not isinstance(finding, dict):
                        add(None, "format", "Each entry in 'findings' must be a JSON object.")
                        continue
                _check_finding(finding, register, add)
        return issues


def _check_finding(finding: dict, register: ZetarisData, add) -> None:
        order_id = finding.get("order_id")
        order = order_entity_id(order_id) if order_id else None

        # A. format
        missing = [f for f in REQUIRED_FIELDS if f not in finding]
        if missing:
                add(order_id, "fields", f"Finding is missing required field(s): {', '.join(missing)}.")
        finding_type = finding.get("finding_type")
        if finding_type not in FINDING_TYPES:
                add(order_id, "finding_type",
                    f"finding_type {finding_type!r} is not allowed; use one of: {', '.join(sorted(FINDING_TYPES))}.")
        if not order or order not in register.attrs:
                add(order_id, "order_retrieved",
                    f"Order {order_id!r} was never retrieved from Zetaris; retrieve it before reporting a finding.")
                return

        # B. evidence, matched against what Zetaris returned
        evidence = finding.get("evidence") or []
        if not evidence:
                add(order_id, "evidence_present",
                    "Evidence is empty. Cite the retrieved fields that support this finding "
                    "(for a missing invoice, cite the order's row with invoice_id null).")
        for item in evidence:
                _check_evidence_item(order_id, order, item, register, add)
        billed = register.invoices_of(order)
        for invoice_id in finding.get("invoice_ids") or []:
                if not isinstance(invoice_id, str) or not invoice_id:
                        add(order_id, "invoice_ids_format",
                            f'invoice_ids contains {json.dumps(invoice_id)}. List only real invoice ids; '
                            f'when an order has no invoice, use an empty list: "invoice_ids": [].')
                elif invoice_entity_id(invoice_id) not in billed:
                        add(order_id, "invoice_belongs",
                            f"Invoice {invoice_id} is not an invoice of order {order_id} in the retrieved data.")

        # C. business rules
        if finding_type == "MISSING_INVOICE_CANDIDATE":
                if finding.get("confidence") == "high":
                        add(order_id, "confidence",
                            "A missing invoice is unverified (it may exist in a system we cannot see), "
                            "so confidence must be medium or low, not high.")
                if str(finding.get("uncertainty", "")).strip().lower() in EMPTY_UNCERTAINTY:
                        add(order_id, "uncertainty",
                            "State the remaining uncertainty, e.g. the invoice may exist in another billing system.")
                if billed:
                        add(order_id, "missing_invoice_contradicted",
                            f"Order {order_id} has invoice(s) {', '.join(i.split(':')[-1] for i in billed)} "
                            f"in the retrieved data, so it is not a missing invoice.")
        if finding_type == "UNPAID_REPLACEMENT_INVOICE":
                if not any(new in billed for new, _old in register.replaces):
                        add(order_id, "replacement_supported",
                            f"No replacement is recorded for order {order_id}'s invoices. Retrieve event_type and "
                            f"notes for its invoices; only claim a replacement that the billing history shows.")
        if finding_type == "RECONCILED":
                paid = [i for i in billed if register.attrs.get(i, {}).get("invoice_status") == "ISSUED"
                        and register.attrs.get(i, {}).get("payment_status") == "PAID"]
                if not paid:
                        add(order_id, "reconciled_supported",
                            f"RECONCILED needs an ISSUED and PAID invoice for order {order_id}; none was retrieved.")
        elif finding.get("requires_human_review") is not True:
                add(order_id, "human_review", "Every finding except RECONCILED must set requires_human_review to true.")


def _check_evidence_item(order_id, order: str, item, register: ZetarisData, add) -> None:
        if not isinstance(item, dict) or "field" not in item or "value" not in item:
                add(order_id, "evidence_format", f"Evidence item {item!r} must have invoice_id, field and value.")
                return
        invoice_id, field, value = item.get("invoice_id"), item["field"], item["value"]
        target = invoice_entity_id(invoice_id) if invoice_id else order
        label = f"invoice {invoice_id}" if invoice_id else f"order {order_id}"
        retrieved = register.attrs.get(target)
        if retrieved is None:
                add(order_id, "evidence_record",
                    f"Evidence cites invoice_id {invoice_id!r}, but no such invoice was retrieved from Zetaris. "
                    f"Use a real invoice id from the data (use invoice_id null for the order's own row).")
        elif field not in retrieved:
                add(order_id, "evidence_retrieved",
                    f"Evidence {label} {field} = {value!r}: column '{field}' was never retrieved for this record. "
                    f"Retrieve it or remove the evidence item.")
        elif retrieved[field] != value:
                add(order_id, "evidence_value",
                    f"Evidence says {label} {field} = {value!r}, but Zetaris returned {retrieved[field]!r}.")
