"""CareOps wiring for the Meterless World Model engine: one register per investigation run.

What goes into the register:
- Zetaris rows (source kind "zetaris"): each row is split into an order entity and,
  when it has one, an invoice entity, linked by invoice -bills-> order. Only the
  columns the query actually selected are written, so a NULL that Zetaris returned
  (invoice_id: None) is kept apart from a column that was never retrieved.
- Replacement links read from billing-history notes (source kind "derived").
- Investigator findings as facts about the order (source kind "agent").
- Run, attempt and query contexts, including the critic's verdict per attempt.

The orchestrator activates a RunWorld for the duration of a run; tools find it
through current(), so their arguments (and what the LLM sees) do not change.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from careops.world_model import WorldModel, external_id, to_iso

log = logging.getLogger("careops.world")

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # registers are saved under CareOps/.world/

ORDER_COLUMNS = ("order_id", "patient_id", "test_code", "order_date", "lab_status")
INVOICE_COLUMNS = ("invoice_id", "invoice_status", "payment_status", "event_type", "event_date", "notes")
CONFIDENCE_SCORES = {"high": 0.9, "medium": 0.6, "low": 0.3}
_REPLACEMENT_NOTE = re.compile(r"Replacement for (\w+)", re.IGNORECASE)

_current: ContextVar[RunWorld | None] = ContextVar("careops_run_world", default=None)


def order_entity_id(order_id: str) -> str:
        return external_id("order", "zetaris", order_id)


def invoice_entity_id(invoice_id: str) -> str:
        return external_id("invoice", "zetaris", invoice_id)


class ZetarisData:
        """What Zetaris returned in this run, read from the register's event log.

        Only writes with source kind "zetaris" count; values agents wrote (facts) are excluded.
        Columns that no query selected are absent, not None.
        """

        def __init__(self, world: WorldModel):
                self.attrs: dict[str, dict] = {}                # entity id -> attrs from Zetaris writes only
                self.bills: set[tuple[str, str]] = set()        # (invoice id, order id)
                self.replaces: set[tuple[str, str]] = set()     # (new invoice id, old invoice id), derived
                for event in world.events():
                        payload = event["payload"]
                        if event["kind"] == "upsertEntity" and payload["source"]["kind"] == "zetaris":
                                self.attrs.setdefault(payload["id"], {}).update(payload["attrs"])
                        elif event["kind"] == "relate" and payload["type"] == "bills":
                                self.bills.add((payload["from"], payload["to"]))
                        elif event["kind"] == "relate" and payload["type"] == "replaces":
                                self.replaces.add((payload["from"], payload["to"]))

        def invoices_of(self, order: str) -> list[str]:
                return sorted(invoice for invoice, billed in self.bills if billed == order)

        def summary(self) -> dict:
                """Deduplicated per-order view of everything retrieved: the context handed to a retrying investigator."""
                short = lambda entity_id: entity_id.rsplit(":", 1)[-1]
                orders = {
                        short(eid): {"order": attrs,
                                     "invoices": {short(i): self.attrs.get(i, {}) for i in self.invoices_of(eid)}}
                        for eid, attrs in sorted(self.attrs.items()) if eid.startswith("order:")
                }
                summary: dict = {"orders": orders}
                linked = {invoice for invoice, _ in self.bills}
                unlinked = {short(eid): attrs for eid, attrs in sorted(self.attrs.items())
                            if eid.startswith("invoice:") and eid not in linked}
                if unlinked:
                        summary["invoices_without_order"] = unlinked
                if self.replaces:
                        summary["replacement_links_derived_from_notes"] = sorted(
                                f"{short(new)} replaces {short(old)}" for new, old in self.replaces)
                return summary


def current() -> RunWorld | None:
        """The register of the run in progress, or None outside a run."""
        return _current.get()


@contextmanager
def activate(run_world: RunWorld) -> Iterator[RunWorld]:
        token = _current.set(run_world)
        try:
                yield run_world
        finally:
                _current.reset(token)


class RunWorld:
        """The World Model register for one investigation run, saved to .world/<run_id>.json."""

        def __init__(self, run_id: str, request: str, directory: Path = PROJECT_ROOT):
                self.run_id = run_id
                self.world = WorldModel(namespace=run_id, storage="local", directory=directory)
                self.file = Path(directory) / ".world" / f"{run_id}.json"
                self.run_context = external_id("run", "careops", run_id)
                self.attempt_context: str | None = None
                self.attempt_number = 0
                self.world.upsert_context(type="run", external_key=("careops", run_id),
                                          attrs={"request": request}, source=self._source("orchestrator"))

        def _source(self, by: str, kind: str = "agent") -> dict:
                return {"kind": kind, "by": by, "at": to_iso(datetime.now(timezone.utc)), "run_id": self.run_id}

        # ------------------------------------------------------------ runs

        def start_attempt(self, number: int) -> None:
                context = self.world.upsert_context(type="attempt", external_key=("careops", f"{self.run_id}-attempt-{number}"),
                                                    parent=self.run_context, attrs={"number": number},
                                                    source=self._source("orchestrator"))
                self.attempt_context = context["id"]
                self.attempt_number = number

        def finish(self, status: str, attempts: int) -> None:
                self.world.upsert_context(type="run", external_key=("careops", self.run_id),
                                          attrs={"status": status, "attempts": attempts}, source=self._source("orchestrator"))
                log.info("register saved: %s (%d events, %d audit entries)",
                         self.file, len(self.world.events()), len(self.world.audit()))

        # ------------------------------------------------------- Zetaris data

        def record_query(self, sql: str, rows: list[dict], total_count: int, truncated: bool) -> None:
                source = self._source("data_agent", kind="zetaris")
                self.world.upsert_context(type="query", parent=self.attempt_context or self.run_context,
                                          attrs={"sql": sql, "row_count": len(rows), "total_count": total_count,
                                                 "truncated": truncated},
                                          source=source)
                for row in rows:
                        self._record_row(row, source)
                log.info("recorded %d row(s) from Zetaris", len(rows))

        def _record_row(self, row: dict, source: dict) -> None:
                order_id, invoice_id = row.get("order_id"), row.get("invoice_id")
                order_attrs = {c: row[c] for c in ORDER_COLUMNS if c in row}
                invoice_attrs = {c: row[c] for c in INVOICE_COLUMNS if c in row}
                if invoice_id:
                        self.world.upsert_entity(type="invoice", external_key=("zetaris", invoice_id),
                                                 attrs=invoice_attrs, source=source)
                        if order_id:
                                self.world.upsert_entity(type="order", external_key=("zetaris", order_id),
                                                         attrs=order_attrs, source=source)
                                self.world.relate(from_=invoice_entity_id(invoice_id), type="bills",
                                                  to=order_entity_id(order_id), source=source)
                        self._record_replacement(row, invoice_id)
                elif order_id:
                        # No invoice on this row: the invoice columns the query selected are NULL,
                        # and that absence is itself evidence about the order.
                        self.world.upsert_entity(type="order", external_key=("zetaris", order_id),
                                                 attrs={**order_attrs, **invoice_attrs}, source=source)

        def _record_replacement(self, row: dict, invoice_id: str) -> None:
                if row.get("event_type") != "REPLACEMENT_INVOICE_CREATED":
                        return
                match = _REPLACEMENT_NOTE.search(row.get("notes") or "")
                if match:
                        # Read from the history note, not stated by Zetaris as a link: kind "derived".
                        self.world.relate(from_=invoice_entity_id(invoice_id), type="replaces",
                                          to=invoice_entity_id(match.group(1)),
                                          source=self._source("data_agent", kind="derived"))

        def retrieved_context(self) -> str | None:
                """Everything Zetaris returned so far in this run, as compact JSON; None if nothing yet."""
                summary = ZetarisData(self.world).summary()
                return json.dumps(summary, indent=1) if summary["orders"] or len(summary) > 1 else None

        # ---------------------------------------------------- agent outputs

        def record_findings(self, findings_json: str) -> int:
                """Store each investigator finding as a fact about its order. Returns how many were stored."""
                try:
                        findings = json.loads(findings_json)["findings"]
                except (ValueError, KeyError, TypeError):
                        log.warning("findings are not valid JSON; nothing recorded")
                        return 0
                source = self._source("investigator")
                for finding in findings:
                        if not finding.get("order_id"):
                                continue
                        self.world.assert_fact(about=order_entity_id(finding["order_id"]), predicate="finding",
                                               value=finding, confidence=CONFIDENCE_SCORES.get(finding.get("confidence"), 0.5),
                                               context=self.attempt_context, source=source)
                return len(findings)

        def record_verdict(self, verdict: str, issues: list[dict]) -> None:
                self.world.upsert_context(type="attempt", external_key=("careops", f"{self.run_id}-attempt-{self.attempt_number}"),
                                          parent=self.run_context, attrs={"verdict": verdict, "issues": issues},
                                          source=self._source("critic"))
