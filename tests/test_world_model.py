"""World Model engine tests.

Part 1 ports the 8 checks of the official conformance runner
(Meterless engines/world-model/conformance/runner.ts) with the same ids, inputs
and assertions. The official runner can only load TypeScript modules, so it
cannot run against this Python implementation directly.

Part 2 ports the reference smoke tests (reference/test/smoke.test.ts).
Part 3 covers behaviour specific to this port.
"""

from datetime import datetime, timezone

import pytest

from app.engines.world_model import (
        WorldModel, WorldModelError, canonical_json, decide, entity_id_from_name, name_similarity, normalize_name,
)

CONFORMANCE_OBSERVATIONS = [
        {"external_id": "a1", "title": "Fed holds rates", "at": "2026-05-17T14:00:00.000Z",
         "source": {"kind": "rss", "at": "2026-05-17T14:00:00.000Z"}, "entities": [{"type": "person", "name": "Jerome Powell"}]},
        {"external_id": "a2", "title": "Powell signals patience", "at": "2026-05-17T16:30:00.000Z",
         "source": {"kind": "rss", "at": "2026-05-17T16:30:00.000Z"}, "entities": [{"type": "person", "name": "Jerome Powell"}]},
]


def fixed_clock():
        return datetime(2026, 5, 18, tzinfo=timezone.utc)


# ===================================================== Part 1: conformance checks

def test_sid_001_diacritics_and_nbsp_normalize_identically():
        assert normalize_name("José  García") == normalize_name("Jose Garcia")
        assert entity_id_from_name("person", "José García") == entity_id_from_name("person", "jose garcia")


def test_sid_002_distinct_names_stay_distinct():
        assert entity_id_from_name("org", "Acme Corp") != entity_id_from_name("org", "Acme Corporation")
        assert entity_id_from_name("person", "x") != entity_id_from_name("org", "x"), "type participates in the key"


def test_band_001_merge_bands_at_the_boundaries():
        assert decide(0.85) == "auto-merge"
        assert decide(0.84) == "review"
        assert decide(0.82) == "review"
        assert decide(0.81) == "new"


def test_prov_001_writes_without_provenance_are_rejected():
        world = WorldModel(storage="memory", namespace="conf")
        with pytest.raises(WorldModelError, match="provenance"):
                world.upsert_entity(type="person", name="No Provenance", source=None)


def test_idem_001_same_batch_twice_is_an_empty_diff():
        world = WorldModel(storage="memory", namespace="conf", clock=fixed_clock)
        world.ingest(CONFORMANCE_OBSERVATIONS)
        shape1 = world.state_shape_canonical()
        second = world.ingest(CONFORMANCE_OBSERVATIONS)
        assert second["changed"] is False
        assert world.state_shape_canonical() == shape1


def test_idem_002_a_mutated_batch_changes_the_shape():
        world = WorldModel(storage="memory", namespace="conf", clock=fixed_clock)
        world.ingest(CONFORMANCE_OBSERVATIONS)
        shape1 = world.state_shape_canonical()
        mutated = [{**CONFORMANCE_OBSERVATIONS[0], "title": CONFORMANCE_OBSERVATIONS[0]["title"] + " (edited)"},
                   CONFORMANCE_OBSERVATIONS[1]]
        run = world.ingest(mutated)
        assert run["changed"] is True
        assert world.state_shape_canonical() != shape1


def test_bitemp_001_superseding_edge_closes_old_one_and_point_in_time_queries_respect_validity():
        world = WorldModel(storage="memory", namespace="conf")
        at = lambda s: {"kind": "manual", "at": s}
        world.upsert_entity(type="player", external_key=("g", "p1"), attrs={}, source=at("2087-01-01T00:00:00.000Z"))
        world.upsert_entity(type="team", external_key=("g", "a"), attrs={}, source=at("2087-01-01T00:00:00.000Z"))
        world.upsert_entity(type="team", external_key=("g", "b"), attrs={}, source=at("2088-03-01T00:00:00.000Z"))
        world.relate(from_="player:g:p1", type="plays-for", to="team:g:a",
                     valid_from="2087-01-01T00:00:00.000Z", source=at("2087-01-01T00:00:00.000Z"))
        world.relate(from_="player:g:p1", type="plays-for", to="team:g:b",
                     valid_from="2088-03-01T00:00:00.000Z", source=at("2088-03-01T00:00:00.000Z"))
        now = world.view("graph").neighbors_of("player:g:p1", via="plays-for")
        assert [e["id"] for e in now] == ["team:g:b"]
        then = world.view("graph").neighbors_of("player:g:p1", via="plays-for", at="2087-06-01T00:00:00.000Z")
        assert [e["id"] for e in then] == ["team:g:a"]


def test_audit_001_every_mutation_writes_an_audit_entry_in_order():
        world = WorldModel(storage="memory", namespace="conf")
        world.upsert_entity(type="x", name="Audit One", source={"kind": "manual", "at": "2026-01-01T00:00:00.000Z"})
        world.upsert_entity(type="x", name="Audit Two", source={"kind": "manual", "at": "2026-01-02T00:00:00.000Z"})
        audit = world.audit()
        assert len(audit) >= 2
        assert all(audit[i]["seq"] > audit[i - 1]["seq"] for i in range(1, len(audit)))


# ===================================================== Part 2: reference smoke tests

AT = "2026-05-17T10:00:00.000Z"


def src(at=AT):
        return {"kind": "manual", "at": at}


def test_normalize_name_plain_form():
        assert normalize_name("Jose Garcia") == "jose garcia"


def test_merge_band_above_auto_merge():
        assert decide(0.86) == "auto-merge"


def test_upserts_merge_attrs_and_snapshot_answers_point_in_time_queries():
        world = WorldModel(storage="memory", namespace="t")
        world.upsert_entity(type="player", external_key=("g", "p1"), attrs={"name": "P One", "overall": 87},
                            source=src("2087-08-01T12:00:00.000Z"))
        world.upsert_entity(type="player", external_key=("g", "p1"), attrs={"overall": 94, "tier": "all-pro"},
                            source=src("2088-01-20T12:00:00.000Z"))
        august = world.view("snapshot").entity_at("player:g:p1", "2087-08-15")
        feb = world.view("snapshot").entity_at("player:g:p1", "2088-02-01")
        assert august["attrs"]["overall"] == 87 and "tier" not in august["attrs"]
        assert feb["attrs"]["overall"] == 94 and feb["attrs"]["tier"] == "all-pro"


def test_fact_conflicts_highest_confidence_wins():
        world = WorldModel(storage="memory", namespace="t", conflict_policy="highest-confidence-wins")
        world.assert_fact(about="person:jerome-powell", predicate="title", value="Chair", confidence=0.95,
                          source={"kind": "rss", "url": "a", "at": "2026-05-17T09:00:00.000Z"})
        world.assert_fact(about="person:jerome-powell", predicate="title", value="Chairman", confidence=0.7,
                          source={"kind": "rss", "url": "b", "at": "2026-05-17T09:30:00.000Z"})
        assert world.view("graph").entity("person:jerome-powell")["attrs"]["title"] == "Chair"
        facts = world.view("facts").about("person:jerome-powell")
        assert len(facts) == 2
        assert len([f for f in facts if f.get("superseded_by")]) == 1


def test_name_resolution_auto_merges_or_queues_by_band():
        world = WorldModel(storage="memory", namespace="t")
        world.upsert_entity(type="person", name="Jerome Powell", source=src())
        world.upsert_entity(type="person", name="jerome powell", source=src())  # same id after normalization
        merged = world.upsert_entity(type="person", name="Jerome Powel", source=src())  # 12/13 = 0.923
        assert merged["id"] == entity_id_from_name("person", "Jerome Powell")
        assert world.view("graph").entity(entity_id_from_name("person", "Jerome Powel"))["id"] == \
                entity_id_from_name("person", "Jerome Powell")
        sim = name_similarity("Jerome Powell x", "Jerome Powell abc")
        assert 0.82 <= sim < 0.85
        world.upsert_entity(type="person", name="Jerome Powell abc", source=src())
        world.upsert_entity(type="person", name="Jerome Powell x", source=src())
        assert any(p["status"] == "pending" for p in world.merge_queue())


def test_ingest_reports_processed_and_skipped_counts():
        observations = [
                {"external_id": "art-1", "title": "Fed holds rates", "at": "2026-05-17T14:00:00.000Z",
                 "source": {"kind": "rss", "at": "2026-05-17T14:00:00.000Z"}, "entities": [{"type": "person", "name": "Jerome Powell"}]},
                {"external_id": "art-2", "title": "Powell signals patience", "at": "2026-05-17T16:30:00.000Z",
                 "source": {"kind": "rss", "at": "2026-05-17T16:30:00.000Z"}, "entities": [{"type": "person", "name": "Jerome Powell"}]},
        ]
        world = WorldModel(storage="memory", namespace="t", clock=fixed_clock)
        first = world.ingest(observations)
        assert first["processed"] == 2 and first["clusters"] > 0
        shape1 = world.state_shape_canonical()
        second = world.ingest(observations)
        assert second["processed"] == 0 and second["skipped_unchanged"] == 2 and second["changed"] is False
        assert world.state_shape_canonical() == shape1
        third = world.ingest([{**observations[0], "title": "Fed holds rates steady"}])
        assert third["changed"] is True


def test_writes_land_in_audit_order():
        world = WorldModel(storage="memory", namespace="t")
        world.upsert_entity(type="x", name="First Entity", source=src("2026-01-01T00:00:00.000Z"))
        world.upsert_entity(type="x", name="Second Entity", source=src("2026-01-02T00:00:00.000Z"))
        audit = world.audit()
        assert len(audit) == 2
        assert entity_id_from_name("x", "First Entity") in audit[0]["summary"]
        assert audit[0]["seq"] < audit[1]["seq"]


def test_custom_views_fold_over_matching_events():
        world = WorldModel(storage="memory", namespace="t")
        world.upsert_entity(type="account", external_key=("sf", "a1"), attrs={}, source=src())
        world.upsert_context(type="campaign", external_key=("sf", "c1"), attrs={}, source=src())
        world.relate(from_="account:sf:a1", type="open-deal", to="campaign:sf:c1",
                     attrs={"stage": "negotiation", "daysInStage": 14}, source=src())

        def reducer(state, event):
                p = event["payload"]
                if p.get("type") == "open-deal" and (p.get("attrs") or {}).get("daysInStage", 0) > 10:
                        state.append({"account": p["from"], "days": p["attrs"]["daysInStage"]})
                return state

        world.register_view("stuck-deals", events=["relate"], initial_state=[], reducer=reducer)
        stuck = world.view("stuck-deals").get()
        assert stuck == [{"account": "account:sf:a1", "days": 14}]


def test_identical_operations_produce_identical_canonical_state():
        def go():
                world = WorldModel(storage="memory", namespace="t", clock=fixed_clock)
                world.upsert_entity(type="person", name="Ada Lovelace", source=src())
                world.assert_fact(about=entity_id_from_name("person", "Ada Lovelace"), predicate="field",
                                  value="mathematics", confidence=0.9, source=src())
                return world.state_shape_canonical() + canonical_json(world.audit())
        assert go() == go()


# ===================================================== Part 3: this port

def test_snapshot_view_sees_fact_values():
        # The TypeScript reference applies fact winners outside the event log, so this would fail there.
        world = WorldModel(storage="memory", namespace="t")
        world.upsert_entity(type="invoice", external_key=("zetaris", "I104"), attrs={"payment_status": "UNPAID"},
                            source=src("2026-10-21T00:00:00.000Z"))
        world.assert_fact(about="invoice:zetaris:I104", predicate="payment_status", value="PAID",
                          source=src("2026-10-25T00:00:00.000Z"))
        assert world.view("snapshot").entity_at("invoice:zetaris:I104", "2026-10-22")["attrs"]["payment_status"] == "UNPAID"
        assert world.view("snapshot").entity_at("invoice:zetaris:I104", "2026-10-26")["attrs"]["payment_status"] == "PAID"


def test_null_attrs_are_kept_distinct_from_missing_attrs():
        # A column returned as NULL is evidence; a column never retrieved is not.
        world = WorldModel(storage="memory", namespace="t")
        world.upsert_entity(type="order", external_key=("zetaris", "L102"), attrs={"invoice_id": None}, source=src())
        attrs = world.view("graph").entity("order:zetaris:L102")["attrs"]
        assert "invoice_id" in attrs and attrs["invoice_id"] is None
        assert "event_type" not in attrs


def test_relate_is_idempotent():
        world = WorldModel(storage="memory", namespace="t")
        world.relate(from_="invoice:z:I104", type="replaces", to="invoice:z:I102", source=src())
        world.relate(from_="invoice:z:I104", type="replaces", to="invoice:z:I102", source=src())
        assert len([e for e in world.events() if e["kind"] == "relate"]) == 1


def test_provenance_requires_kind_and_at():
        world = WorldModel(storage="memory", namespace="t")
        for bad in ({"at": AT}, {"kind": "manual"}, {}):
                with pytest.raises(WorldModelError):
                        world.upsert_entity(type="x", name="y", source=bad)


def test_local_storage_replays_the_log_on_load(tmp_path):
        world = WorldModel(storage="local", namespace="careops", directory=tmp_path)
        world.upsert_entity(type="order", external_key=("zetaris", "L103"), attrs={"lab_status": "COMPLETED"}, source=src())
        world.relate(from_="invoice:zetaris:I104", type="bills", to="order:zetaris:L103", source=src())
        reloaded = WorldModel(storage="local", namespace="careops", directory=tmp_path)
        assert reloaded.view("graph").entity("order:zetaris:L103")["attrs"]["lab_status"] == "COMPLETED"
        assert reloaded.state_shape_canonical() == world.state_shape_canonical()
        assert [a["seq"] for a in reloaded.audit()] == [a["seq"] for a in world.audit()]


def test_datetime_provenance_is_stored_as_iso():
        world = WorldModel(storage="memory", namespace="t")
        entity = world.upsert_entity(type="x", name="When", source={"kind": "manual", "at": datetime(2026, 10, 23, 12, tzinfo=timezone.utc)})
        assert entity["source"]["at"] == "2026-10-23T12:00:00.000Z"
