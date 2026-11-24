from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vsphere_agent.alarms import AlarmCollector
from vsphere_agent.serialize import ndjson_entry
from vsphere_agent.state import StateStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def entity(key):
    return SimpleNamespace(_moId=key)


def alarm(key, status="red"):
    return SimpleNamespace(key=key, overallStatus=status, time=NOW, acknowledged=False)


class Adapter:
    vcenter_id = "vc"
    vcenter_name = "example"
    vcenter_version = "7"
    def __init__(self):
        self.results = {"entity": [alarm("a")]}
    def enumerate_entities(self):
        return [entity(key) for key in self.results]
    def alarms_for(self, item):
        value = self.results[item._moId]
        if isinstance(value, Exception):
            raise value
        return value


def create(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    adapter, records = Adapter(), []
    collector = AlarmCollector(adapter, store, "logs-alarms", "logs-agent", records.append, clock=lambda: NOW)
    return store, adapter, collector, records


def finish(collector, records, start=0, status="success"):
    for record in list(records[start:]):
        if record.fields.get("snapshot_id") and record.fields.get("kind") != "snapshot_summary":
            collector.ack(record, status)


def test_missing_once_return_and_missing_again_with_append_snapshots(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    collector.collect(); finish(collector, records)
    adapter.results["entity"] = []
    start = len(records); collector.collect(); finish(collector, records, start)
    start = len(records); collector.collect(); finish(collector, records, start)
    adapter.results["entity"] = [alarm("a")]
    start = len(records); collector.collect(); finish(collector, records, start)
    adapter.results["entity"] = []
    start = len(records); collector.collect(); finish(collector, records, start)
    missing = [r for r in records if r.fields.get("kind") == "alarm_not_returned"]
    snapshots = [r for r in records if r.data_type == "alarm"]
    assert len(missing) == 2 and len(snapshots) == 2
    assert snapshots[0].document_id != snapshots[1].document_id
    assert "原因未知" in missing[0].fields["description"]
    store.close()


def test_empty_query_success_failure_and_lost_scope_are_not_confused(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    collector.collect(); finish(collector, records)
    adapter.results["entity"] = RuntimeError("permission changed")
    start = len(records); collector.collect(); finish(collector, records, start)
    adapter.results["entity"] = []
    start = len(records); collector.collect(); finish(collector, records, start)
    assert not [r for r in records if r.fields.get("kind") == "alarm_not_returned"]
    summaries = [r for r in records if r.fields.get("kind") == "snapshot_summary"]
    assert [r.fields["snapshot_status"] for r in summaries] == ["complete", "partial", "complete"]
    store.close()


def test_snapshot_waits_for_ack_and_rejects_overlapping_run(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    batch = collector.collect()
    assert batch is not None
    assert collector.collect() is None
    assert not [r for r in records if r.fields.get("kind") == "snapshot_summary"]
    first = next(r for r in records if r.data_type == "alarm")
    collector.ack(first, "unknown")
    summary = next(r for r in records if r.fields.get("kind") == "snapshot_summary")
    assert summary.fields["snapshot_status"] == "unknown"
    assert summary.fields["expected_records"] == 1
    assert summary.fields["confirmed_records"] == 0
    store.close()


def test_invisible_entity_does_not_produce_missing(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    collector.collect(); finish(collector, records)
    adapter.results = {}
    start = len(records); collector.collect(); finish(collector, records, start)
    adapter.results = {"entity": []}
    start = len(records); collector.collect(); finish(collector, records, start)
    assert not [r for r in records if r.fields.get("kind") == "alarm_not_returned"]
    store.close()


def test_known_permission_failure_later_in_batch_discards_prior_missing_conclusions(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    adapter.results = {"entity-1": [alarm("a")], "entity-2": [alarm("b")]}
    collector.collect(); finish(collector, records)
    adapter.results = {"entity-1": [], "entity-2": vim.fault.NoPermission()}
    start = len(records); collector.collect(); finish(collector, records, start)
    assert not [r for r in records if r.fields.get("kind") == "alarm_not_returned"]
    store.close()


def test_duplicate_alarm_key_cannot_claim_snapshot_complete(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    adapter.results["entity"] = [alarm("a"), alarm("a")]
    collector.collect(); finish(collector, records)
    summary = next(r for r in records if r.fields.get("kind") == "snapshot_summary")
    assert summary.fields["snapshot_status"] != "complete"
    store.close()


def test_session_expiry_is_propagated_to_reconnect(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    adapter.results["entity"] = vim.fault.NotAuthenticated()
    with pytest.raises(vim.fault.NotAuthenticated):
        collector.collect()
    assert not [r for r in records if r.fields.get("kind") == "alarm_not_returned"]
    store.close()


def test_result_returned_after_generation_invalidated_is_ignored(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    original = adapter.alarms_for
    active = [True]
    collector.active = lambda: active[0]
    def late(item):
        result = original(item)
        active[0] = False
        return result
    adapter.alarms_for = late
    collector.collect()
    assert not [r for r in records if r.data_type == "alarm"]
    assert store.entities("vc") == set()
    store.close()


def test_alarm_snapshot_includes_readable_metadata_and_confirmation(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    collector.secrets = ("test-secret",)
    obj = SimpleNamespace(_moId="vm-1", name="vm-test-secret")
    definition = SimpleNamespace(_moId="alarm-1", info=SimpleNamespace(
        name="虚拟机 CPU 使用情况", description="CPU 告警 test-secret"))
    item = alarm("alarm-1.vm-1", "yellow")
    item.alarm = definition
    item.acknowledged = True
    item.acknowledgedByUser = "operator"
    item.acknowledgedTime = NOW
    item.eventKey = 42
    adapter.enumerate_entities = lambda: [obj]
    adapter.alarms_for = lambda entity: [item]

    collector.collect(); finish(collector, records)
    fields = next(record.fields for record in records if record.data_type == "alarm")
    assert fields["entity_moref"] == "vm-1"
    assert fields["entity_name"] == "vm-[已隐藏]"
    assert fields["alarm_moref"] == "alarm-1"
    assert fields["alarm_name"] == "虚拟机 CPU 使用情况"
    assert fields["alarm_description"] == "CPU 告警 [已隐藏]"
    assert fields["state"] == "yellow" and fields["state_label"] == "警告"
    assert fields["acknowledged_time"] == NOW
    assert fields["acknowledged_by_user"] == "operator"
    assert fields["alarm_event_key"] == 42
    assert "虚拟机 CPU 使用情况" in fields["message"] and "警告" in fields["message"]
    record = next(record for record in records if record.data_type == "alarm")
    document = json.loads(ndjson_entry(record).decode().splitlines()[1])
    assert document["alarm_event_key"] == 42 and document["acknowledged_time"]
    assert "test-secret" not in json.dumps(document)
    store.close()


def test_alarm_preserves_untranslated_api_name(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    obj = SimpleNamespace(_moId="vm-1", name="example-vm")
    item = alarm("alarm-1.vm-1", "yellow")
    item.alarm = SimpleNamespace(_moId="alarm-1", info=SimpleNamespace(name="Untranslated alarm"))
    adapter.enumerate_entities = lambda: [obj]
    adapter.alarms_for = lambda entity: [item]
    try:
        collector.collect()
        fields = next(record.fields for record in records if record.data_type == "alarm")
        assert fields["alarm_name"] == "Untranslated alarm"
        assert "Untranslated alarm" in fields["message"]
        assert fields["state"] == "yellow"
    finally:
        store.close()


def test_alarm_metadata_permission_failure_preserves_raw_snapshot(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    class InaccessibleEntity:
        _moId = "vm-1"
        @property
        def name(self):
            raise vim.fault.NoPermission()
    class InaccessibleAlarm:
        _moId = "alarm-1"
        @property
        def info(self):
            raise vim.fault.NoPermission()
    item = alarm("alarm-1.vm-1", "gray")
    item.alarm = InaccessibleAlarm()
    adapter.enumerate_entities = lambda: [InaccessibleEntity()]
    adapter.alarms_for = lambda entity: [item]

    collector.collect(); finish(collector, records)
    fields = next(record.fields for record in records if record.data_type == "alarm")
    assert fields["state"] == "gray" and fields["state_label"] == "未知"
    assert "entity_name" not in fields and "alarm_name" not in fields
    assert fields["metadata_status"] == "unavailable"
    assert next(r for r in records if r.fields.get("kind") == "snapshot_summary").fields["snapshot_status"] == "complete"
    store.close()


def test_alarm_definition_is_read_once_per_snapshot_and_refreshed_next_snapshot(tmp_path):
    store, adapter, collector, records = create(tmp_path)
    reads = []
    class Definition:
        _moId = "alarm-1"
        @property
        def info(self):
            reads.append(1)
            return SimpleNamespace(name=f"alarm version {len(reads)}")
    definition = Definition()
    entities = [SimpleNamespace(_moId="vm-1", name="one"), SimpleNamespace(_moId="vm-2", name="two")]
    adapter.enumerate_entities = lambda: entities
    def alarms_for(obj):
        item = alarm("alarm-1." + obj._moId)
        item.alarm = definition
        return [item]
    adapter.alarms_for = alarms_for

    collector.collect(); finish(collector, records)
    assert len(reads) == 1
    assert [r.fields["alarm_name"] for r in records if r.data_type == "alarm"] == ["alarm version 1"] * 2
    start = len(records)
    collector.collect(); finish(collector, records, start)
    assert len(reads) == 2
    assert [r.fields["alarm_name"] for r in records[start:] if r.data_type == "alarm"] == ["alarm version 2"] * 2
    store.close()
