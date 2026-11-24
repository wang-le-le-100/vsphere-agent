"""vSphere 会话与会话级 API 资源。"""

import ssl

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl


class VCenterAdapter:
    def __init__(self, config):
        self.config = config
        self.instance = None
        self.content = None
        self._resources = []
        self.event_history = None
        self.task_history = None
        self._task_labels = None
        self._event_metadata = None
        self.locale_issue = None

    def connect(self):
        context = (ssl.create_default_context(cafile=self.config.ca_file) if self.config.verify_ssl
                   else ssl._create_unverified_context())
        self.instance = SmartConnect(host=self.config.host, port=self.config.port, user=self.config.username,
                                     pwd=self.config.password, sslContext=context,
                                     httpConnectionTimeout=self.config.request_timeout)
        try:
            self.content = self.instance.RetrieveContent()
            about = self.content.about
            if not about.instanceUuid:
                raise ValueError("vCenter 未返回稳定的 instanceUuid")
            self.locale_issue = None
            try:
                self.content.sessionManager.SetLocale("zh_CN")
                session = self.content.sessionManager.currentSession
                if session is None:
                    raise vim.fault.NotAuthenticated()
                message_locale = session.messageLocale
                if not isinstance(message_locale, str) or message_locale.lower().replace("-", "_") != "zh_cn":
                    self.locale_issue = "message_locale_unavailable"
            except vim.fault.NotAuthenticated:
                raise
            except (vim.fault.InvalidLocale, vim.fault.NoPermission):
                self.locale_issue = "locale_unavailable"
            self.vcenter_id = about.instanceUuid
            self.vcenter_name = about.name
            self.vcenter_version = about.version
            return self
        except Exception:
            self.close()
            raise

    def open_events(self, boundary):
        spec = vim.event.EventFilterSpec(time=vim.event.EventFilterSpec.ByTime(beginTime=boundary), disableFullMessage=False)
        self.event_history = self.content.eventManager.CreateCollectorForEvents(filter=spec)
        self._resources.append((self.event_history, "DestroyCollector"))

    def read_events(self, page_size):
        while True:
            page = self.event_history.ReadNextEvents(page_size)
            if not page:
                break
            yield page

    def event_metadata(self, event_class):
        if self._event_metadata is None:
            try:
                descriptions = self.content.eventManager.description.eventInfo
                self._event_metadata = {
                    item.key: (item.description, item.category) for item in descriptions if item.key
                }
            except vim.fault.NotAuthenticated:
                raise
            except vmodl.MethodFault:
                self._event_metadata = {}
        return self._event_metadata.get(event_class, (None, None))

    def open_tasks(self, boundary):
        time_filter = vim.TaskFilterSpec.ByTime(timeType="queuedTime", beginTime=boundary)
        self.task_history = self.content.taskManager.CreateCollectorForTasks(filter=vim.TaskFilterSpec(time=time_filter))
        self._resources.append((self.task_history, "DestroyCollector"))

    def read_tasks(self, page_size):
        while True:
            page = self.task_history.ReadNextTasks(page_size)
            if not page:
                break
            yield page

    def active_tasks(self):
        active = []
        for task in self.content.taskManager.recentTask:
            info = task.info
            if str(info.state) in ("queued", "running"):
                active.append(info)
        return active

    def task_label(self, description_id):
        if self._task_labels is None:
            try:
                descriptions = self.content.taskManager.description.methodInfo
                self._task_labels = {item.key: item.label for item in descriptions if item.key and item.label}
            except vim.fault.NotAuthenticated:
                raise
            except vmodl.MethodFault:
                self._task_labels = {}
        return self._task_labels.get(description_id)

    def task_info(self, moid):
        try:
            return vim.Task(moid, self.instance._stub).info
        except vim.fault.NotAuthenticated:
            raise
        except vmodl.MethodFault:
            return None

    def enumerate_entities(self):
        view = self.content.viewManager.CreateContainerView(self.content.rootFolder, [vim.ManagedEntity], True)
        try:
            return list(view.view)
        finally:
            view.DestroyView()

    def alarms_for(self, entity):
        return self.content.alarmManager.GetAlarmState(entity=entity)

    def open_task_monitor(self):
        self._task_monitor = self.content.propertyCollector.CreatePropertyCollector()
        self._resources.append((self._task_monitor, "DestroyPropertyCollector"))
        self._task_filters = {}
        self._task_version = ""

    def sync_task_watch(self, refs):
        for moid in set(self._task_filters) - set(refs):
            self._task_filters.pop(moid).DestroyPropertyFilter()
        for moid in set(refs) - set(self._task_filters):
            task = vim.Task(moid, self.instance._stub)
            prop = vim.PropertyCollector.PropertySpec(type=vim.Task, pathSet=["info.state"])
            spec = vim.PropertyCollector.FilterSpec(propSet=[prop], objectSet=[vim.PropertyCollector.ObjectSpec(obj=task)])
            self._task_filters[moid] = self._task_monitor.CreateFilter(spec=spec, partialUpdates=True)

    def wait_task_updates(self, timeout):
        if not self._task_filters:
            return []
        options = vim.PropertyCollector.WaitOptions(maxWaitSeconds=timeout)
        updates = self._task_monitor.WaitForUpdatesEx(self._task_version, options)
        if updates is None:
            return []
        self._task_version = updates.version
        result = []
        for group in updates.filterSet:
            if group.missingSet:
                raise RuntimeError("任务监听对象不可见")
            for item in group.objectSet:
                if item.missingSet or str(item.kind) == "leave":
                    raise RuntimeError("任务监听属性或对象不可见")
                for change in item.changeSet:
                    if change.name == "info.state" and change.val is not None:
                        result.append((item.obj._moId, str(change.val)))
        return result

    def close(self):
        for task_filter in getattr(self, "_task_filters", {}).values():
            try:
                task_filter.DestroyPropertyFilter()
            except Exception:
                pass
        if hasattr(self, "_task_filters"):
            self._task_filters.clear()
        for resource, method in reversed(self._resources):
            try:
                getattr(resource, method)()
            except Exception:
                pass
        self._resources.clear()
        if self.instance is not None:
            try:
                Disconnect(self.instance)
            except Exception:
                pass
        self.instance = self.content = None
        self._task_labels = self._event_metadata = None
