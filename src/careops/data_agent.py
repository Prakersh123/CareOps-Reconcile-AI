import json
import logging
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.messages import HumanMessage, ToolMessage
from langchain.tools import tool

from careops.llm import default_model
from careops.logging_setup import short
from careops.zetaris_client import ZetarisClient, ZetarisError

log = logging.getLogger("careops.data_agent")

# Load CareOps/.env no matter which directory the script is run from.
# Must happen before create_agent, which reads the provider API key from the environment.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

SCHEMA_INFO = """
You can query exactly one table, a Zetaris Unified Semantic Layer (USL) table:

    lightning.metastore.careops.CAREOPS_RECONCILIATION.CAREOPS_RECONCILIATION

It already joins three source systems (lab orders, billing records, billing history),
so never write JOINs and never query any other table.

Grain: one row = one lab order + one of its invoices.
- An order with no invoice appears once, with NULL invoice columns.
- An order with several invoices appears once per invoice
  (e.g. an order L900 with invoices I900 and I901 has two rows). Use DISTINCT when listing orders.

Columns (all STRING; dates are 'YYYY-MM-DD' strings):
| column         | meaning                                   | values seen in data                                  |
|----------------|-------------------------------------------|------------------------------------------------------|
| order_id       | lab order identifier                      | L101, L102, L103, L104                               |
| patient_id     | synthetic patient identifier              | P101, P102, P103, P104                               |
| test_code      | lab test code                             | CBC, LFT, KFT                                        |
| order_date     | date the lab order was placed             | 2026-10-20, 2026-10-21                               |
| lab_status     | lab order status                          | COMPLETED                                            |
| invoice_id     | invoice for the order; NULL = no invoice  | I101, I102, I103, I104                               |
| invoice_status | invoice lifecycle status                  | ISSUED, CANCELLED                                    |
| payment_status | payment state of the invoice              | PAID, UNPAID                                         |
| event_type     | billing history event for this invoice    | INVOICE_CANCELLED, REPLACEMENT_INVOICE_CREATED, NULL |
| event_date     | date of the history event                 | 2026-10-21, NULL                                     |
| notes          | free-text history note                    | 'Cancelled as duplicate', 'Replacement for I102'     |

Rules:
- Status values are UPPERCASE; match them exactly (e.g. payment_status = 'UNPAID').
- Use IS NULL / IS NOT NULL for missing values, never = NULL.
- Read-only: only a single SELECT statement. No INSERT, UPDATE, DELETE or DDL.
- Always add LIMIT 200 unless the question needs fewer rows.
- If the question needs a column that does not exist (e.g. amount), say so instead of guessing.
"""


USL_TABLE = "lightning.metastore.careops.CAREOPS_RECONCILIATION.CAREOPS_RECONCILIATION"

# One client for the whole run: logs in on the first query, then reuses the token.
zetaris = ZetarisClient.from_env()


def _tables_used(sql: str) -> set[str]:
        return {name.lower() for name in re.findall(r"\b(?:FROM|JOIN)\s+([\w.`]+)", sql, re.IGNORECASE)}


@tool
def queryExecutor(query: str) -> str:
        """Execute one read-only SELECT query on Zetaris and return the result as JSON.

        Only the CAREOPS_RECONCILIATION USL table may be queried.
        On failure an error message is returned; fix the query and call the tool again.
        """
        log.info("SQL: %s", short(query, 300))
        other_tables = _tables_used(query) - {USL_TABLE.lower()}
        if other_tables:
                log.warning("  blocked: query touches other tables %s", sorted(other_tables))
                return f"ERROR: only {USL_TABLE} may be queried, got: {', '.join(sorted(other_tables))}"
        started = time.monotonic()
        try:
                result = zetaris.select(query, max_rows=200)
        except ZetarisError as exc:
                log.warning("  query failed (agent will see the error and may retry): %s", short(exc))
                # Returned, not raised, so the agent can read the error and retry with a fixed query.
                return f"ERROR: {exc}"
        log.info("  -> %d rows (total %d%s) in %.1fs", len(result.rows), result.total_count,
                 ", TRUNCATED" if result.truncated else "", time.monotonic() - started)
        return json.dumps({
                "sql": result.sql,
                "row_count": len(result.rows),
                "total_count": result.total_count,
                "truncated": result.truncated,
                "rows": result.rows,
        })



agent = create_agent(
        model=default_model(),
        system_prompt="You are an expert sql query writer from natural language and executor and gives result using tool\n" + SCHEMA_INFO,
        tools=[queryExecutor]
        )


def call_agent(query:str)->str:
        log.info("request: %s", short(query))
        result = agent.invoke(
        {
                "messages": [
                        HumanMessage(content=query)
                ]
        }
        )
        # Return the raw output of every successful queryExecutor call, unmodified and in order,
        # so callers get exactly what Zetaris returned rather than the LLM's summary of it.
        tool_outputs = [m.content for m in result["messages"]
                        if isinstance(m, ToolMessage) and m.name == "queryExecutor"]
        successful = [out for out in tool_outputs if not out.startswith("ERROR")]
        failed = len(tool_outputs) - len(successful)
        if successful:
                log.info("returning %d query result(s)%s", len(successful),
                         f" ({failed} failed attempt(s) discarded)" if failed else "")
                # Always a JSON list (one entry per query), so the format is the same for one or many.
                return "[" + ", ".join(successful) + "]"
        if tool_outputs:
                log.warning("all %d queries failed; returning last error", failed)
                return tool_outputs[-1]  # every attempt failed: pass the last error on
        # No query was run (e.g. the question needs a column that does not exist).
        log.warning("no query run: %s", short(result["messages"][-1].content))
        return "NO QUERY RUN: " + result["messages"][-1].content



