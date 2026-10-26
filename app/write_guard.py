"""Refuse requests to change data, before any agent runs.

CareOps is read-only by design: Zetaris is queried with SELECT only, and nothing writes to the
billing systems. A request like "delete invoice I102" or "mark I104 as paid" is answered with a
clear refusal instead of an investigation, without a model call. The rules look for an
instruction to change records (a write verb said as a command, or a SQL write statement), so
questions about changes, such as "which invoices were cancelled?", still run.
"""

from __future__ import annotations

import re

_WRITE_VERBS = (r"delete|remove|drop|erase|wipe|purge|truncate|update|modify|change|edit|overwrite|insert|"
                r"cancel|void|refund|reverse|mark|set|fix|correct|re-?issue|approve|pay|write\s+off|merge|"
                r"alter|rename|replace")

_PATTERNS = [
        # SQL write statements, anywhere in the text
        r"\b(delete\s+from|drop\s+(table|database|schema|view|namespace)|truncate(\s+table)?\s+\w|"
        r"update\s+\w+\s+set|insert\s+into|alter\s+table|create\s+(table|database)|grant\s|revoke\s)",
        # a write verb as a command: at the start of a sentence, after a polite lead-in, or chained with "and"
        rf"(^|[.!?;:]\s*|\b(please|kindly|can you|could you|would you|will you|go ahead and|"
        rf"i want you to|i need you to|you should|then|and)\s+)({_WRITE_VERBS})\b",
        # creating billing records
        r"\b(create|issue|raise|generate|add)\s+(a\s+|an\s+|the\s+|new\s+)*(invoice|bill|payment|record)s?\b",
]
_WRITE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)

REFUSAL = ("This system is read-only. It investigates lab orders and billing and recommends actions for a "
           "human, but it never changes, deletes or creates records in any system. Nothing was changed. "
           "Ask a question instead, for example: \"Which completed lab orders have no invoice?\"")


def write_request(text: str) -> str | None:
        """The words that ask for a change, or None when the request only asks for information."""
        match = _WRITE.search(text or "")
        return " ".join(match.group(0).split()) if match else None
