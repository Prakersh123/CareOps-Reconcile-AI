"""Is the CareOps data set up in Zetaris? Checked at startup, by /health, and before every real run.

The agents query one USL table; if it is missing (scripts/setup_zetaris.py was not run) a run
would spend minutes on model calls and then fail. This check says so up front, in plain words.
Sample mode does not need it.

One client per process, and it never retries a refused sign-in, so a wrong password costs one
failed attempt, not a locked Zetaris account.
"""

from __future__ import annotations

import logging
import re

from app.adapters.zetaris_client import ZetarisClient, ZetarisError

log = logging.getLogger("careops.data_check")

USL_TABLE = "lightning.metastore.careops.CAREOPS_RECONCILIATION.CAREOPS_RECONCILIATION"
SETUP_HINT = "run `python scripts/setup_zetaris.py` once (or use sample mode)"
# Spark/Zetaris wording when the namespace, USL or its activation is missing.
_MISSING = re.compile(r"not found|cannot be found|does not exist|not activated|invalid namespace|"
                      r"TABLE_OR_VIEW_NOT_FOUND|REQUIRES_SINGLE_PART_NAMESPACE", re.IGNORECASE)

_client: ZetarisClient | None = None


def _zetaris() -> ZetarisClient:
        global _client
        if _client is None:
                _client = ZetarisClient.from_env()
        return _client


def check() -> dict:
        """{"ready": bool, "problem": None | "zetaris" | "data", "detail": str} for the real (non-sample) pipeline."""
        try:
                rows = _zetaris().select(f"SELECT COUNT(*) AS n FROM {USL_TABLE}").rows
        except ZetarisError as exc:
                message = str(exc)
                if _MISSING.search(message):
                        return {"ready": False, "problem": "data",
                                "detail": f"Zetaris is reachable but the CareOps data is not set up: {SETUP_HINT}."}
                return {"ready": False, "problem": "zetaris", "detail": f"Cannot query Zetaris: {message}"}
        count = int(rows[0]["n"]) if rows else 0
        if count == 0:
                return {"ready": False, "problem": "data",
                        "detail": f"The USL table exists but returns no rows: {SETUP_HINT}."}
        return {"ready": True, "problem": None, "detail": f"{USL_TABLE} answers with {count} row(s)."}


def log_status() -> dict:
        status = check()
        if status["ready"]:
                log.info("Zetaris data ready: %s", status["detail"])
        else:
                log.warning("Zetaris data NOT ready: %s", status["detail"])
        return status
