"""Meterless World Model engine, Python port.

Ported from the Meterless World Model reference implementation
(github.com/Meterless/Meterless, engines/world-model/reference, Apache-2.0),
following the engine spec in engines/world-model/AGENTS.md.

Event-sourced and deterministic: state is a fold over an append-only event log,
views are projections of that fold, every write must carry provenance, and every
mutation writes an audit entry. Zero dependencies beyond the standard library.

Differences from the TypeScript reference:
- Synchronous API (no promises); a lock keeps the single-writer discipline.
- assert_fact applies the conflict-policy winner inside the event fold, so
  point-in-time (snapshot) queries see fact values. The reference mutates the
  entity outside the log, so its snapshot view misses them.
- Python naming (snake_case) for functions, arguments and record keys.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 1
EPOCH_ISO = "1970-01-01T00:00:00.000Z"

# Merge bands (reference ruling): >= 0.85 auto-merge (0.85 exactly merges),
# [0.82, 0.85) operator review (0.82 exactly queues), below that a new entity.
AUTO_MERGE_AT = 0.85
REVIEW_LOWER = 0.82
# Ingest clustering gate: entity-overlap Jaccard >= 0.5 co-clusters observations.
CLUSTER_JACCARD = 0.5
HOUR_MS = 60 * 60 * 1000


class WorldModelError(Exception):
        pass


# ---------------------------------------------------------------- time helpers

def to_iso(value: Any) -> str:
        """ISO-8601 UTC with milliseconds and a Z suffix, like JavaScript's toISOString()."""
        if isinstance(value, datetime):
                if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return str(value)


def _iso_to_ms(iso: str) -> int:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)


def _ms_to_iso(ms: int) -> str:
        return to_iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def _iso_days_before(iso: str, days: int) -> str:
        return _ms_to_iso(_iso_to_ms(iso) - int(timedelta(days=days).total_seconds() * 1000))


# ------------------------------------------------------------------ stable ids

def sha256_hex(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
        """Deterministic JSON: sorted keys, no whitespace. The idempotency diff depends on it."""
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


_COMBINING_MARKS = re.compile("[̀-ͯ]")
_ZERO_WIDTH = re.compile("[​-‍﻿]")


def normalize_name(name: str) -> str:
        """NFKD -> strip combining marks -> strip zero-width chars -> lowercase -> collapse whitespace -> trim.

        "José  García" (accents, non-breaking space) and "jose garcia" normalize identically,
        but "Acme Corp" and "Acme Corporation" stay different: nothing is abbreviated or stemmed.
        """
        text = unicodedata.normalize("NFKD", name)
        text = _COMBINING_MARKS.sub("", text)
        text = _ZERO_WIDTH.sub("", text).lower()
        return re.sub(r"\s+", " ", text).strip()


def external_id(type_: str, system: str, id_: str) -> str:
        """Externally keyed entity or context: the literal grammar type:system:externalId."""
        return f"{type_}:{system}:{id_}"


def entity_id_from_name(type_: str, name: str) -> str:
        """Name-keyed entity: ent_ + first 16 hex of sha256("type:normalized name"). Type is part of the key."""
        return "ent_" + sha256_hex(f"{type_}:{normalize_name(name)}")[:16]


def context_id_from_attrs(type_: str, attrs: dict, parent: str | None = None) -> str:
        """Attr-keyed context, hashed WITH its parent: week:14 under season:145 differs from under season:144."""
        return "ctx_" + sha256_hex(f"{type_}:{canonical_json(attrs)}:{parent or ''}")[:16]


def relationship_id(from_: str, type_: str, to: str, context: str | None = None, valid_from: str | None = None) -> str:
        """Content-addressed: re-adding the same edge is a no-op."""
        return "rel_" + sha256_hex(f"{from_}|{type_}|{to}|{context or ''}|{valid_from or ''}")[:16]


def fact_id(about: str, predicate: str, value: Any, asserted_at: str, source_kind: str, source_url: str | None = None) -> str:
        """Content-addressed: asserting the identical fact twice is a no-op."""
        return "fact_" + sha256_hex(
                f"{about}|{predicate}|{canonical_json(value)}|{asserted_at}|{source_kind}|{source_url or ''}"
        )[:16]


# ------------------------------------------------------------------ resolution

def levenshtein(a: str, b: str) -> int:
        if a == b:
                return 0
        if not a:
                return len(b)
        if not b:
                return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, start=1):
                curr = [i] + [0] * len(b)
                for j, cb in enumerate(b, start=1):
                        curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb))
                prev = curr
        return prev[-1]


def name_similarity(raw_a: str, raw_b: str) -> float:
        """1 - levenshtein / max length, on NORMALIZED names."""
        a, b = normalize_name(raw_a), normalize_name(raw_b)
        max_len = max(len(a), len(b))
        if max_len == 0:
                return 1.0
        return 1 - levenshtein(a, b) / max_len


def decide(similarity: float, auto_merge_at: float = AUTO_MERGE_AT, review_lower: float = REVIEW_LOWER) -> str:
        if similarity >= auto_merge_at:
                return "auto-merge"
        if similarity >= review_lower:
                return "review"
        return "new"


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
        sa, sb = set(a), set(b)
        if not sa or not sb:
                return 0.0
        inter = len(sa & sb)
        return inter / (len(sa) + len(sb) - inter)


# ----------------------------------------------------------------- event store

class EventStore:
        """Append-only event log plus the state folded from it. Optional JSON persistence per namespace."""

        def __init__(self, namespace: str, storage: str = "memory", directory: str | Path | None = None,
                     conflict_policy: str = "most-recent-wins", source_priority: list[str] | None = None):
                self.events: list[dict] = []
                self.audit: list[dict] = []
                self.observations: dict[str, dict] = {}
                self.stories: dict[str, dict] = {}
                self.scores: list[dict] = []
                self.merge_queue: list[dict] = []
                self.entities: dict[str, dict] = {}
                self.contexts: dict[str, dict] = {}
                self.relationships: dict[str, dict] = {}
                self.facts: dict[str, dict] = {}
                self.aliases: dict[str, str] = {}  # alias id -> primary id
                self.conflict_policy = conflict_policy
                self.source_priority = source_priority or []
                self._seq = 0
                self._audit_seq = 0
                self._subscribers: list[Callable[[dict], None]] = []
                self._file: Path | None = None

                if storage == "local":
                        folder = Path(directory or Path.cwd()) / ".world"
                        folder.mkdir(parents=True, exist_ok=True)
                        self._file = folder / f"{namespace}.json"
                        if self._file.exists():
                                saved = json.loads(self._file.read_text(encoding="utf-8"))
                                self.audit = saved.get("audit", [])
                                self._audit_seq = self.audit[-1]["seq"] if self.audit else 0
                                self.observations = dict(saved.get("observations", []))
                                self.stories = dict(saved.get("stories", []))
                                self.scores = saved.get("scores", [])
                                self.merge_queue = saved.get("merge_queue", [])
                                for event in saved.get("events", []):
                                        self._apply(event, is_new=False)

        def append(self, kind: str, payload: dict, at: str) -> dict:
                event = {"id": f"evt_{self._seq + 1}", "seq": self._seq + 1, "kind": kind,
                         "payload": copy.deepcopy(payload), "schema_version": SCHEMA_VERSION, "at": at}
                self._apply(event, is_new=True)
                return event

        def _apply(self, event: dict, is_new: bool) -> None:
                self._seq = max(self._seq, event["seq"])
                self.events.append(event)
                self._fold(event)
                if is_new:
                        self.persist()
                        for subscriber in self._subscribers:
                                subscriber(event)

        def _fold(self, event: dict) -> None:
                kind, payload = event["kind"], event["payload"]
                if kind == "upsertEntity":
                        existing = self.entities.get(payload["id"])
                        if existing:
                                existing["attrs"] = {**existing["attrs"], **payload["attrs"]}
                                existing["updated_at"] = payload["updated_at"]
                                existing["source"] = payload["source"]
                                name = payload.get("name")
                                if name:
                                        if name not in existing["aliases"]:
                                                existing["aliases"].append(name)
                                        existing["name"] = name
                        else:
                                self.entities[payload["id"]] = copy.deepcopy(payload)
                elif kind == "upsertContext":
                        existing = self.contexts.get(payload["id"])
                        if existing:
                                existing["attrs"] = {**existing["attrs"], **payload["attrs"]}
                                existing["updated_at"] = payload["updated_at"]
                        else:
                                self.contexts[payload["id"]] = copy.deepcopy(payload)
                elif kind == "relate":
                        # Supersession: a new open edge with the same (from, type, context) and a
                        # different target CLOSES the previous open edge. Nothing is deleted.
                        for other in self.relationships.values():
                                if (other["from"] == payload["from"] and other["type"] == payload["type"]
                                                and other.get("context", "") == payload.get("context", "")
                                                and other["to"] != payload["to"]
                                                and "valid_to" not in other and "superseded_by" not in other):
                                        other["valid_to"] = payload.get("valid_from") or payload["created_at"]
                                        other["superseded_by"] = payload["id"]
                        self.relationships.setdefault(payload["id"], copy.deepcopy(payload))
                elif kind == "assertFact":
                        self._fold_fact(payload)
                elif kind == "alias":
                        self.aliases[payload["alias_id"]] = payload["primary_id"]
                # snapshot / tombstone / view-rebuild carry no folded state

        def _fold_fact(self, payload: dict) -> None:
                if payload["id"] in self.facts:
                        return
                rivals = [f for f in self.facts.values()
                          if f["about"] == payload["about"] and f["predicate"] == payload["predicate"]
                          and "superseded_by" not in f]
                fact = copy.deepcopy(payload)
                self.facts[fact["id"]] = fact
                winner = fact
                for rival in rivals:
                        if self._rival_wins(rival, winner):
                                winner = rival
                for f in [fact, *rivals]:
                        if f["id"] != winner["id"]:
                                f["superseded_by"] = winner["id"]
                entity = self.entities.get(fact["about"])
                if entity is not None:
                        entity["attrs"][fact["predicate"]] = winner["value"]
                        entity["updated_at"] = fact["asserted_at"]

        def _rival_wins(self, rival: dict, winner: dict) -> bool:
                if self.conflict_policy == "highest-confidence-wins":
                        return rival["confidence"] > winner["confidence"]
                if self.conflict_policy == "source-priority":
                        return self._priority(rival["source"]["kind"]) < self._priority(winner["source"]["kind"])
                return rival["asserted_at"] > winner["asserted_at"]  # most-recent-wins

        def _priority(self, kind: str) -> float:
                return self.source_priority.index(kind) if kind in self.source_priority else float("inf")

        def fold_until(self, at: str) -> EventStore:
                """Replay the log up to and including a timestamp: the snapshot view's state."""
                scratch = EventStore("scratch", "memory", conflict_policy=self.conflict_policy,
                                     source_priority=self.source_priority)
                for event in self.events:
                        if event["at"] <= at:
                                scratch._fold(event)
                return scratch

        def resolve_alias(self, id_: str) -> str:
                current, seen = id_, set()
                while current in self.aliases and current not in seen:
                        seen.add(current)
                        current = self.aliases[current]
                return current

        def log_audit(self, kind: str, actor: str, summary: str, event_id: str | None = None,
                      run_id: str | None = None, at: str | None = None) -> None:
                self._audit_seq += 1
                entry = {"seq": self._audit_seq, "at": at or EPOCH_ISO, "kind": kind, "actor": actor, "summary": summary}
                if event_id:
                        entry["event_id"] = event_id
                if run_id:
                        entry["run_id"] = run_id
                self.audit.append(entry)
                self.persist()

        def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
                self._subscribers.append(fn)
                return lambda: self._subscribers.remove(fn)

        def persist(self) -> None:
                if not self._file:
                        return
                data = {
                        "events": self.events,
                        "audit": self.audit,
                        "observations": list(self.observations.items()),
                        "stories": list(self.stories.items()),
                        "scores": self.scores,
                        "merge_queue": self.merge_queue,
                }
                self._file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ------------------------------------------------------------- ingest pipeline

def world_state_shape(store: EventStore) -> dict:
        """The idempotency diff surface: everything derived from observations, in canonical order."""
        by_id = lambda records: sorted(records, key=lambda r: r["id"])
        return {
                "entities": by_id(store.entities.values()),
                "contexts": by_id(store.contexts.values()),
                "relationships": by_id(store.relationships.values()),
                "facts": by_id(store.facts.values()),
                "observations": sorted(store.observations.values(), key=lambda o: o["external_id"]),
                "stories": by_id(store.stories.values()),
        }


def run_ingest_pipeline(store: EventStore, observations: list[dict],
                        upsert: Callable[[dict], tuple[str, list[str]]]) -> dict:
        """The canonical ingest pipeline (AGENTS.md section 4.1). Re-running the same batch is an empty diff.

        Every derived timestamp comes from the OBSERVATION, never the wall clock.
        """
        run_id = "run_" + sha256_hex(canonical_json([o["external_id"] for o in observations]))[:12]
        timings: list[dict] = []
        before = canonical_json(world_state_shape(store))
        events_before = len(store.events)

        def timed(step: int, name: str, fn: Callable[[], Any]) -> Any:
                started = time.perf_counter()
                out = fn()
                timings.append({"step": step, "name": name, "ms": (time.perf_counter() - started) * 1000})
                return out

        # 1. Reconcile: recompute staleness from observation times.
        def reconcile() -> None:
                latest = max((o["at"] for o in observations), default="")
                for record in store.observations.values():
                        record["stale"] = latest != "" and record["at"] < _iso_days_before(latest, 30)
        timed(1, "reconcile-stale", reconcile)

        # 2. Classify: unchanged observations (same checksum) are skipped.
        fresh: list[tuple[dict, str]] = []
        skipped: list[dict] = []

        def classify() -> None:
                for obs in observations:
                        checksum = sha256_hex(canonical_json({"externalId": obs["external_id"], "title": obs["title"],
                                                              "text": obs.get("text") or "", "at": obs["at"]}))
                        existing = store.observations.get(obs["external_id"])
                        if existing and existing["checksum"] == checksum:
                                skipped.append(obs)
                        else:
                                fresh.append((obs, checksum))
        timed(2, "cluster-classify", classify)

        # 3. Persist primary aggregates: observation entities + mentioned entities.
        def persist_aggregates() -> None:
                for obs, checksum in fresh:
                        entity_id, entity_ids = upsert(obs)
                        store.observations[obs["external_id"]] = {
                                "id": "obs_" + sha256_hex(obs["external_id"])[:12],
                                "external_id": obs["external_id"],
                                "entity_id": entity_id,
                                "checksum": checksum,
                                "at": obs["at"],
                                "entity_ids": entity_ids,
                                "stale": False,
                        }
        timed(3, "persist-aggregates", persist_aggregates)

        # Group all observations into stories by entity overlap.
        clusters: list[list[dict]] = []

        def group_stories() -> None:
                everything = sorted(store.observations.values(), key=lambda o: o["external_id"])
                used: set[str] = set()
                for seed in everything:
                        if seed["external_id"] in used:
                                continue
                        group = [seed]
                        used.add(seed["external_id"])
                        for other in everything:
                                if other["external_id"] in used:
                                        continue
                                if jaccard(seed.get("entity_ids", []), other.get("entity_ids", [])) >= CLUSTER_JACCARD:
                                        group.append(other)
                                        used.add(other["external_id"])
                        clusters.append(group)
        timed(2, "story-grouping", group_stories)

        # 4. Score (bucketed velocity), 5. infer milestones, 8. patch source items with story ids.
        def score() -> None:
                for group in clusters:
                        member_ids = sorted(g["external_id"] for g in group)
                        story_id = "story_" + sha256_hex(canonical_json(member_ids))[:12]
                        times = sorted(_iso_to_ms(g["at"]) for g in group)
                        last = times[-1]

                        def velocity(window_ms: int) -> float:
                                return sum(1 for t in times if t > last - window_ms) / (window_ms / HOUR_MS)

                        prev_hour = sum(1 for t in times if last - 2 * HOUR_MS < t <= last - HOUR_MS)
                        curr_hour = sum(1 for t in times if t > last - HOUR_MS)
                        store.scores = [s for s in store.scores if s["story_id"] != story_id]
                        store.scores.append({
                                "story_id": story_id,
                                "at": _ms_to_iso(last),
                                "velocity_1h": velocity(HOUR_MS),
                                "velocity_24h": velocity(24 * HOUR_MS),
                                "velocity_7d": velocity(7 * 24 * HOUR_MS),
                                "trend_direction": (curr_hour > prev_hour) - (curr_hour < prev_hour),
                        })
                        entity_ids = sorted({e for g in group for e in g.get("entity_ids", [])})
                        store.stories[story_id] = {
                                "id": story_id,
                                "title": group[0]["external_id"],
                                "member_ids": member_ids,
                                "entity_names": list(entity_ids),
                                "entity_ids": entity_ids,
                                "sources": [],
                                "first_at": _ms_to_iso(times[0]),
                                "last_at": _ms_to_iso(last),
                                "milestones": [{"kind": "multi-source", "at": _ms_to_iso(times[1])}] if len(group) >= 2 else [],
                        }
                        for record in group:
                                record["story_id"] = story_id
        timed(4, "score-measure", score)

        # 9. Rebuild aggregates: mention counts and first/last seen per entity.
        def rebuild_aggregates() -> None:
                for entity in store.entities.values():
                        mentions = [o for o in store.observations.values() if entity["id"] in o.get("entity_ids", [])]
                        if not mentions:
                                continue
                        seen = sorted(m["at"] for m in mentions)
                        entity["aggregates"] = {
                                "mention_count": len(mentions),
                                "first_seen_at": seen[0],
                                "last_seen_at": seen[-1],
                                "linked_story_ids": sorted({m["story_id"] for m in mentions if m.get("story_id")}),
                        }
        timed(9, "rebuild-aggregates", rebuild_aggregates)

        store.persist()
        report = {
                "run_id": run_id,
                "processed": len(fresh),
                "skipped_unchanged": len(skipped),
                "clusters": len(clusters),
                "events_appended": len(store.events) - events_before,
                "changed": before != canonical_json(world_state_shape(store)),
                "step_timings_ms": timings,  # reported only, never persisted into state
        }
        store.log_audit("ingest", "pipeline", f"run {run_id}: {len(fresh)} processed, {len(skipped)} skipped",
                        run_id=run_id, at=observations[0]["at"] if observations else None)
        return report


# ------------------------------------------------------------------ the facade

class WorldModel:
        """World Model facade: provenanced writes, an audit entry per mutation, and read views."""

        def __init__(self, namespace: str, storage: str = "memory", directory: str | Path | None = None,
                     clock: Callable[[], datetime] | None = None, auto_merge_at: float = AUTO_MERGE_AT,
                     review_lower: float = REVIEW_LOWER, conflict_policy: str = "most-recent-wins",
                     source_priority: list[str] | None = None):
                if not namespace:
                        raise WorldModelError("WorldModel: namespace is required")
                # The clock is used ONLY for genuinely-now operator actions (merge approval).
                # Record timestamps always come from provenance, which keeps ingest idempotent.
                self.clock = clock or (lambda: datetime.now(timezone.utc))
                self.auto_merge_at = auto_merge_at
                self.review_lower = review_lower
                self.conflict_policy = conflict_policy
                self._store = EventStore(namespace, storage, directory, conflict_policy, source_priority)
                self._lock = threading.RLock()  # single writer
                self._custom_views: dict[str, dict] = {}

        # ---------------------------------------------------------- writes

        @staticmethod
        def _provenance(source: dict | None) -> dict:
                if not source or not source.get("kind") or source.get("at") is None:
                        raise WorldModelError("World Model: every write carries provenance (kind + at). No provenance, no write.")
                return {**{k: v for k, v in source.items() if v is not None}, "at": to_iso(source["at"])}

        def upsert_entity(self, *, type: str, source: dict | None, name: str | None = None,
                          external_key: tuple[str, str] | None = None, attrs: dict | None = None,
                          state: str = "active") -> dict:
                with self._lock:
                        provenance = self._provenance(source)
                        name = name or (attrs or {}).get("name")
                        if external_key:
                                id_ = external_id(type, *external_key)
                        elif name:
                                id_ = entity_id_from_name(type, name)
                        else:
                                raise WorldModelError("World Model: name-keyed entities need a name (or provide an external_key)")

                        # Resolution pass for name-keyed entities: near-duplicates of the same type
                        # auto-merge (alias event) or queue for operator review.
                        if not external_key and name:
                                for other in list(self._store.entities.values()):
                                        if other["type"] != type or other["id"] == id_ or not other.get("name"):
                                                continue
                                        similarity = name_similarity(name, other["name"])
                                        decision = decide(similarity, self.auto_merge_at, self.review_lower)
                                        if decision == "auto-merge":
                                                self._store.append("alias", {"alias_id": id_, "primary_id": other["id"],
                                                                             "merged_at": provenance["at"], "merged_by": "resolver",
                                                                             "reason": f"similarity {similarity:.3f}"}, provenance["at"])
                                                self._store.log_audit("auto-merge", "resolver",
                                                                      f"{name} -> {other['name']} ({similarity:.3f})", at=provenance["at"])
                                                return self._fold_entity_upsert(other["id"], type, attrs, provenance, name, state)
                                        if decision == "review" and not any(
                                                        {p["left_id"], p["right_id"]} == {id_, other["id"]} for p in self._store.merge_queue):
                                                self._store.merge_queue.append({
                                                        "id": f"merge_{len(self._store.merge_queue) + 1}",
                                                        "left_id": id_, "right_id": other["id"],
                                                        "left_name": name, "right_name": other["name"],
                                                        "similarity": similarity,
                                                        "reason": "name similarity in review band [0.82, 0.85)",
                                                        "status": "pending", "proposed_at": provenance["at"],
                                                })
                                                self._store.log_audit("merge-proposed", "resolver",
                                                                      f"{name} ~ {other['name']} ({similarity:.3f}) queued",
                                                                      at=provenance["at"])
                        return self._fold_entity_upsert(id_, type, attrs, provenance, name, state)

        def _fold_entity_upsert(self, id_: str, type_: str, attrs: dict | None, provenance: dict,
                                name: str | None, state: str = "active") -> dict:
                resolved = self._store.resolve_alias(id_)
                entity = {
                        "type": type_, "id": resolved, "attrs": dict(attrs or {}),
                        "schema_version": SCHEMA_VERSION, "source": provenance,
                        "created_at": provenance["at"], "updated_at": provenance["at"],
                        "aliases": [name] if name else [], "state": state,
                }
                if name:
                        entity["name"] = name
                event = self._store.append("upsertEntity", entity, provenance["at"])
                self._store.log_audit("upsertEntity", provenance.get("by", "system"), f"{resolved} ({type_})",
                                      event["id"], provenance.get("run_id"), provenance["at"])
                return copy.deepcopy(self._store.entities[resolved])

        def upsert_context(self, *, type: str, source: dict | None, external_key: tuple[str, str] | None = None,
                           attrs: dict | None = None, parent: str | None = None) -> dict:
                with self._lock:
                        provenance = self._provenance(source)
                        id_ = external_id(type, *external_key) if external_key else context_id_from_attrs(type, attrs or {}, parent)
                        context = {
                                "type": type, "id": id_, "attrs": dict(attrs or {}),
                                "schema_version": SCHEMA_VERSION, "source": provenance,
                                "created_at": provenance["at"], "updated_at": provenance["at"],
                        }
                        if parent:
                                context["parent"] = parent
                        event = self._store.append("upsertContext", context, provenance["at"])
                        self._store.log_audit("upsertContext", provenance.get("by", "system"), f"{id_} ({type})",
                                              event["id"], provenance.get("run_id"), provenance["at"])
                        return copy.deepcopy(self._store.contexts[id_])

        def relate(self, *, from_: str, type: str, to: str, source: dict | None, context: str | None = None,
                   attrs: dict | None = None, valid_from: str | None = None, valid_to: str | None = None) -> dict:
                with self._lock:
                        provenance = self._provenance(source)
                        frm, dst = self._store.resolve_alias(from_), self._store.resolve_alias(to)
                        id_ = relationship_id(frm, type, dst, context, valid_from)
                        if id_ in self._store.relationships:
                                return copy.deepcopy(self._store.relationships[id_])  # content-addressed no-op
                        rel = {
                                "id": id_, "from": frm, "type": type, "to": dst,
                                "valid_from": valid_from or provenance["at"],
                                "schema_version": SCHEMA_VERSION, "source": provenance, "created_at": provenance["at"],
                        }
                        if context:
                                rel["context"] = context
                        if attrs:
                                rel["attrs"] = attrs
                        if valid_to:
                                rel["valid_to"] = valid_to
                        event = self._store.append("relate", rel, provenance["at"])
                        self._store.log_audit("relate", provenance.get("by", "system"), f"{frm} -[{type}]-> {dst}",
                                              event["id"], provenance.get("run_id"), provenance["at"])
                        return copy.deepcopy(self._store.relationships[id_])

        def assert_fact(self, *, about: str, predicate: str, value: Any, source: dict | None,
                        confidence: float = 0.5, context: str | None = None) -> dict:
                """Record a claim about an entity. The conflict policy decides which value lands on the entity."""
                with self._lock:
                        provenance = self._provenance(source)
                        subject = self._store.resolve_alias(about)
                        id_ = fact_id(subject, predicate, value, provenance["at"], provenance["kind"], provenance.get("url"))
                        if id_ in self._store.facts:
                                return copy.deepcopy(self._store.facts[id_])
                        if subject not in self._store.entities:
                                inferred = subject.split(":")[0] if ":" in subject else "entity"
                                self._store.append("upsertEntity", {
                                        "type": inferred, "id": subject, "attrs": {}, "schema_version": SCHEMA_VERSION,
                                        "source": provenance, "created_at": provenance["at"], "updated_at": provenance["at"],
                                        "aliases": [], "state": "emerging",
                                }, provenance["at"])
                        fact = {
                                "id": id_, "about": subject, "predicate": predicate, "value": value,
                                "confidence": confidence, "source": provenance, "asserted_at": provenance["at"],
                        }
                        if context:
                                fact["context"] = context
                        event = self._store.append("assertFact", fact, provenance["at"])
                        winning_value = self._store.entities[subject]["attrs"][predicate]
                        self._store.log_audit("assertFact", provenance.get("by", "system"),
                                              f"{subject}.{predicate} = {json.dumps(winning_value, default=str)} ({self.conflict_policy})",
                                              event["id"], provenance.get("run_id"), provenance["at"])
                        return copy.deepcopy(self._store.facts[id_])

        def ingest(self, observations: list[dict]) -> dict:
                """Batch ingest. Each observation: external_id, title, at, source, optional type, text, entities."""
                with self._lock:
                        def upsert(obs: dict) -> tuple[str, list[str]]:
                                provenance = self._provenance({**obs["source"], "at": obs["at"]})
                                obs_type = obs.get("type") or "item"
                                attrs = {"title": obs["title"]}
                                if obs.get("text") is not None:
                                        attrs["text"] = obs["text"]
                                entity = self._fold_entity_upsert(external_id(obs_type, "ingest", obs["external_id"]),
                                                                  obs_type, attrs, provenance, obs["title"])
                                entity_ids = []
                                for mention in obs.get("entities") or []:
                                        mid = entity_id_from_name(mention["type"], mention["name"])
                                        self._fold_entity_upsert(mid, mention["type"], None, provenance, mention["name"])
                                        target = self._store.resolve_alias(mid)
                                        entity_ids.append(target)
                                        rid = relationship_id(entity["id"], "mentions", target)
                                        if rid not in self._store.relationships:
                                                self._store.append("relate", {
                                                        "id": rid, "from": entity["id"], "type": "mentions", "to": target,
                                                        "valid_from": provenance["at"], "schema_version": SCHEMA_VERSION,
                                                        "source": provenance, "created_at": provenance["at"],
                                                }, provenance["at"])
                                return entity["id"], sorted(entity_ids)

                        return run_ingest_pipeline(self._store, observations, upsert)

        # ----------------------------------------------------------- reads

        def view(self, name: str):
                if name == "graph":
                        return GraphView(self._store)
                if name == "facts":
                        return FactsView(self._store)
                if name == "snapshot":
                        return SnapshotView(self._store)
                if name == "timeline":
                        return TimelineView(self._store)
                if name in self._custom_views:
                        return _CustomView(self._store, self._custom_views[name])
                raise WorldModelError(f'World Model: unknown view "{name}"')

        def register_view(self, name: str, *, events: list[str], initial_state: Any,
                          reducer: Callable[[Any, dict], Any]) -> None:
                self._custom_views[name] = {"events": events, "initial_state": initial_state, "reducer": reducer}

        # -------------------------------------------------------- operator

        def merge_queue(self) -> list[dict]:
                return copy.deepcopy(self._store.merge_queue)

        def approve_merge(self, proposal_id: str, by: str) -> None:
                with self._lock:
                        proposal = self._pending_proposal(proposal_id)
                        at = to_iso(self.clock())
                        proposal.update(status="approved", resolved_at=at, resolved_by=by)
                        self._store.append("alias", {"alias_id": proposal["left_id"], "primary_id": proposal["right_id"],
                                                     "merged_at": at, "merged_by": by, "reason": proposal["reason"]}, at)
                        self._store.log_audit("merge-approved", by, f"{proposal['left_name']} -> {proposal['right_name']}", at=at)

        def reject_merge(self, proposal_id: str, by: str) -> None:
                with self._lock:
                        proposal = self._pending_proposal(proposal_id)
                        at = to_iso(self.clock())
                        proposal.update(status="rejected", resolved_at=at, resolved_by=by)
                        self._store.log_audit("merge-rejected", by,
                                              f"{proposal['left_name']} stays separate from {proposal['right_name']}", at=at)

        def _pending_proposal(self, proposal_id: str) -> dict:
                for proposal in self._store.merge_queue:
                        if proposal["id"] == proposal_id and proposal["status"] == "pending":
                                return proposal
                raise WorldModelError(f"World Model: no pending merge proposal {proposal_id}")

        def audit(self) -> list[dict]:
                return copy.deepcopy(self._store.audit)

        def events(self) -> list[dict]:
                return copy.deepcopy(self._store.events)

        def state_shape(self) -> dict:
                return copy.deepcopy(world_state_shape(self._store))

        def state_shape_canonical(self) -> str:
                return canonical_json(world_state_shape(self._store))

        def subscribe_events(self, fn: Callable[[dict], None]) -> Callable[[], None]:
                return self._store.subscribe(fn)


# ----------------------------------------------------------------------- views

def _valid_at(rel: dict, at: str) -> bool:
        if rel.get("valid_from") and at < rel["valid_from"]:
                return False
        if rel.get("valid_to") and at >= rel["valid_to"]:
                return False
        return True


class GraphView:
        def __init__(self, store: EventStore):
                self._store = store

        def entity(self, id_: str) -> dict | None:
                entity = self._store.entities.get(self._store.resolve_alias(id_))
                return copy.deepcopy(entity) if entity else None

        def neighbors_of(self, id_: str, via: str | None = None, in_context: str | None = None,
                         at: str | None = None) -> list[dict]:
                rid = self._store.resolve_alias(id_)
                out = []
                for rel in self._store.relationships.values():
                        if via and rel["type"] != via:
                                continue
                        if in_context and rel.get("context") != in_context:
                                continue
                        if at and not _valid_at(rel, at):
                                continue
                        if not at and rel.get("valid_to"):
                                continue  # closed edges are excluded from "now" queries
                        other = rel["to"] if rel["from"] == rid else rel["from"] if rel["to"] == rid else None
                        if other and other in self._store.entities:
                                out.append(copy.deepcopy(self._store.entities[other]))
                return sorted(out, key=lambda e: e["id"])

        def incoming(self, id_: str, via: str | None = None) -> list[dict]:
                rid = self._store.resolve_alias(id_)
                out = [copy.deepcopy(self._store.entities[rel["from"]])
                       for rel in self._store.relationships.values()
                       if rel["to"] == rid and (not via or rel["type"] == via) and not rel.get("valid_to")
                       and rel["from"] in self._store.entities]
                return sorted(out, key=lambda e: e["id"])

        def entities_in_context(self, context_id: str, type: str | None = None) -> list[dict]:
                seen: dict[str, dict] = {}
                for rel in self._store.relationships.values():
                        if rel.get("context") != context_id:
                                continue
                        for id_ in (rel["from"], rel["to"]):
                                entity = self._store.entities.get(id_)
                                if entity and (not type or entity["type"] == type):
                                        seen[entity["id"]] = copy.deepcopy(entity)
                return sorted(seen.values(), key=lambda e: e["id"])

        def traverse(self, start_id: str, via: str, direction: str, max_depth: int = 5) -> list[dict]:
                out, visited = [], set()
                frontier = [self._store.resolve_alias(start_id)]
                for _ in range(max_depth):
                        if not frontier:
                                break
                        next_frontier = []
                        for id_ in frontier:
                                for rel in self._store.relationships.values():
                                        if rel["type"] != via or rel.get("valid_to"):
                                                continue
                                        if direction == "ancestors":
                                                target = rel["to"] if rel["from"] == id_ else None
                                        else:
                                                target = rel["from"] if rel["to"] == id_ else None
                                        if target and target not in visited:
                                                visited.add(target)
                                                if target in self._store.entities:
                                                        out.append(copy.deepcopy(self._store.entities[target]))
                                                next_frontier.append(target)
                        frontier = next_frontier
                return out

        def query(self, type: str | None = None, where: Callable[[dict], bool] | None = None) -> list[dict]:
                return sorted((copy.deepcopy(e) for e in self._store.entities.values()
                               if (not type or e["type"] == type) and (not where or where(e))),
                              key=lambda e: e["id"])


class FactsView:
        def __init__(self, store: EventStore):
                self._store = store

        def about(self, id_: str) -> list[dict]:
                rid = self._store.resolve_alias(id_)
                return sorted((copy.deepcopy(f) for f in self._store.facts.values() if f["about"] == rid),
                              key=lambda f: f["asserted_at"])


class SnapshotView:
        def __init__(self, store: EventStore):
                self._store = store

        def entity_at(self, id_: str, at: str) -> dict | None:
                """Entity state as of a timestamp (a bare YYYY-MM-DD means the end of that day)."""
                iso = f"{at}T23:59:59.999Z" if len(at) == 10 else at
                state = self._store.fold_until(iso)
                entity = state.entities.get(state.resolve_alias(id_))
                return copy.deepcopy(entity) if entity else None


class TimelineView:
        def __init__(self, store: EventStore):
                self._store = store

        def get(self) -> list[dict]:
                return copy.deepcopy(self._store.events)

        def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
                return self._store.subscribe(fn)


class _CustomView:
        def __init__(self, store: EventStore, spec: dict):
                self._store, self._spec = store, spec

        def get(self) -> Any:
                state = copy.deepcopy(self._spec["initial_state"])
                for event in self._store.events:
                        if event["kind"] in self._spec["events"]:
                                state = self._spec["reducer"](state, copy.deepcopy(event))
                return state
