"""SAMPLE_MODE: the pipeline without Zetaris, a model, or network access.

What stays real: the World Model register (Meterless engine), the critic's code checks
against it, the report, and the trace. What is substituted:
- Zetaris -> the bundled synthetic USL rows in data/sample_usl_rows.json
- the LLM investigator -> classify(), a deterministic rule-based stand-in applying the
  same business rules as the investigator's prompt
- the LLM critic -> skipped; the verdict comes from the code checks alone
Every event says so, so nobody mistakes a sample run for an agent run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app import data_check, progress
from app.engines.careops_world import RunWorld
from app.paths import PROJECT_ROOT
from app.agents.critic_checks import check_findings, norm

SAMPLE_FILE = PROJECT_ROOT / "data" / "sample_usl_rows.json"
USL_TABLE = data_check.USL_TABLE
_REPLACEMENT_NOTE = re.compile(r"Replacement for (\w+)", re.IGNORECASE)
_ORDER_ID = re.compile(r"\bL\d{3,}\b", re.IGNORECASE)


def load_rows(path: Path = SAMPLE_FILE) -> list[dict]:
        return json.loads(path.read_text(encoding="utf-8"))["rows"]


def _evidence(invoice: dict, fields=("invoice_status", "payment_status", "event_type", "notes")) -> list[dict]:
        return [{"invoice_id": invoice["invoice_id"], "field": f, "value": invoice[f]}
                for f in fields if f in invoice and invoice[f] is not None]


def classify(order_id: str, rows: list[dict]) -> dict:
        """One finding for one order, by the investigator's business rules."""
        invoices = {r["invoice_id"]: r for r in rows if r.get("invoice_id")}
        base = {"order_id": order_id, "requires_human_review": True, "confidence": "high"}
        if not invoices:
                return {**base, "finding_type": "MISSING_INVOICE_CANDIDATE", "invoice_ids": [], "confidence": "medium",
                        "evidence": [{"invoice_id": None, "field": "invoice_id", "value": None}],
                        "explanation": f"Completed order {order_id} has no invoice in the available billing data. "
                                       f"This is a candidate, not a confirmed billing error.",
                        "uncertainty": "The invoice may exist in a billing system this data does not cover.",
                        "recommended_action": f"Ask the billing team whether {order_id} was invoiced elsewhere."}

        replaced = {}  # new invoice -> the invoice it replaces, from billing history
        for r in invoices.values():
                match = _REPLACEMENT_NOTE.search(r.get("notes") or "")
                if r.get("event_type") == "REPLACEMENT_INVOICE_CREATED" and match:
                        replaced[r["invoice_id"]] = match.group(1)
        issued = [i for i, r in invoices.items() if norm(r.get("invoice_status")) == "ISSUED"]
        cancelled = [i for i, r in invoices.items() if norm(r.get("invoice_status")) == "CANCELLED"]
        ids = sorted(invoices)
        evidence = [item for i in ids for item in _evidence(invoices[i])]

        if replaced:
                new, old = next(iter(replaced.items()))
                if norm(invoices[new].get("payment_status")) == "PAID":
                        return {**base, "finding_type": "RECONCILED", "invoice_ids": ids, "evidence": evidence,
                                "requires_human_review": False,
                                "explanation": f"{old} was cancelled and replaced by {new}, which is issued and paid.",
                                "uncertainty": "None material.", "recommended_action": "No action required."}
                return {**base, "finding_type": "UNPAID_REPLACEMENT_INVOICE", "invoice_ids": ids, "evidence": evidence,
                        "explanation": f"Billing history shows {new} replaced {old}; this is a replacement, not duplicate "
                                       f"billing. {new} is issued but unpaid.",
                        "uncertainty": f"A payment for {new} may exist that has not been recorded yet.",
                        "recommended_action": f"Follow up on payment of {new}."}
        if len(issued) > 1:
                return {**base, "finding_type": "POSSIBLE_DUPLICATE_BILLING", "invoice_ids": ids, "evidence": evidence,
                        "confidence": "medium",
                        "explanation": f"{order_id} has {len(issued)} issued invoices and no history explaining them.",
                        "uncertainty": "A replacement or split invoice may not be recorded in the history.",
                        "recommended_action": "Review the invoices with the billing team before any correction."}
        if issued:
                invoice = invoices[issued[0]]
                if norm(invoice.get("payment_status")) == "PAID":
                        return {**base, "finding_type": "RECONCILED", "invoice_ids": ids,
                                "evidence": _evidence(invoice, ("invoice_status", "payment_status")),
                                "requires_human_review": False,
                                "explanation": f"{issued[0]} is issued and paid.",
                                "uncertainty": "None material.", "recommended_action": "No action required."}
                return {**base, "finding_type": "UNPAID_INVOICE", "invoice_ids": ids, "evidence": evidence,
                        "explanation": f"{issued[0]} is issued but unpaid; this needs payment follow-up, not correction.",
                        "uncertainty": "Payment may be in transit or not yet recorded.",
                        "recommended_action": f"Follow up on payment of {issued[0]}."}
        if cancelled:
                return {**base, "finding_type": "CANCELLED_WITHOUT_REPLACEMENT", "invoice_ids": ids, "evidence": evidence,
                        "explanation": f"{', '.join(cancelled)} was cancelled and no replacement invoice was found.",
                        "uncertainty": "A replacement may exist in a system this data does not cover.",
                        "recommended_action": f"Check whether {order_id} should be re-invoiced."}
        return {**base, "finding_type": "INSUFFICIENT_EVIDENCE", "invoice_ids": ids, "evidence": evidence,
                "confidence": "low", "explanation": "The invoice states do not match any known pattern.",
                "uncertainty": "Unclassified invoice states.", "recommended_action": "Review manually."}


def run_sample(request: str, run_world: RunWorld) -> dict:
        """One offline attempt: sample rows -> register -> rule-based findings -> code checks. Returns the final state."""
        rows = [r for r in load_rows() if r.get("lab_status") == "COMPLETED"]
        wanted = {m.upper() for m in _ORDER_ID.findall(request)}
        if wanted:
                rows = [r for r in rows if r["order_id"] in wanted]
        sql = f"SELECT * FROM {USL_TABLE} WHERE lab_status = 'COMPLETED'" + (
                f" AND order_id IN ({', '.join(repr(o) for o in sorted(wanted))})" if wanted else "")

        run_world.start_attempt(1)
        progress.emit("attempt_started", attempt=1, max_attempts=1, retry_issues=[])
        progress.emit("data_request", request="[sample mode] all completed orders with invoices and history"
                      + (f" for {', '.join(sorted(wanted))}" if wanted else ""))
        progress.emit("query", sql=f"{sql}\n-- sample mode: answered from data/sample_usl_rows.json, not Zetaris",
                      rows=len(rows), total=len(rows), truncated=False, seconds=0.0)
        run_world.record_query(sql, rows, len(rows), False, origin="bundled sample data (sample mode)")

        by_order: dict[str, list[dict]] = {}
        for row in rows:
                by_order.setdefault(row["order_id"], []).append(row)
        findings = [classify(order_id, order_rows) for order_id, order_rows in sorted(by_order.items())]
        findings_json = json.dumps({"findings": findings})
        progress.emit("investigator_done", attempt=1, data_requests=1,
                      completion="sample mode: rule-based stand-in, no model call",
                      findings=[{"order_id": f["order_id"], "finding_type": f["finding_type"],
                                 "confidence": f["confidence"]} for f in findings])
        run_world.record_findings(findings_json)

        issues = check_findings(findings_json, run_world.world)
        progress.emit("code_checks", attempt=1, passed=not issues, issues=issues)
        verdict = "ACCEPT" if not issues else "RETRY"
        progress.emit("verdict", attempt=1, verdict=verdict, decided_by="code", issues=issues)
        run_world.record_verdict(verdict, issues, decided_by="code")
        return {"request": request, "findings": findings_json, "retrieved": [], "verdict": None, "attempt": 1,
                "status": "VERIFIED" if not issues else "INCONCLUSIVE",
                "stop_reason": "sample mode: code checks only, LLM critic skipped"}
