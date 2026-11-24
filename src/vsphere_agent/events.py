"""从当前会话有效边界之后分页采集事件。"""

from datetime import datetime, timezone

from pyVmomi import vim

from .models import Record
from .serialize import document_id, redact


def common_fields(adapter):
    return {"vcenter_id": adapter.vcenter_id, "vcenter_name": adapter.vcenter_name, "vcenter_version": adapter.vcenter_version}


def entity_fields(argument, secrets=()):
    if argument is None:
        return {}
    fields = {}
    name = getattr(argument, "name", None)
    if name is not None:
        fields["entity_name"] = redact(name, secrets, 4096)
    for key in ("vm", "host", "ds", "datacenter", "computeResource", "net", "dvs"):
        obj = getattr(argument, key, None)
        if obj is not None:
            fields["entity_moref"] = obj._moId
            fields["entity_type"] = obj.__class__.__name__
            break
    return fields


class EventCollector:
    def __init__(self, adapter, state, target, boundary, emit, page_size=500, secrets=(), active=lambda: True):
        self.adapter, self.state, self.target = adapter, state, target
        self.boundary, self.emit, self.page_size = boundary, emit, page_size
        self.secrets, self.active = secrets, active

    def collect(self):
        for page in self.adapter.read_events(self.page_size):
            for event in page:
                if not self.active():
                    return
                created = event.createdTime
                if created.tzinfo is None or created.utcoffset() is None:
                    raise ValueError("事件时间没有时区")
                if created <= self.boundary:
                    continue
                key = event.key
                fields = common_fields(self.adapter)
                fields.update(event_id=key, raw_type=event.__class__.__name__)
                for source, target in (("fullFormattedMessage", "description"), ("userName", "user_name"),
                                       ("eventTypeId", "event_type_id"), ("severity", "event_severity")):
                    value = getattr(event, source, None)
                    if value:
                        fields[target] = redact(value, self.secrets, 4096)
                chain_id = getattr(event, "chainId", None)
                if chain_id is not None:
                    fields["event_chain_id"] = chain_id
                lookup = getattr(self.adapter, "event_metadata", None)
                if lookup:
                    type_name, category = lookup(event.__class__)
                    if type_name:
                        fields["event_type_name"] = redact(type_name, self.secrets, 4096)
                    if category:
                        fields["event_category"] = redact(category, self.secrets, 4096)
                for name in ("vm", "host", "ds", "datacenter", "computeResource", "net", "dvs"):
                    item = getattr(event, name, None)
                    if item is not None:
                        fields.update(entity_fields(item, self.secrets))
                        break
                if not fields.get("entity_name") and getattr(event, "objectName", None):
                    fields["entity_name"] = redact(event.objectName, self.secrets, 4096)
                if isinstance(event, vim.event.TaskEvent):
                    task_key = getattr(getattr(event, "info", None), "key", None)
                    if task_key:
                        fields["task_key"] = redact(task_key, self.secrets, 4096)
                message = fields.get("description") or getattr(event, "message", None)
                if not message:
                    message = "事件：" + fields.get("event_type_name", fields.get("event_type_id", fields["raw_type"]))
                    if fields.get("entity_name"):
                        message += "；对象：" + fields["entity_name"]
                fields["message"] = redact(message, self.secrets, 4096)
                if not self.active():
                    return
                if not self.state.remember_event(self.adapter.vcenter_id, key, datetime.now(timezone.utc).timestamp()):
                    continue
                self.emit(Record(self.target, document_id(self.adapter.vcenter_id, key), "event", created, fields))
