import logging
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path


class _RunIdFilter(logging.Filter):
        """Tag every line with the run in progress, e.g. run_id=run-20261024-120000 (or run_id=- outside a run)."""

        def filter(self, record: logging.LogRecord) -> bool:
                from app import progress
                record.run_id = progress.current_run_id() or "-"
                return True


def setup_logging(stream=None) -> Path:
        """Send careops logs to the console (stdout by default) and to logs/careops-<timestamp>.log.

        One-off runs that print JSON pass stream=sys.stderr so stdout stays machine-readable.
        Per-run JSONL traces are written separately (app.trace).
        """
        from app.paths import logs_dir
        logs_dir().mkdir(parents=True, exist_ok=True)
        log_file = logs_dir() / f"careops-{datetime.now():%Y%m%d-%H%M%S}.log"
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] run_id=%(run_id)s %(name)s | %(message)s", "%H:%M:%S")

        logger = logging.getLogger("careops")
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
        logger.propagate = False  # don't duplicate into the root logger
        for handler in (logging.StreamHandler(stream or sys.stdout), logging.FileHandler(log_file, encoding="utf-8")):
                handler.setFormatter(formatter)
                handler.addFilter(_RunIdFilter())
                logger.addHandler(handler)

        # LangSmith prints this beta notice on every run; it is not about our code.
        warnings.filterwarnings("ignore", message=r".*langsmith\.address")
        return log_file


def short(text, limit: int = 160) -> str:
        """One-line, length-limited version of text for log messages."""
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[:limit] + "..."
