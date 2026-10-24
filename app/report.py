"""The final investigation report: what the CLI prints, the API returns and the UI shows.

Built from the run's final state and its World Model register, so every part of it
is traceable: each evidence item carries whether it matches what Zetaris returned,
and the attempt history shows what the critic caught and whether code or the LLM decided.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.engines.careops_world import RunWorld, ZetarisData
from app.agents.critic_checks import evidence_status
from app.paths import reports_dir


def _findings(text: str) -> list[dict] | None:
        try:
                parsed = json.loads(text)["findings"]
        except (ValueError, KeyError, TypeError):
                return None
        return [f for f in parsed if isinstance(f, dict)] if isinstance(parsed, list) else None


def _attempt_history(run_world: RunWorld) -> list[dict]:
        """Per attempt: the critic's verdict, its issues, and whether code checks or the LLM critic decided."""
        attempts: dict[str, dict] = {}
        for event in run_world.world.events():
                payload = event["payload"]
                if event["kind"] == "upsertContext" and payload["type"] == "attempt":
                        attempts.setdefault(payload["id"], {}).update(payload["attrs"])
        history = []
        for attrs in sorted(attempts.values(), key=lambda a: a.get("number", 0)):
                issues = attrs.get("issues", [])
                verdict = attrs.get("verdict")
                if verdict is None:
                        decided_by = None
                elif attrs.get("decided_by"):
                        decided_by = attrs["decided_by"]  # recorded by the orchestrator
                elif issues and all(str(i.get("check", "")).startswith("code:") for i in issues):
                        decided_by = "code"
                else:
                        decided_by = "llm"
                history.append({"number": attrs.get("number"), "verdict": verdict,
                                "decided_by": decided_by, "issues": issues})
        return history


def build_report(final: dict, run_world: RunWorld) -> dict:
        register = ZetarisData(run_world.world)
        findings = _findings(final.get("findings") or "")
        checked = []
        for finding in findings or []:
                evidence = []
                for item in finding.get("evidence") or []:
                        check, detail = evidence_status(finding.get("order_id"), item, register)
                        evidence.append({**item, "verified": check is None, "check": check, "detail": detail}
                                        if isinstance(item, dict) else {"raw": item, "verified": False, "check": check, "detail": detail})
                checked.append({**finding, "evidence": evidence})

        events = run_world.world.events()
        relationships = sorted(
                f"{e['payload']['from'].rsplit(':', 1)[-1]} {e['payload']['type']} {e['payload']['to'].rsplit(':', 1)[-1]}"
                for e in events if e["kind"] == "relate")
        return {
                "run_id": run_world.run_id,
                "request": final.get("request"),
                "status": final.get("status"),
                "attempts": final.get("attempt"),
                "stop_reason": final.get("stop_reason"),
                "sample_mode": bool(final.get("sample_mode")),
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "summary": {
                        "findings": len(checked),
                        "needs_human_review": sum(1 for f in checked if f.get("requires_human_review")),
                        "by_type": dict(Counter(f.get("finding_type") for f in checked)),
                        "evidence_items": sum(len(f["evidence"]) for f in checked),
                        "evidence_verified": sum(1 for f in checked for e in f["evidence"] if e["verified"]),
                },
                "findings": checked,
                "findings_unparsed": final.get("findings") if findings is None else None,
                "attempt_history": _attempt_history(run_world),
                "register": {
                        "file": str(run_world.file),
                        "events": len(events),
                        "audit_entries": len(run_world.world.audit()),
                        "orders_retrieved": sum(1 for e in register.attrs if e.startswith("order:")),
                        "invoices_retrieved": sum(1 for e in register.attrs if e.startswith("invoice:")),
                        "relationships": relationships,
                },
        }


def save_report(report: dict) -> Path:
        reports_dir().mkdir(parents=True, exist_ok=True)
        path = reports_dir() / f"{report['run_id']}.json"
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path


def load_report(run_id: str) -> dict | None:
        path = reports_dir() / f"{Path(run_id).name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def list_reports() -> list[dict]:
        """Newest first, with just enough to list runs."""
        out = []
        for path in sorted(reports_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                        report = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                        continue
                out.append({key: report.get(key) for key in ("run_id", "request", "status", "attempts", "generated_at")}
                           | {"summary": report.get("summary")})
        return out
