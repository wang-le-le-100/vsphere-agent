from datetime import datetime, timezone
import ssl
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vsphere_agent.config import VCenterConfig
from vsphere_agent.vcenter import VCenterAdapter


class FakeHistory:
    def __init__(self):
        self.pages = [[1, 2], [3], []]
        self.closed = False
    def ReadNextEvents(self, count):
        return self.pages.pop(0)
    def DestroyCollector(self):
        self.closed = True


class FakeManager:
    def __init__(self):
        self.history = FakeHistory()
        self.filter = None
    def CreateCollectorForEvents(self, filter):
        self.filter = filter
        return self.history


def test_connect_uses_https_verified_and_releases_collector(monkeypatch):
    manager = FakeManager()
    session = SimpleNamespace(SetLocale=lambda locale: None, currentSession=SimpleNamespace(messageLocale="zh_CN"))
    content = SimpleNamespace(about=SimpleNamespace(instanceUuid="vc-id", name="vc", version="7"), eventManager=manager,
                              sessionManager=session)
    instance = SimpleNamespace(RetrieveContent=lambda: content)
    calls = []
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: calls.append(kwargs) or instance)
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", lambda si: calls.append(si))
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.connect()
    boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)
    adapter.open_events(boundary)
    assert list(adapter.read_events(2)) == [[1, 2], [3]]
    assert manager.filter.time.beginTime == boundary
    assert manager.filter.disableFullMessage is False
    assert calls[0]["sslContext"].verify_mode == ssl.CERT_REQUIRED
    assert calls[0]["sslContext"].check_hostname
    assert adapter.vcenter_id == "vc-id"
    adapter.close()
    assert manager.history.closed
    assert calls[-1] is instance


def test_connect_can_explicitly_disable_certificate_verification(monkeypatch):
    calls = []
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: calls.append(kwargs) or locale_instance(LocaleManager()))
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", lambda si: None)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret", verify_ssl=False))
    adapter.connect()
    assert calls[0]["sslContext"].verify_mode == ssl.CERT_NONE
    assert not calls[0]["sslContext"].check_hostname
    adapter.close()


def test_connect_uses_configured_ca(monkeypatch):
    calls = []
    context = ssl.create_default_context()
    monkeypatch.setattr("vsphere_agent.vcenter.ssl.create_default_context", lambda **kwargs: calls.append(kwargs) or context)
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: locale_instance(LocaleManager()))
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", lambda si: None)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret", ca_file="/path/ca.pem"))
    adapter.connect()
    assert calls == [{"cafile": "/path/ca.pem"}]
    adapter.close()


def test_missing_vcenter_instance_uuid_is_an_error(monkeypatch):
    content = SimpleNamespace(about=SimpleNamespace(instanceUuid=None, name="vc", version="7"))
    instance = SimpleNamespace(RetrieveContent=lambda: content)
    closed = []
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: instance)
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", closed.append)
    with pytest.raises(ValueError):
        VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret")).connect()
    assert closed == [instance]


class LocaleManager:
    def __init__(self, message_locale="zh_CN", fault=None):
        self.message_locale = message_locale
        self.fault = fault
        self.calls = []

    def SetLocale(self, locale):
        self.calls.append(locale)
        if self.fault:
            raise self.fault

    @property
    def currentSession(self):
        return SimpleNamespace(messageLocale=self.message_locale)


def locale_instance(session):
    task_description = SimpleNamespace(methodInfo=[
        vim.ElementDescription(key="VirtualMachine.migrate", label="迁移虚拟机"),
        vim.ElementDescription(key="Datastore.updateVVolVirtualMachineFiles", label="updateVVolVirtualMachineFiles"),
    ])
    event_description = SimpleNamespace(eventInfo=[vim.event.EventDescription.EventDetail(
        key=vim.event.AlarmStatusChangedEvent, description="已更改警报状态", category="info")])
    content = SimpleNamespace(
        about=SimpleNamespace(instanceUuid="vc-id", name="vc", version="7"), sessionManager=session,
        taskManager=SimpleNamespace(description=task_description),
        eventManager=SimpleNamespace(description=event_description),
    )
    return SimpleNamespace(RetrieveContent=lambda: content)


def test_connect_requests_chinese_again_for_each_new_session(monkeypatch):
    sessions = [LocaleManager(), LocaleManager(message_locale="zh-CN")]
    instances = iter(locale_instance(session) for session in sessions)
    closed = []
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: next(instances))
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", closed.append)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))

    for session in sessions:
        adapter.connect()
        assert session.calls == ["zh_CN"]
        assert adapter.locale_issue is None
        assert adapter.task_label("VirtualMachine.migrate") == "迁移虚拟机"
        assert adapter.task_label("Datastore.updateVVolVirtualMachineFiles") == "updateVVolVirtualMachineFiles"
        assert adapter.event_metadata(vim.event.AlarmStatusChangedEvent) == ("已更改警报状态", "info")
        adapter.close()
    assert len(closed) == 2


@pytest.mark.parametrize("fault", [vim.fault.InvalidLocale(), vim.fault.NoPermission()])
def test_unsupported_locale_keeps_session_and_reports_degradation(monkeypatch, fault):
    session = LocaleManager(message_locale="en", fault=fault)
    instance = locale_instance(session)
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: instance)
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", lambda si: None)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    assert adapter.connect() is adapter
    assert session.calls == ["zh_CN"]
    assert adapter.locale_issue == "locale_unavailable"
    adapter.close()


@pytest.mark.parametrize("message_locale", ["en", "zh_TW", None])
def test_non_simplified_chinese_message_locale_reports_degradation(monkeypatch, message_locale):
    session = LocaleManager(message_locale=message_locale)
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: locale_instance(session))
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", lambda si: None)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.connect()
    assert adapter.locale_issue == "message_locale_unavailable"
    adapter.close()


@pytest.mark.parametrize("fault", [vim.fault.NotAuthenticated(), OSError("network")])
def test_locale_connection_failure_propagates_and_closes_session(monkeypatch, fault):
    session = LocaleManager(fault=fault)
    instance = locale_instance(session)
    closed = []
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: instance)
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", closed.append)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    with pytest.raises(type(fault)):
        adapter.connect()
    assert closed == [instance]
    assert adapter.content is None


def test_missing_current_session_propagates_auth_failure(monkeypatch):
    class MissingSession(LocaleManager):
        @property
        def currentSession(self):
            return None
    session = MissingSession()
    instance = locale_instance(session)
    closed = []
    monkeypatch.setattr("vsphere_agent.vcenter.SmartConnect", lambda **kwargs: instance)
    monkeypatch.setattr("vsphere_agent.vcenter.Disconnect", closed.append)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    with pytest.raises(vim.fault.NotAuthenticated):
        adapter.connect()
    assert closed == [instance]


def test_event_metadata_uses_sdk_event_class_key_and_is_cached():
    reads = []
    class Manager:
        @property
        def description(self):
            reads.append(True)
            return SimpleNamespace(eventInfo=[vim.event.EventDescription.EventDetail(
                key=vim.event.TaskEvent, description="任务事件", category="info")])
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.content = SimpleNamespace(eventManager=Manager())
    assert adapter.event_metadata(vim.event.TaskEvent) == ("任务事件", "info")
    assert adapter.event_metadata(vim.event.EventEx) == (None, None)
    assert len(reads) == 1


def test_event_metadata_permission_failure_is_optional_but_expired_session_propagates():
    class Manager:
        def __init__(self, fault):
            self.fault = fault
        @property
        def description(self):
            raise self.fault
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.content = SimpleNamespace(eventManager=Manager(vim.fault.NoPermission()))
    assert adapter.event_metadata(vim.event.TaskEvent) == (None, None)
    adapter = VCenterAdapter(VCenterConfig("vc.invalid", "user", "secret"))
    adapter.content = SimpleNamespace(eventManager=Manager(vim.fault.NotAuthenticated()))
    with pytest.raises(vim.fault.NotAuthenticated):
        adapter.event_metadata(vim.event.TaskEvent)
