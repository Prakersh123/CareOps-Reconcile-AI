"""Command line entry point.

    careops investigate                       run the default investigation
    careops investigate "Check order L103"    ask your own question
    careops investigate "..." --json          print the full report as JSON
    careops serve [--port 8765]               start the API + web UI
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

log = logging.getLogger("careops.cli")


def _print_report(report: dict, log_file) -> None:
        summary = report["summary"]
        print(f"\n{report['status']} after {report['attempts']} attempt(s)  ·  run {report['run_id']}")
        print(f"Request: {report['request']}\n")
        for finding in report["findings"]:
                verified = sum(1 for e in finding["evidence"] if e["verified"])
                review = "HUMAN REVIEW" if finding.get("requires_human_review") else "no review"
                print(f"  {finding.get('order_id')}: {finding.get('finding_type')}  "
                      f"[{finding.get('confidence')} confidence, {review}, evidence {verified}/{len(finding['evidence'])} verified]")
                print(f"      {finding.get('explanation')}")
                print(f"      → {finding.get('recommended_action')}")
        if report.get("findings_unparsed"):
                print("  (findings were not valid JSON)\n  " + report["findings_unparsed"][:500])
        print("\nCritic history:")
        for attempt in report["attempt_history"]:
                print(f"  attempt {attempt['number']}: {attempt['verdict']} (decided by {attempt['decided_by']})")
                for issue in attempt["issues"]:
                        print(f"      - [{issue.get('check')}] {issue.get('order_id')}: {issue.get('problem')}")
        print(f"\n{summary['findings']} finding(s), {summary['needs_human_review']} need human review, "
              f"{summary['evidence_verified']}/{summary['evidence_items']} evidence items verified against Zetaris")
        print(f"Report: reports/{report['run_id']}.json  ·  Register: {report['register']['file']}  ·  Log: {log_file}")


def main(argv: list[str] | None = None) -> int:
        parser = argparse.ArgumentParser(prog="careops", description="CareOps Reconcile AI")
        sub = parser.add_subparsers(dest="command", required=True)
        investigate = sub.add_parser("investigate", help="run an investigation")
        investigate.add_argument("request", nargs="?", help="your question (default: the standard reconciliation request)")
        investigate.add_argument("--json", action="store_true", help="print the full report as JSON")
        serve = sub.add_parser("serve", help="start the API and web UI")
        serve.add_argument("--host", default="127.0.0.1")
        serve.add_argument("--port", type=int, default=8765)
        args = parser.parse_args(argv)

        from app.logging_setup import setup_logging
        log_file = setup_logging()

        if args.command == "serve":
                import uvicorn
                uvicorn.run("app.api:app", host=args.host, port=args.port)
                return 0

        from app.orchestrator import DEFAULT_REQUEST, run_investigation
        try:
                final = run_investigation(args.request or DEFAULT_REQUEST)
        except Exception:
                log.exception("run failed")
                print(f"\nRun failed. Full log: {log_file}", file=sys.stderr)
                return 1
        if args.json:
                print(json.dumps(final["report"], indent=2, ensure_ascii=False))
        else:
                _print_report(final["report"], log_file)
        return 0 if final["status"] == "VERIFIED" else 2


if __name__ == "__main__":
        raise SystemExit(main())
