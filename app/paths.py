"""Where run artefacts go. CAREOPS_HOME (default: the project root) holds:

    logs/      process logs and per-run JSONL traces (trace-<run_id>.jsonl)
    .world/    one World Model register per run (<run_id>.json)
    reports/   one final report per run (<run_id>.json)

Read at call time, so tests and containers can point it elsewhere.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def home() -> Path:
        return Path(os.environ.get("CAREOPS_HOME") or PROJECT_ROOT)


def logs_dir() -> Path:
        return home() / "logs"


def reports_dir() -> Path:
        return home() / "reports"


def world_file(run_id: str) -> Path:
        """The register of a run; WorldModel(storage="local", directory=home()) writes exactly here."""
        return home() / ".world" / f"{run_id}.json"
