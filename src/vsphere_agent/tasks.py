"""任务发现与 PropertyCollector 观察结果的状态归并。"""

from collections import OrderedDict
from datetime import datetime, timezone
from threading import RLock

from .events import common_fields
from .models import Record
from .serialize import document_id, redact


ACTIVE = {"queued", "running"}
TASK_STATE_LABELS = {"queued": "排队中", "running": "运行中", "success": "成功", "error": "失败"}
VOLATILE_FIELDS = ("progress", "task_detail", "error", "start_time", "complete_time", "queue_duration_ms", "metadata_status")


class TaskTracker:
    def __init__(self, adapter, target, boundary, session_id, emit, clock=None, secrets=(), max_completed=10000):
        self.adapter, self.target, self.boundary = adapter, target, boundary
        self.session_id, self.emit = session_id, emit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.secrets, self.max_completed = secrets, max_completed
        self._states, self._refs, self._completed = {}, {}, OrderedDict()
        self._sequence = 0
        self._lock = RLock()

    def _fields(self, info, resolve_label=True):
        fields = common_fields(self.adapter)
        fields.update(task_key=str(info.key), raw_type=info.__class__.__name__)
        for source, target in (("descriptionId", "description"), ("entityName", "entity_name"), ("progress", "progress")):
            value = getattr(info, source, None)
            if value is not None:
                fields[target] = redact(value, self.secrets, 4096) if isinstance(value, str) else value
        description_id = getattr(info, "descriptionId", None)
        lookup = getattr(self.adapter, "task_label", None) if resolve_label else None
        if description_id and lookup:
            label = lookup(description_id)
            if label:
                fields["task_name"] = redact(label, self.secrets, 4096)
        description = getattr(info, "description", None)
        detail = getattr(description, "message", None)
        if detail:
            fields["task_detail"] = redact(detail, self.secrets, 4096)
        for source, target in (("queueTime", "queue_time"), ("startTime", "start_time"), ("completeTime", "complete_time")):
            value = getattr(info, source, None)
            if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
                fields[target] = value
        if "queue_time" in fields and "start_time" in fields:
            duration = (fields["start_time"] - fields["queue_time"]).total_seconds()
            if duration >= 0:
                fields["queue_duration_ms"] = int(duration * 1000)
        entity = getattr(info, "entity", None)
        if entity is not None:
            fields["entity_moref"] = entity._moId
            fields["entity_type"] = entity.__class__.__name__
        reason = getattr(info, "reason", None)
        user = getattr(reason, "userName", None)
        if user:
            fields["user_name"] = redact(user, self.secrets)
        error = getattr(info, "error", None)
        error_message = getattr(error, "localizedMessage", None)
        if error_message:
            fields["error"] = redact(error_message, self.secrets)
        return fields

    def baseline(self, infos):
        for info in infos:
            if info.queueTime.tzinfo is None or info.queueTime.utcoffset() is None:
                raise ValueError("任务时间没有时区")
            if info.queueTime > self.boundary:
                self.discover(info)
                continue
            key, state = str(info.key), str(info.state)
            if state in ACTIVE:
                with self._lock:
                    self._states[key] = (state, self._fields(info))
                    self._refs[info.task._moId] = key

    def _emit(self, key, state, fields):
        self._sequence += 1
        data = dict(fields, state=state, state_label=TASK_STATE_LABELS.get(state, "未识别"))
        name = data.get("task_name", data.get("description", key))
        entity = data.get("entity_name", data.get("entity_moref"))
        parts = [f"任务：{name}"]
        if entity:
            parts.append(f"对象：{entity}")
        parts.append(f"状态：{data['state_label']}")
        if data.get("error"):
            parts.append(f"错误：{data['error']}")
        elif data.get("task_detail"):
            parts.append(f"详情：{data['task_detail']}")
        data["message"] = redact("；".join(parts), self.secrets, 4096)
        self.emit(Record(self.target, document_id(self.adapter.vcenter_id, self.session_id, key, self._sequence), "task", self.clock(), data))

    def _finish(self, key):
        self._refs = {moid: task_key for moid, task_key in self._refs.items() if task_key != key}
        self._completed[key] = None
        while len(self._completed) > self.max_completed:
            old, _ = self._completed.popitem(last=False)
            self._states.pop(old, None)

    def discover(self, info):
        if info.queueTime.tzinfo is None or info.queueTime.utcoffset() is None:
            raise ValueError("任务时间没有时区")
        if info.queueTime <= self.boundary:
            return
        key, state = str(info.key), str(info.state)
        with self._lock:
            if key in self._states:
                return
            fields = self._fields(info)
            self._states[key] = (state, fields)
            self._emit(key, state, fields)
            if state in ACTIVE:
                self._refs[info.task._moId] = key
            else:
                self._finish(key)

    def update(self, moid, state, info=None):
        with self._lock:
            key = self._refs.get(moid)
            if key is None:
                return
            previous, old_fields = self._states[key]
            if state == previous:
                return
            fields = dict(old_fields)
            for name in VOLATILE_FIELDS:
                fields.pop(name, None)
            if info is None:
                fields["metadata_status"] = "unavailable"
            elif str(getattr(info, "key", "")) != key or str(getattr(info, "state", "")) != state:
                fields["metadata_status"] = "state_changed_during_lookup"
            else:
                fields.update(self._fields(info, resolve_label=False))
            self._states[key] = (state, fields)
            self._emit(key, state, fields)
            if state not in ACTIVE:
                self._finish(key)

    def active_refs(self):
        with self._lock:
            return dict(self._refs)
