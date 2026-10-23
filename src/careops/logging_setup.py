import logging
import warnings
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


def setup_logging() -> Path:
        """Send careops logs to the console and to logs/run-<timestamp>.log; keep library logs quiet."""
        LOG_DIR.mkdir(exist_ok=True)
        log_file = LOG_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.log"
        formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-21s | %(message)s", "%H:%M:%S")

        logger = logging.getLogger("careops")
        logger.setLevel(logging.INFO)
        logger.propagate = False  # don't duplicate into the root logger
        for handler in (logging.StreamHandler(), logging.FileHandler(log_file, encoding="utf-8")):
                handler.setFormatter(formatter)
                logger.addHandler(handler)

        # LangSmith prints this beta notice on every run; it is not about our code.
        warnings.filterwarnings("ignore", message=r".*langsmith\.address")
        return log_file


def short(text, limit: int = 160) -> str:
        """One-line, length-limited version of text for log messages."""
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[:limit] + "..."
