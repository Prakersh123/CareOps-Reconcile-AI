"""Entry point.

    python run.py                                   start the API + web UI on $HOST:$PORT (default 0.0.0.0:8000)
    python run.py "Check order L103"               run one investigation, print the JSON result
    python run.py --input input_examples/example_1.json   same, input in the POST /run format
    python run.py --sample "..."                    run in SAMPLE_MODE (no Zetaris, model or network)

Exit code for one-off runs: 0 success, 1 error. Logs go to logs/ and the console: stdout for the server,
stderr for one-off runs (so stdout carries only the JSON result).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
        parser = argparse.ArgumentParser(description="CareOps Reconcile AI")
        parser.add_argument("query", nargs="?", help="run one investigation with this request and print JSON")
        parser.add_argument("--input", help="JSON file in the POST /run format: {run_id?, input: {query}, options: {sample_mode}}")
        parser.add_argument("--sample", action="store_true", help="SAMPLE_MODE for this run")
        parser.add_argument("--run-id", help="run id to use (default: run-<timestamp>)")
        args = parser.parse_args()

        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
        from app.logging_setup import setup_logging
        one_off = bool(args.query or args.input)
        setup_logging(stream=sys.stderr if one_off else sys.stdout)  # one-off: stdout carries only the JSON

        if not one_off:
                import uvicorn
                uvicorn.run("app.api:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
                return 0

        from app.service import run_once
        body = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else {}
        query = args.query or body.get("input", {}).get("query")
        sample = True if args.sample else body.get("options", {}).get("sample_mode")
        result = run_once(query or "", run_id=args.run_id or body.get("run_id"), sample_mode=sample)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
        raise SystemExit(main())
