from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from vsphere_agent.serialize import ndjson_entry
from vsphere_agent.tasks import TaskTracker


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def task(key, second, state, progress=0):
    return SimpleNamespace(key=key, queueTime=BASE + timedelta(seconds=second), state=state, progress=progress,
                           task=SimpleNamespace(_moId=key), descriptionId="sample", entity=None, entityName=None)


def adapter():
    return SimpleNamespace(vcenter_id="vc", vcenter_name="example", vcenter_version="7")


def test_old_active_task_is_baselined_only_then_emits_state_changes():
    records = []
    tracker = TaskTracker(adapter(), "logs-tasks", BASE, "session", records.append, clock=lambda: BASE + timedelta(seconds=1))
    tracker.baseline([task("old", -10, "running")])
    assert records == []
    tracker.update("old", "running")
    assert records == []
    tracker.update("old", "success")
    assert [r.fields["state"] for r in records] == ["success"]
    assert tracker.active_refs() == {}


def test_new_short_task_and_listener_state_change_are_append_only():
    records = []
    tracker = TaskTracker(adapter(), "logs-tasks", BASE, "session", records.append, clock=lambda: BASE + timedelta(seconds=10))
    tracker.discover(task("too-old", 0, "success"))
    tracker.discover(task("quick", 1, "success"))
    tracker.discover(task("running", 2, "queued"))
    tracker.discover(task("running", 2, "queued", progress=50))
    tracker.update("running", "running")
    tracker.update("running", "running")
    tracker.update("running", "error")
    assert [(r.fields["task_key"], r.fields["state"]) for r in records] == [("quick", "success"), ("running", "queued"), ("running", "running"), ("running", "error")]
    assert len({r.document_id for r in records}) == 4
    assert all(r.timestamp == BASE + timedelta(seconds=10) for r in records)


def test_reconnection_builds_new_baseline_without_replaying_previous_state():
    records = []
    tracker = TaskTracker(adapter(), "logs-tasks", BASE + timedelta(seconds=5), "new-session", records.append)
    tracker.baseline([task("during-disconnect", 3, "running")])
    tracker.discover(task("during-disconnect", 3, "success"))
    assert records == []
    tracker.update("during-disconnect", "success")
    assert [r.fields["state"] for r in records] == ["success"]


def test_task_record_contains_readable_name_details_times_and_error():
    records = []
    vc = adapter()
    vc.task_label = lambda key: "迁移虚拟机" if key == "VirtualMachine.migrate" else None
    item = task("move", 1, "error", progress=42)
    item.descriptionId = "VirtualMachine.migrate"
    item.description = SimpleNamespace(message="迁移虚拟机 test-secret")
    item.entityName = "vm-test-secret"
    item.reason = SimpleNamespace(userName="operator")
    item.startTime = BASE + timedelta(seconds=2)
    item.completeTime = BASE + timedelta(seconds=5)
    item.error = SimpleNamespace(localizedMessage="任务失败 test-secret")
    tracker = TaskTracker(vc, "logs-tasks", BASE, "session", records.append, secrets=("test-secret",))

    tracker.discover(item)
    fields = records[0].fields
    assert fields["description"] == "VirtualMachine.migrate"
    assert fields["task_name"] == "迁移虚拟机"
    assert fields["task_detail"] == "迁移虚拟机 [已隐藏]"
    assert fields["entity_name"] == "vm-[已隐藏]" and fields["user_name"] == "operator"
    assert fields["state"] == "error" and fields["state_label"] == "失败"
    assert fields["progress"] == 42 and fields["error"] == "任务失败 [已隐藏]"
    assert fields["queue_time"] == item.queueTime and fields["start_time"] == item.startTime
    assert fields["complete_time"] == item.completeTime and fields["queue_duration_ms"] == 1000
    assert "迁移虚拟机" in fields["message"] and "失败" in fields["message"]
    document = json.loads(ndjson_entry(records[0]).decode().splitlines()[1])
    assert document["queue_duration_ms"] == 1000 and document["queue_time"]
    assert document["task_name"] == "迁移虚拟机" and "test-secret" not in json.dumps(document)


def test_task_preserves_untranslated_api_label():
    vc = adapter()
    vc.task_label = lambda key: "updateVVolVirtualMachineFiles"
    item = task("task-1", 1, "success")
    records = []
    TaskTracker(vc, "logs-tasks", BASE, "session", records.append).discover(item)
    assert records[0].fields["task_name"] == "updateVVolVirtualMachineFiles"
    assert "updateVVolVirtualMachineFiles" in records[0].fields["message"]
    assert records[0].fields["state"] == "success"


def test_task_state_changes_use_fresh_details_without_progress_only_records():
    records = []
    tracker = TaskTracker(adapter(), "logs-tasks", BASE, "session", records.append)
    tracker.discover(task("move", 1, "queued", progress=0))
    current = task("move", 1, "running", progress=48)
    current.description = SimpleNamespace(message="正在迁移")
    current.startTime = BASE + timedelta(seconds=2)
    tracker.update("move", "running", current)
    current.progress = 60
    tracker.update("move", "running", current)
    assert len(records) == 2
    assert records[1].fields["progress"] == 48
    assert records[1].fields["task_detail"] == "正在迁移"

    final = task("move", 1, "error", progress=70)
    final.error = SimpleNamespace(localizedMessage="网络错误")
    final.completeTime = BASE + timedelta(seconds=5)
    tracker.update("move", "error", final)
    assert records[-1].fields["error"] == "网络错误"
    assert records[-1].fields["progress"] == 70
    assert records[-1].fields["complete_time"] == final.completeTime


def test_task_mismatched_or_missing_detail_does_not_reuse_stale_progress():
    records = []
    tracker = TaskTracker(adapter(), "logs-tasks", BASE, "session", records.append)
    tracker.discover(task("move", 1, "queued", progress=0))
    newer = task("move", 1, "success", progress=100)
    tracker.update("move", "running", newer)
    assert records[-1].fields["state"] == "running"
    assert "progress" not in records[-1].fields
    assert records[-1].fields["metadata_status"] == "state_changed_during_lookup"
    tracker.update("move", "success", None)
    assert "progress" not in records[-1].fields
    assert records[-1].fields["metadata_status"] == "unavailable"


def test_missing_localized_error_message_is_not_fabricated():
    item = task("failed", 1, "error")
    item.error = SimpleNamespace(localizedMessage=None)
    records = []
    tracker = TaskTracker(adapter(), "logs-tasks", BASE, "session", records.append)
    tracker.discover(item)
    assert "error" not in records[0].fields
    assert "None" not in records[0].fields["message"]
