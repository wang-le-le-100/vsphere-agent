from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pyVmomi import vim, vmodl

from vsphere_agent.config import VCenterConfig
from vsphere_agent.vcenter import VCenterAdapter


class History:
    def __init__(self):
        self.pages = [["task-a"], []]
    def ReadNextTasks(self, count):
        return self.pages.pop(0)
    def DestroyCollector(self):
        pass


class Manager:
    recentTask = []
    def CreateCollectorForTasks(self, filter):
        self.spec = filter
        return History()


def test_task_collector_filters_queued_time_and_pages():
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    manager = Manager()
    adapter.content = SimpleNamespace(taskManager=manager)
    boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)
    adapter.open_tasks(boundary)
    assert manager.spec.time.timeType == "queuedTime"
    assert manager.spec.time.beginTime == boundary
    assert list(adapter.read_tasks(500)) == [["task-a"]]
    assert adapter.active_tasks() == []
    adapter.close()


class PropertyCollector:
    def __init__(self):
        self.filters = []
        self.closed = False
    def CreateFilter(self, spec, partialUpdates):
        item = SimpleNamespace(DestroyPropertyFilter=lambda: self.filters.remove(item))
        self.filters.append(item)
        return item
    def WaitForUpdatesEx(self, version, options):
        task = vim.Task("task-1", None)
        changes = [SimpleNamespace(name="info.progress", val=50), SimpleNamespace(name="info.state", val="success")]
        update = SimpleNamespace(obj=task, changeSet=changes, missingSet=[], kind="modify")
        return SimpleNamespace(version="1", filterSet=[SimpleNamespace(objectSet=[update], missingSet=[])])
    def DestroyPropertyCollector(self):
        self.closed = True


def test_property_collector_is_single_owner_and_only_returns_state_changes():
    pc = PropertyCollector()
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.instance = SimpleNamespace(_stub=None)
    adapter.content = SimpleNamespace(propertyCollector=SimpleNamespace(CreatePropertyCollector=lambda: pc))
    adapter.open_task_monitor()
    adapter.sync_task_watch({"task-1": "task-1"})
    assert adapter.wait_task_updates(1) == [("task-1", "success")]
    adapter.sync_task_watch({})
    assert pc.filters == []
    adapter.instance = None
    adapter.close()
    assert pc.closed


def test_task_label_uses_vcenter_method_description_and_caches_it():
    reads = []
    class TaskManager:
        @property
        def description(self):
            reads.append(1)
            return SimpleNamespace(methodInfo=[vim.ElementDescription(key="VirtualMachine.migrate", label="迁移虚拟机")])
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.content = SimpleNamespace(taskManager=TaskManager())
    assert adapter.task_label("VirtualMachine.migrate") == "迁移虚拟机"
    assert adapter.task_label("unknown") is None
    assert len(reads) == 1


@pytest.mark.parametrize("fault", [vim.fault.NoPermission(), vmodl.fault.ManagedObjectNotFound()])
def test_unavailable_task_info_does_not_lose_observed_state(monkeypatch, fault):
    class Task:
        @property
        def info(self):
            raise fault
    monkeypatch.setattr("vsphere_agent.vcenter.vim.Task", lambda moid, stub: Task())
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.instance = SimpleNamespace(_stub=None)
    assert adapter.task_info("task-1") is None


@pytest.mark.parametrize("fault", [vim.fault.NotAuthenticated(), OSError("network")])
def test_task_info_connection_failure_propagates(monkeypatch, fault):
    class Task:
        @property
        def info(self):
            raise fault
    monkeypatch.setattr("vsphere_agent.vcenter.vim.Task", lambda moid, stub: Task())
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.instance = SimpleNamespace(_stub=None)
    with pytest.raises(type(fault)):
        adapter.task_info("task-1")


def test_task_label_permission_failure_is_optional_and_auth_failure_propagates():
    class TaskManager:
        def __init__(self, fault):
            self.fault = fault
        @property
        def description(self):
            raise self.fault
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.content = SimpleNamespace(taskManager=TaskManager(vim.fault.NoPermission()))
    assert adapter.task_label("example") is None
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.content = SimpleNamespace(taskManager=TaskManager(vim.fault.NotAuthenticated()))
    with pytest.raises(vim.fault.NotAuthenticated):
        adapter.task_label("example")
