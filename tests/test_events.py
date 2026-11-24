from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from pyVmomi import vim

from vsphere_agent.events import EventCollector
from vsphere_agent.serialize import ndjson_entry
from vsphere_agent.state import StateStore


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def event(key, seconds):
    return SimpleNamespace(key=key, createdTime=BASE + timedelta(seconds=seconds), fullFormattedMessage="test event", userName="user")


class FakeAdapter:
    vcenter_id = "vc-1"
    vcenter_name = "example"
    vcenter_version = "7"
    def __init__(self, pages):
        self.pages = pages
    def read_events(self, size):
        for page in self.pages:
            yield page


def test_filters_start_boundary_and_deduplicates_multiple_unordered_pages(tmp_path):
    adapter = FakeAdapter([[event(1, -1), event(2, 0), event(5, 3)], [event(3, 1), event(4, 2), event(5, 3)]])
    store = StateStore(tmp_path / "state.sqlite")
    records = []
    collector = EventCollector(adapter, store, "logs-events", BASE, records.append, page_size=2)
    collector.collect()
    assert [r.fields["event_id"] for r in records] == [5, 3, 4]
    assert all(r.timestamp > BASE for r in records)
    collector.collect()
    assert len(records) == 3
    store.close()


def test_new_boundary_never_replays_disconnected_interval(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    records = []
    adapter = FakeAdapter([[event(1, 1), event(2, 4), event(3, 7)]])
    collector = EventCollector(adapter, store, "logs-events", BASE + timedelta(seconds=5), records.append)
    collector.collect()
    assert [r.fields["event_id"] for r in records] == [3]
    store.close()


def test_event_entity_name_does_not_expose_known_credentials(tmp_path):
    item = event(1, 1)
    item.vm = SimpleNamespace(name="name test-secret", vm=SimpleNamespace(_moId="vm-1"))
    store = StateStore(tmp_path / "state.sqlite")
    records = []
    EventCollector(FakeAdapter([[item]]), store, "logs-events", BASE, records.append,
                   secrets=("test-secret",)).collect()
    assert "test-secret" not in records[0].fields["entity_name"]
    store.close()


def test_event_includes_readable_message_type_category_and_chain(tmp_path):
    item = vim.event.EventEx(key=9, chainId=12, createdTime=BASE + timedelta(seconds=1), userName="operator",
                             fullFormattedMessage="启动虚拟机 test-secret", eventTypeId="custom.start",
                             severity="warning", objectName="vm-test-secret")
    adapter = FakeAdapter([[item]])
    adapter.event_metadata = lambda cls: ("虚拟机事件", "warning")
    store, records = StateStore(tmp_path / "state.sqlite"), []
    EventCollector(adapter, store, "logs-events", BASE, records.append, secrets=("test-secret",)).collect()
    fields = records[0].fields
    assert fields["event_id"] == 9 and fields["event_chain_id"] == 12
    assert fields["event_type_id"] == "custom.start" and fields["event_severity"] == "warning"
    assert fields["event_type_name"] == "虚拟机事件" and fields["event_category"] == "warning"
    assert fields["description"] == fields["message"] == "启动虚拟机 [已隐藏]"
    assert fields["entity_name"] == "vm-[已隐藏]" and fields["user_name"] == "operator"
    document = json.loads(ndjson_entry(records[0]).decode().splitlines()[1])
    assert document["event_chain_id"] == 12 and document["event_severity"] == "warning"
    assert "test-secret" not in json.dumps(document)
    store.close()


def test_task_event_includes_returned_task_key_and_fallback_message(tmp_path):
    info = vim.TaskInfo(key="task-7", descriptionId="VirtualMachine.powerOn")
    item = vim.event.TaskEvent(key=2, chainId=3, createdTime=BASE + timedelta(seconds=1), info=info)
    adapter = FakeAdapter([[item]])
    store, records = StateStore(tmp_path / "state.sqlite"), []
    EventCollector(adapter, store, "logs-events", BASE, records.append).collect()
    fields = records[0].fields
    assert fields["task_key"] == "task-7"
    assert "TaskEvent" in fields["message"]
    assert "event_type_name" not in fields and "event_severity" not in fields
    store.close()


def test_event_ex_uses_returned_message_when_formatted_message_is_absent(tmp_path):
    item = vim.event.EventEx(key=1, createdTime=BASE + timedelta(seconds=1), message="说明 test-secret")
    store, records = StateStore(tmp_path / "state.sqlite"), []
    EventCollector(FakeAdapter([[item]]), store, "logs-events", BASE, records.append, secrets=("test-secret",)).collect()
    assert records[0].fields["message"] == "说明 [已隐藏]"
    store.close()


def test_event_preserves_untranslated_api_message_and_type(tmp_path):
    item = event(1, 1)
    item.fullFormattedMessage = "Untranslated event"
    adapter = FakeAdapter([[item]])
    adapter.event_metadata = lambda cls: ("Untranslated event type", "info")
    store, records = StateStore(tmp_path / "state.sqlite"), []
    try:
        EventCollector(adapter, store, "logs-events", BASE, records.append).collect()
        fields = records[0].fields
        assert fields["description"] == fields["message"] == "Untranslated event"
        assert fields["event_type_name"] == "Untranslated event type"
        assert fields["event_id"] == 1
    finally:
        store.close()


def test_event_metadata_returned_after_generation_expires_is_ignored(tmp_path):
    item = event(1, 1)
    active = [True]
    adapter = FakeAdapter([[item]])
    def metadata(cls):
        active[0] = False
        return ("late", "info")
    adapter.event_metadata = metadata
    store, records = StateStore(tmp_path / "state.sqlite"), []
    EventCollector(adapter, store, "logs-events", BASE, records.append, active=lambda: active[0]).collect()
    assert records == []
    assert store.remember_event("vc-1", 1, datetime.now(timezone.utc).timestamp())
    store.close()
